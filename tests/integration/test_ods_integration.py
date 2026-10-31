"""ODS landing, raw round-trip and replay against a migrated PostgreSQL (T-005).

The scratch database created by ``migrated_warehouse`` carries partitions for
the current UTC month, the following three, and every month the suite pins
``fetch_time`` to, so the happy path uses October 2026 and the "missing
partition" case uses January 2099.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from typer.testing import CliRunner

from financecoffer.cli import app
from financecoffer.core.config import Config
from financecoffer.core.errors import StorageError
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.models.ops import EtlEventLog
from financecoffer.db.repositories.ops import EventLogRepository
from financecoffer.db.session import Database
from financecoffer.events import compute_event_id, register_event, validate_event
from financecoffer.ods import OdsService, land_event

PROVIDER = "t005-ods"
SYMBOL = "900005"
CRAWL_TIME = "2026-10-27T02:00:00Z"

runner = CliRunner()


@pytest.fixture()
def db(migrated_warehouse: str) -> Iterator[Database]:
    database = Database(migrated_warehouse)
    try:
        yield database
    finally:
        try:
            with database.session_scope() as session:
                session.execute(
                    delete(StockRawData).where(StockRawData.symbol == SYMBOL)
                )
                session.execute(
                    delete(EtlEventLog).where(EtlEventLog.provider == PROVIDER)
                )
        finally:
            database.dispose()


def payload(
    *,
    biz_key: str = "2026-10-27",
    crawl_time: str = CRAWL_TIME,
    event_time: str = "2026-10-27T10:00:00+08:00",
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider=PROVIDER, data_type="quote", symbol=SYMBOL, biz_key=biz_key
        ),
        "biz_key": biz_key,
        "event_time": event_time,
        "data_type": "quote",
        "symbol": SYMBOL,
        "market": "CN",
        "source_info": {"provider": PROVIDER, "crawl_time": crawl_time},
        "content": {
            "quality_metrics": {"quality_status": "normal"},
            "structured_fields": {"trade_date": "2026-10-20", "close": "11.11"},
        },
        "trace_id": "trace-t005",
    }


def submit(
    database: Database, body: dict[str, Any], raw_text: str
) -> tuple[Any, Any]:
    """The ``ingest event`` transaction: register, then land, commit once."""

    event = validate_event(body)
    with database.session_scope() as session:
        registration = register_event(session, event)
        landing = (
            None
            if registration.duplicate
            else land_event(session, event, raw_text=raw_text)
        )
    return registration, landing


def count(database: Database, model: Any, *conditions: Any) -> int:
    with database.session_scope() as session:
        statement = select(func.count()).select_from(model).where(*conditions)
        return int(session.scalar(statement) or 0)


def service(database: Database) -> OdsService:
    dsn = database.dsn
    return OdsService(config=Config(db_url=dsn), dsn=dsn)


def test_the_same_event_lands_once(db: Database) -> None:
    body = payload()
    raw_text = json.dumps(body, ensure_ascii=False, indent=2)

    first_registration, first_landing = submit(db, body, raw_text)
    second_registration, second_landing = submit(db, body, raw_text)

    assert first_registration.duplicate is False
    assert first_landing is not None
    assert second_registration.duplicate is True
    assert second_landing is None

    assert count(db, StockRawData, StockRawData.event_id == body["event_id"]) == 1
    assert count(db, EtlEventLog, EtlEventLog.provider == PROVIDER) == 1

    with db.session_scope() as session:
        row = session.scalars(
            select(StockRawData).where(StockRawData.event_id == body["event_id"])
        ).one()
        assert row.etl_status == "pending"
        assert row.source == "manual"
        assert row.raw_text == raw_text
        assert row.raw_json == body
        assert row.fetch_time == datetime(2026, 10, 27, 2, 0, tzinfo=UTC)


def test_cli_ingest_and_ods_show_raw_round_trip(
    db: Database, tmp_path: Any
) -> None:
    body = payload()
    raw_text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    event_file = tmp_path / "quote.json"
    event_file.write_bytes(raw_text.encode("utf-8"))
    dsn = db.dsn

    first = runner.invoke(
        app, ["--db-url", dsn, "ingest", "event", "--file", str(event_file)]
    )
    assert first.exit_code == 0, first.stdout
    first_payload = json.loads(first.stdout)
    assert first_payload["ok"] is True
    assert first_payload["data"]["duplicate"] is False
    record_id = first_payload["data"]["ods"]["id"]
    assert first_payload["data"]["ods"]["partition"] == "ods.stock_raw_data_202610"

    second = runner.invoke(
        app, ["--db-url", dsn, "ingest", "event", "--file", str(event_file)]
    )
    assert second.exit_code == 0, second.stdout
    second_payload = json.loads(second.stdout)
    assert second_payload["data"]["duplicate"] is True
    assert second_payload["data"]["ods"] is None

    shown = runner.invoke(app, ["--db-url", dsn, "ods", "show", str(record_id)])
    assert shown.exit_code == 0, shown.stdout
    detail = json.loads(shown.stdout)["data"]
    assert detail["raw_json"] == body
    assert detail["has_raw_text"] is True

    raw = runner.invoke(
        app, ["--db-url", dsn, "ods", "show", str(record_id), "--raw"]
    )
    assert raw.exit_code == 0
    assert raw.stdout_bytes == raw_text.encode("utf-8")

    listed = runner.invoke(
        app, ["--db-url", dsn, "ods", "list", "--symbol", SYMBOL]
    )
    assert listed.exit_code == 0, listed.stdout
    listing = json.loads(listed.stdout)["data"]
    assert listing["count"] == 1
    assert listing["records"][0]["id"] == record_id

    stats = runner.invoke(app, ["--db-url", dsn, "ods", "stats", "--group-by", "source"])
    assert stats.exit_code == 0, stats.stdout
    assert {"value": "manual", "count": 1} in json.loads(stats.stdout)["data"]["groups"]


def test_raw_output_preserves_crlf_bytes(db: Database, tmp_path: Any) -> None:
    body = payload(biz_key="2026-10-29")
    raw_text = json.dumps(body, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n"
    event_file = tmp_path / "crlf.json"
    event_file.write_bytes(raw_text.encode("utf-8"))

    ingested = runner.invoke(
        app, ["--db-url", db.dsn, "ingest", "event", "--file", str(event_file)]
    )
    assert ingested.exit_code == 0, ingested.stdout
    record_id = json.loads(ingested.stdout)["data"]["ods"]["id"]

    raw = runner.invoke(app, ["--db-url", db.dsn, "ods", "show", str(record_id), "--raw"])
    assert raw.exit_code == 0
    assert raw.stdout_bytes == raw_text.encode("utf-8")


def test_missing_partition_rolls_back_the_whole_ingest(db: Database) -> None:
    body = payload(crawl_time="2099-01-05T00:00:00Z")

    with pytest.raises(StorageError) as caught:
        submit(db, body, json.dumps(body))

    assert caught.value.details is not None
    assert caught.value.details["partition"] == "ods.stock_raw_data_209901"
    assert caught.value.details["hint"] == "fcoffer db partitions create --month 2099-01"

    # The event-log claim lived in the same transaction and rolled back with it.
    assert count(db, EtlEventLog, EtlEventLog.provider == PROVIDER) == 0
    assert count(db, StockRawData, StockRawData.symbol == SYMBOL) == 0


def test_replay_dry_run_leaves_rows_and_event_log_untouched(db: Database) -> None:
    body = payload()
    raw_text = json.dumps(body)
    _, landing = submit(db, body, raw_text)
    assert landing is not None
    ods = service(db)

    before = ods.show(landing.record_id)
    before_text = ods.raw_text(landing.record_id)

    manifest = ods.replay(record_ids=(landing.record_id,), dry_run=True)

    assert manifest["dry_run"] is True
    assert manifest["count"] == 1
    assert manifest["records"][0]["id"] == landing.record_id
    assert manifest["records"][0]["event_id"] == body["event_id"]

    after = ods.show(landing.record_id)
    assert after == before
    assert ods.raw_text(landing.record_id) == before_text
    assert count(db, StockRawData, StockRawData.symbol == SYMBOL) == 1

    with db.session_scope() as session:
        row = session.get(EtlEventLog, body["event_id"])
        assert row is not None
        assert row.status == "received"
        assert row.processed_at is None


def test_replay_requeues_only_the_event_log(db: Database) -> None:
    body = payload(biz_key="2026-10-28")
    raw_text = json.dumps(body)
    _, landing = submit(db, body, raw_text)
    assert landing is not None
    ods = service(db)
    before = ods.show(landing.record_id)

    with db.session_scope() as session:
        processed = EventLogRepository(session).succeed(body["event_id"])
    assert processed is not None
    assert processed.status == "done"
    assert processed.processed_at is not None

    result = ods.replay(record_ids=(landing.record_id,))

    assert result["dry_run"] is False
    assert result["requeued"] == 1
    assert result["unmatched"] == []

    with db.session_scope() as session:
        row = session.get(EtlEventLog, body["event_id"])
        assert row is not None
        assert row.status == "received"
        assert row.error is None
        assert row.processed_at is None

    after = ods.show(landing.record_id)
    assert after == before
    assert after["etl_status"] == "pending"
    assert ods.raw_text(landing.record_id) == raw_text
    assert count(db, StockRawData, StockRawData.symbol == SYMBOL) == 1
