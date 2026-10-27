"""The Redis Stream consumer, end to end against real Redis and PostgreSQL.

Every case runs on its own stream, group, DLQ and control key, created with a
random suffix and deleted afterwards -- the production stream
(``fc:stockdata:events``) is never touched, so two runs on one machine cannot
interfere with each other or with a live ingestion.

What the loop must guarantee (``docs/specs/financecoffer.md`` section 3.6):

* a committed event is acknowledged -- ``XPENDING`` drops back to zero;
* the same event twice is written once, and the second copy is acknowledged;
* a deterministic failure goes to the DLQ immediately, can be replayed after
  the payload is corrected, and leaves the source stream clean;
* a message left pending by a crashed consumer is drained on the next start
  with the same consumer name, instead of waiting for ``XAUTOCLAIM``.

The consumer stores what it receives: quality fields reach the DWD row
verbatim, no metric is recomputed on the way.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from financecoffer.core.config import Config
from financecoffer.db.models.dwd import StockFinancialSnapshot
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.models.ops import EtlConsumerOffset, EtlDlq, EtlEventLog
from financecoffer.db.session import Database
from financecoffer.etl.consumer import Consumer
from financecoffer.etl.dlq import list_entries, retry_dlq
from financecoffer.events import compute_event_id

PROVIDER = "t016-etl"
SYMBOL = "920021"
CRAWL_TIME = "2026-10-27T02:00:00Z"
CONSUMER_NAME = "t016-consumer"
REDIS_URL = os.environ.get("FC_TEST_REDIS_URL", "redis://127.0.0.1:6379/1")


@dataclass(frozen=True)
class Sandbox:
    """One isolated stream family plus the config that points at it."""

    config: Config
    stream: str
    group: str
    dlq: str
    control: str


@pytest.fixture()
def client() -> Iterator[Any]:
    try:
        import redis
    except ModuleNotFoundError:  # pragma: no cover - depends on install extras
        pytest.skip("the redis package is not installed")
    try:
        connection = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        connection.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Redis is not reachable at {REDIS_URL}: {exc}")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def db(migrated_warehouse: str) -> Iterator[Database]:
    database = Database(migrated_warehouse)
    _clean(database, stream=None)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture()
def sandbox(db: Database, client: Any) -> Iterator[Sandbox]:
    suffix = uuid.uuid4().hex[:10]
    stream = f"fc:test:t016:events:{suffix}"
    box = Sandbox(
        config=Config(
            db_url=db.dsn,
            redis_url=REDIS_URL,
            event_stream=stream,
            event_group="fc-test-t016",
            etl_dlq_stream=f"fc:test:t016:dlq:{suffix}",
            etl_control_key=f"fc:test:t016:control:{suffix}",
            etl_consumer_name=CONSUMER_NAME,
            etl_batch_size=10,
            etl_block_ms=0,
            etl_max_attempts=2,
            etl_backoff_ms=0,
            etl_claim_idle_ms=0,
        ),
        stream=stream,
        group="fc-test-t016",
        dlq=f"fc:test:t016:dlq:{suffix}",
        control=f"fc:test:t016:control:{suffix}",
    )
    try:
        yield box
    finally:
        client.delete(box.stream, box.dlq, box.control)
        _clean(db, stream=box.stream)


def _clean(database: Database, *, stream: str | None) -> None:
    with database.session_scope() as session:
        session.execute(
            delete(StockFinancialSnapshot).where(
                StockFinancialSnapshot.provider == PROVIDER
            )
        )
        session.execute(delete(StockRawData).where(StockRawData.symbol == SYMBOL))
        session.execute(delete(EtlEventLog).where(EtlEventLog.provider == PROVIDER))
        if stream is not None:
            session.execute(delete(EtlDlq).where(EtlDlq.stream == stream))
            session.execute(
                delete(EtlConsumerOffset).where(EtlConsumerOffset.stream == stream)
            )


def payload(
    *,
    biz_key: str = "2026-09-30|Q3",
    revenue: str = "1500000.0000",
    quality: str = "suspicious",
    confidence: str = "low",
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider=PROVIDER, data_type="financial", symbol=SYMBOL, biz_key=biz_key
        ),
        "biz_key": biz_key,
        "event_time": "2026-10-27T10:00:00+08:00",
        "data_type": "financial",
        "symbol": SYMBOL,
        "market": "CN",
        "source_info": {"provider": PROVIDER, "crawl_time": CRAWL_TIME},
        "content": {
            "quality_metrics": {"quality_status": quality, "confidence": confidence},
            "structured_fields": {
                "report_date": "2026-09-30",
                "report_type": "Q3",
                "amount_revenue": revenue,
                "amount_net_profit": "120000.0000",
            },
        },
        "trace_id": "trace-t016-etl",
    }


def _publish(client: Any, box: Sandbox, body: dict[str, Any]) -> str:
    return client.xadd(box.stream, {"payload": json.dumps(body, ensure_ascii=False)})


def _run(client: Any, box: Sandbox, db: Database) -> Any:
    consumer = Consumer(client=client, database=db, config=box.config)
    return consumer.run(once=True, block_ms=0)


def _pending(client: Any, box: Sandbox) -> int:
    summary = client.xpending(box.stream, box.group)
    return int(summary.get("pending", 0) or 0)


def _facts(db: Database) -> list[StockFinancialSnapshot]:
    with db.session_scope() as session:
        return list(
            session.scalars(
                select(StockFinancialSnapshot)
                .where(StockFinancialSnapshot.symbol == SYMBOL)
                .order_by(StockFinancialSnapshot.version)
            )
        )


def test_a_committed_entry_is_acknowledged(client: Any, db: Database, sandbox: Sandbox) -> None:
    body = payload()
    entry_id = _publish(client, sandbox, body)

    report = _run(client, sandbox, db)

    assert report.group_state == "created"
    assert report.written == 1
    assert report.duplicate == 0
    assert report.dlq == 0
    assert report.pending_after == 0
    assert _pending(client, sandbox) == 0
    # Acknowledged, not deleted: the stream is still the audit trail.
    assert client.xlen(sandbox.stream) == 1
    assert [item[0] for item in client.xrange(sandbox.stream)] == [entry_id]

    facts = _facts(db)
    assert len(facts) == 1
    assert facts[0].event_id == body["event_id"]
    # Stored, not recomputed: the quality verdict arrives unchanged.
    assert facts[0].quality_status == "suspicious"
    assert facts[0].confidence == "low"

    with db.session_scope() as session:
        assert session.get(EtlEventLog, body["event_id"]).status == "done"
        assert session.execute(
            select(func.count()).select_from(StockRawData).where(
                StockRawData.event_id == body["event_id"]
            )
        ).scalar_one() == 1


def test_the_same_event_twice_is_written_once(client: Any, db: Database, sandbox: Sandbox) -> None:
    body = payload()
    first = _publish(client, sandbox, body)
    second = _publish(client, sandbox, body)
    assert first != second

    report = _run(client, sandbox, db)

    assert report.written == 1
    assert report.duplicate == 1
    assert report.pending_after == 0
    assert _pending(client, sandbox) == 0

    assert len(_facts(db)) == 1
    with db.session_scope() as session:
        assert session.execute(select(func.count()).select_from(StockRawData)).scalar_one() == 1


def test_a_poison_entry_is_dead_lettered_and_can_be_replayed(
    client: Any, db: Database, sandbox: Sandbox
) -> None:
    poison = {"schema_version": "1.0", "trace_id": "trace-t016-etl"}
    _publish(client, sandbox, poison)

    report = _run(client, sandbox, db)

    assert report.written == 0
    assert report.dlq == 1
    assert report.retries == 0, "a deterministic failure must not be retried"
    assert report.pending_after == 0
    assert _pending(client, sandbox) == 0
    assert client.xlen(sandbox.dlq) == 1

    entries = list_entries(db)
    assert len(entries) == 1
    assert entries[0]["status"] == "pending"
    assert entries[0]["source_stream"] == sandbox.stream
    assert entries[0]["reason"]

    corrected = payload()
    replay = retry_dlq(
        client=client,
        database=db,
        config=sandbox.config,
        dlq_id=entries[0]["id"],
        payload=corrected,
        payload_text=json.dumps(corrected, ensure_ascii=False),
    )

    assert replay["requested"] == 1
    assert replay["retried"] == 1, replay
    assert replay["failed"] == 0
    assert replay["results"][0]["event_id"] == corrected["event_id"]
    assert list_entries(db) == []
    assert client.xlen(sandbox.dlq) == 0
    assert [fact.event_id for fact in _facts(db)] == [corrected["event_id"]]


def test_pending_entries_are_recovered_after_a_crash(
    client: Any, db: Database, sandbox: Sandbox
) -> None:
    body = payload()
    client.xgroup_create(sandbox.stream, sandbox.group, id="0", mkstream=True)
    _publish(client, sandbox, body)

    # A consumer that reads the entry and dies before acknowledging it.
    crashed = client.xreadgroup(
        sandbox.group, CONSUMER_NAME, {sandbox.stream: ">"}, count=1
    )
    assert crashed, "the entry must be delivered before the simulated crash"
    assert _pending(client, sandbox) == 1
    assert _facts(db) == []

    report = _run(client, sandbox, db)

    assert report.group_state == "exists"
    assert report.recovered == 1
    assert report.written == 1
    assert report.pending_after == 0
    assert _pending(client, sandbox) == 0
    assert [fact.event_id for fact in _facts(db)] == [body["event_id"]]


def test_a_second_run_finds_nothing_to_do(client: Any, db: Database, sandbox: Sandbox) -> None:
    _publish(client, sandbox, payload())
    first = _run(client, sandbox, db)
    second = _run(client, sandbox, db)

    assert first.written == 1
    assert second.written == 0
    assert second.duplicate == 0
    assert second.batches == 0
    assert second.pending_after == 0
    assert len(_facts(db)) == 1
