"""ETL consumer end-to-end against real Redis and a migrated PostgreSQL.

The scratch database comes from ``migrated_warehouse``; Redis is the local
instance (``FC_TEST_REDIS_URL``, DB 1 by default). Every test owns a unique
stream / group / DLQ / control key and a unique symbol, and the fixture removes
both before and after the test -- the production stream is never touched.
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
from typer.testing import CliRunner

from financecoffer.cli import app
from financecoffer.core.config import Config
from financecoffer.core.errors import FinanceCofferError
from financecoffer.db.models.dwd import StockQuoteSnapshot
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.models.ops import EtlConsumerOffset, EtlDlq, EtlEventLog
from financecoffer.db.session import Database
from financecoffer.etl.client import close_client, create_client
from financecoffer.etl.consumer import Consumer
from financecoffer.etl.dlq import list_entries, retry_dlq
from financecoffer.etl.metrics import DUPLICATE_EVENTS_TOTAL, METRICS
from financecoffer.etl.status import collect_status
from financecoffer.events import compute_event_id

TEST_REDIS_URL = os.environ.get("FC_TEST_REDIS_URL", "redis://127.0.0.1:6379/1")
PROVIDER = "t007-etl-it"
CRAWL_TIME = "2026-10-27T02:00:00Z"

runner = CliRunner()


@dataclass
class Etl:
    client: Any
    database: Database
    config: Config
    stream: str
    group: str
    dlq: str
    control: str
    symbol: str

    def publish(self, body: dict[str, Any]) -> str:
        """Add the event exactly as upstream would: one ``payload`` field."""

        return self.client.xadd(
            self.stream, {"payload": json.dumps(body, ensure_ascii=False)}
        )

    def consume(self, *, name: str = "it-consumer", once: bool = True) -> Any:
        instance = Consumer(
            client=self.client,
            database=self.database,
            config=self.config,
            consumer_name=name,
        )
        return instance.run(once=once, block_ms=0)


def quote(symbol: str, *, biz_key: str, close: str = "11.1100") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider=PROVIDER, data_type="quote", symbol=symbol, biz_key=biz_key
        ),
        "biz_key": biz_key,
        "event_time": "2026-10-27T02:00:00Z",
        "data_type": "quote",
        "symbol": symbol,
        "market": "CN",
        "source_info": {"provider": PROVIDER, "crawl_time": CRAWL_TIME},
        "content": {
            "quality_metrics": {"quality_status": "normal"},
            "structured_fields": {
                "trade_date": "2026-10-20",
                "close": close,
                "total_market_cap": "123456789.0100",
                "pe_ttm": "15.2000",
            },
        },
    }


@pytest.fixture()
def etl(migrated_warehouse: str) -> Iterator[Etl]:
    try:
        client = create_client(TEST_REDIS_URL)
    except FinanceCofferError:
        pytest.skip(f"Redis is not reachable at {TEST_REDIS_URL}")

    token = uuid.uuid4().hex[:10]
    stream = f"t007-it:{token}:events"
    group = f"t007-it:{token}-group"
    dlq = f"t007-it:{token}:dlq"
    control = f"t007-it:{token}:control"
    symbol = f"9{uuid.uuid4().int % 90000 + 10000:05d}"

    try:
        config = Config(
            db_url=migrated_warehouse,
            redis_url=TEST_REDIS_URL,
            event_stream=stream,
            event_group=group,
            etl_dlq_stream=dlq,
            etl_control_key=control,
            etl_batch_size=10,
            etl_block_ms=0,
            etl_backoff_ms=0,
            etl_claim_idle_ms=0,
            etl_max_attempts=3,
        )
        database = Database(migrated_warehouse)
        environment = Etl(
            client=client,
            database=database,
            config=config,
            stream=stream,
            group=group,
            dlq=dlq,
            control=control,
            symbol=symbol,
        )
        _cleanup(environment)
        METRICS.reset()
        yield environment
    finally:
        try:
            _cleanup(environment)
            METRICS.reset()
        finally:
            database.dispose()
            close_client(client)


def _cleanup(environment: Etl) -> None:
    for key in (environment.stream, environment.dlq, environment.control):
        environment.client.delete(key)
    with environment.database.session_scope() as session:
        session.execute(delete(EtlDlq).where(EtlDlq.stream == environment.stream))
        session.execute(
            delete(EtlConsumerOffset).where(EtlConsumerOffset.stream == environment.stream)
        )
        session.execute(delete(StockRawData).where(StockRawData.symbol == environment.symbol))
        session.execute(
            delete(StockQuoteSnapshot).where(StockQuoteSnapshot.symbol == environment.symbol)
        )
        session.execute(delete(EtlEventLog).where(EtlEventLog.symbol == environment.symbol))


def _count(database: Database, model: Any, *conditions: Any) -> int:
    with database.session_scope() as session:
        statement = select(func.count()).select_from(model).where(*conditions)
        return int(session.scalar(statement) or 0)


def _pending(client: Any, stream: str, group: str) -> int:
    return int((client.xpending(stream, group) or {}).get("pending", 0) or 0)


def test_a_valid_event_lands_writes_and_is_acknowledged(etl: Etl) -> None:
    body = quote(etl.symbol, biz_key="valid-1")
    etl.publish(body)

    report = etl.consume()

    assert report.group_state == "created"
    assert report.written == 1
    assert report.duplicate == 0
    assert report.dlq == 0
    assert report.pending_after == 0
    assert _pending(etl.client, etl.stream, etl.group) == 0

    with etl.database.session_scope() as session:
        raw = session.scalars(
            select(StockRawData).where(StockRawData.event_id == body["event_id"])
        ).one()
        assert raw.raw_text == json.dumps(body, ensure_ascii=False)
        assert raw.raw_json == body
        assert raw.etl_status == "pending"

    with etl.database.session_scope() as session:
        fact = session.scalars(
            select(StockQuoteSnapshot).where(
                StockQuoteSnapshot.event_id == body["event_id"]
            )
        ).one()
        assert fact.version == 1
        log = session.get(EtlEventLog, body["event_id"])
        assert log is not None and log.status == "done"

    assert etl.client.xpending(etl.stream, etl.group)["pending"] == 0
    assert etl.client.xinfo_consumers(etl.stream, etl.group)[0]["name"] == "it-consumer"
    assert etl.client.xlen(etl.stream) == 1


def test_a_duplicate_event_is_acknowledged_without_a_second_write(etl: Etl) -> None:
    body = quote(etl.symbol, biz_key="dup-1")
    etl.publish(body)
    etl.publish(body)

    report = etl.consume()

    assert report.written == 1
    assert report.duplicate == 1
    assert report.dlq == 0
    assert report.pending_after == 0

    assert _count(etl.database, StockRawData, StockRawData.symbol == etl.symbol) == 1
    assert (
        _count(etl.database, StockQuoteSnapshot, StockQuoteSnapshot.symbol == etl.symbol) == 1
    )
    assert METRICS.get(DUPLICATE_EVENTS_TOTAL) == 1


def test_a_bad_event_is_dead_lettered_then_retried_with_a_corrected_payload(
    etl: Etl, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = quote(etl.symbol, biz_key="broken-1", close="not-a-decimal")
    etl.publish(broken)

    report = etl.consume()

    assert report.written == 0
    assert report.dlq == 1
    assert report.pending_after == 0
    assert _count(etl.database, StockRawData, StockRawData.symbol == etl.symbol) == 0

    with etl.database.session_scope() as session:
        row = session.scalars(select(EtlDlq).where(EtlDlq.stream == etl.stream)).one()
        assert row.status == "pending"
        assert row.reason == "schema"
        assert row.event_id == broken["event_id"]
        assert row.raw_event["payload"] == json.dumps(broken, ensure_ascii=False)
        dlq_id = row.id

    assert etl.client.xlen(etl.dlq) == 1
    listed = list_entries(etl.database)
    assert [item["id"] for item in listed] == [dlq_id]

    corrected = quote(etl.symbol, biz_key="broken-1")
    payload_file = tmp_path / "corrected.json"
    payload_file.write_bytes(json.dumps(corrected, ensure_ascii=False).encode("utf-8"))

    monkeypatch.setenv("FC_REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("FC_EVENT_STREAM", etl.stream)
    monkeypatch.setenv("FC_EVENT_GROUP", etl.group)
    monkeypatch.setenv("FC_ETL_DLQ_STREAM", etl.dlq)
    monkeypatch.setenv("FC_ETL_CONTROL_KEY", etl.control)
    result = runner.invoke(
        app,
        [
            "--db-url",
            etl.database.dsn,
            "etl",
            "dlq",
            "retry",
            "--id",
            str(dlq_id),
            "--file",
            str(payload_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["retried"] == 1

    assert _count(etl.database, StockRawData, StockRawData.symbol == etl.symbol) == 1
    assert (
        _count(etl.database, StockQuoteSnapshot, StockQuoteSnapshot.symbol == etl.symbol) == 1
    )
    with etl.database.session_scope() as session:
        row = session.get(EtlDlq, dlq_id)
        assert row is not None and row.status == "retried"
    assert etl.client.xlen(etl.dlq) == 0


def test_a_restart_drains_its_own_pending_before_new_entries(etl: Etl) -> None:
    first = quote(etl.symbol, biz_key="restart-1")
    second = quote(etl.symbol, biz_key="restart-2")
    etl.client.xgroup_create(etl.stream, etl.group, id="0", mkstream=True)
    etl.publish(first)
    etl.publish(second)

    claimed = etl.client.xreadgroup(
        etl.group, "restarted", {etl.stream: ">"}, count=10
    )
    assert claimed
    assert _pending(etl.client, etl.stream, etl.group) == 2

    report = etl.consume(name="restarted")

    assert report.recovered == 2
    assert report.written == 2
    assert report.pending_after == 0
    assert _count(etl.database, StockRawData, StockRawData.symbol == etl.symbol) == 2


def test_status_reports_the_same_pending_count_as_xpending(etl: Etl) -> None:
    etl.client.xgroup_create(etl.stream, etl.group, id="0", mkstream=True)
    etl.publish(quote(etl.symbol, biz_key="status-1"))
    etl.publish(quote(etl.symbol, biz_key="status-2"))
    etl.client.xreadgroup(etl.group, "ghost", {etl.stream: ">"}, count=10)

    snapshot = collect_status(
        client=etl.client,
        database=etl.database,
        config=etl.config,
        consumer_name="ghost",
    )

    assert snapshot["healthy"] is True
    assert snapshot["pending"] == _pending(etl.client, etl.stream, etl.group) == 2
    assert snapshot["pending_by_consumer"] == [{"name": "ghost", "pending": 2}]
    assert snapshot["lag"] == 0
    assert snapshot["stream_length"] == 2
    assert snapshot["dlq"]["stream_depth"] == 0
    assert snapshot["dlq"]["database_pending"] == 0

    report = etl.consume(name="ghost")
    assert report.recovered == 2
    assert report.pending_after == 0


def test_a_retry_without_a_correction_keeps_the_dead_letter_pending(etl: Etl) -> None:
    broken = quote(etl.symbol, biz_key="still-broken")
    broken["content"]["structured_fields"].pop("close")
    etl.publish(broken)
    etl.consume()

    with etl.database.session_scope() as session:
        row = session.scalars(select(EtlDlq).where(EtlDlq.stream == etl.stream)).one()
        dlq_id = row.id

    result = retry_dlq(
        client=etl.client,
        database=etl.database,
        config=etl.config,
        dlq_id=dlq_id,
    )

    assert result["failed"] == 1
    with etl.database.session_scope() as session:
        row = session.get(EtlDlq, dlq_id)
        assert row is not None
        assert row.status == "pending"
        assert row.retry_count == 1
    assert etl.client.xlen(etl.dlq) == 1
    assert _count(etl.database, StockRawData, StockRawData.symbol == etl.symbol) == 0
