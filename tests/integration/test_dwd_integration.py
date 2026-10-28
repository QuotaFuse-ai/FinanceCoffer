"""DWD write path against a migrated PostgreSQL (T-006).

Each test runs inside the scratch database built by ``migrated_warehouse`` and
replays the same transaction ``ingest event`` uses: register, land to ODS, then
write the DWD fact, committing once.

Version bumps need two *different* business keys for the same DWD business key,
because the deterministic ``event_id`` is derived from ``biz_key``: sharing a
``biz_key`` would be an event-log replay instead of a correction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from typer.testing import CliRunner

from financecoffer.cli import app
from financecoffer.core.errors import ErrorCode, InvalidArgumentError
from financecoffer.db.models.dwd import (
    StockAnnouncement,
    StockFinancialSnapshot,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.models.ops import EtlEventLog
from financecoffer.db.session import Database
from financecoffer.dwd import WriteOutcome, write_event
from financecoffer.events import compute_event_id, register_event, validate_event
from financecoffer.ods import land_event

PROVIDER = "t006-dwd"
SYMBOL = "900006"
CRAWL_TIME = "2026-10-27T02:00:00Z"

_DWD_MODELS = (
    StockFinancialSnapshot,
    StockAnnouncement,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)

runner = CliRunner()


@pytest.fixture()
def db(migrated_warehouse: str) -> Iterator[Database]:
    database = Database(migrated_warehouse)
    try:
        yield database
    finally:
        try:
            with database.session_scope() as session:
                for model in _DWD_MODELS:
                    session.execute(delete(model).where(model.provider == PROVIDER))
                session.execute(delete(StockRawData).where(StockRawData.symbol == SYMBOL))
                session.execute(delete(EtlEventLog).where(EtlEventLog.provider == PROVIDER))
        finally:
            database.dispose()


def payload(
    data_type: str,
    *,
    biz_key: str,
    content: dict[str, Any],
    source_info: dict[str, Any] | None = None,
    crawl_time: str = CRAWL_TIME,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider=PROVIDER, data_type=data_type, symbol=SYMBOL, biz_key=biz_key
        ),
        "biz_key": biz_key,
        "event_time": "2026-10-27T10:00:00+08:00",
        "data_type": data_type,
        "symbol": SYMBOL,
        "market": "CN",
        "source_info": {"provider": PROVIDER, "crawl_time": crawl_time},
        "content": content,
        "trace_id": "trace-t006",
    }
    if source_info is not None:
        body["source_info"].update(source_info)
    return body


def financial(
    *,
    report_date: str = "2026-09-30",
    report_type: str = "Q3",
    revenue: str = "1234567890123.4567",
    profit: str = "-5.5000",
    quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "quality_metrics": quality or {"quality_status": "normal", "confidence": "high"},
        "structured_fields": {
            "report_date": report_date,
            "report_type": report_type,
            "amount_revenue": revenue,
            "amount_net_profit": profit,
        },
    }


def announcement(
    text: str = "Q3 revenue grew 12% year over year",
    *,
    title: str = "Q3 earnings",
    ann_type: str = "periodic",
    publish_date: str = "2026-10-20",
) -> dict[str, Any]:
    return {
        "quality_metrics": {"quality_status": "normal"},
        "raw_html": f"<p>{text}</p>",
        "clean_content": text,
        "structured_fields": {
            "title": title,
            "ann_type": ann_type,
            "publish_date": publish_date,
        },
    }


def kline(*, trade_date: str = "2026-10-20", close: str = "11.1100") -> dict[str, Any]:
    return {
        "quality_metrics": {"quality_status": "normal"},
        "structured_fields": {
            "trade_date": trade_date,
            "open": "10.0100",
            "high": "12.5000",
            "low": "9.9900",
            "close": close,
            "volume": "1000000",
            "amount": "11110000",
            "adjust": "qfq",
        },
    }


def quote(*, trade_date: str = "2026-10-20", close: str = "11.1100") -> dict[str, Any]:
    return {
        "quality_metrics": {"quality_status": "normal"},
        "structured_fields": {
            "trade_date": trade_date,
            "close": close,
            "total_market_cap": "123456789.0100",
            "pe_ttm": "15.2000",
        },
    }


def risk(*, occur_date: str = "2026-10-01") -> dict[str, Any]:
    return {
        "quality_metrics": {"quality_status": "suspicious", "confidence": "medium"},
        "structured_fields": {
            "risk_type": "litigation",
            "occur_date": occur_date,
            "severity": "high",
            "summary": "material lawsuit disclosed",
        },
        "source_announcement_ref": "ann-2026-10-01",
    }


def submit(
    database: Database,
    body: dict[str, Any],
    *,
    compliance_enabled: bool = False,
    raw_text: str | None = None,
) -> tuple[Any, Any, WriteOutcome | None]:
    """The ``ingest event`` transaction: register, land, write DWD, commit once."""

    event = validate_event(body, compliance_enabled=compliance_enabled)
    text = raw_text if raw_text is not None else json.dumps(body, ensure_ascii=False)
    with database.session_scope() as session:
        registration = register_event(session, event)
        if registration.duplicate:
            return registration, None, None
        landing = land_event(session, event, raw_text=text)
        fact = write_event(session, event, compliance_enabled=compliance_enabled)
    return registration, landing, fact


def count(database: Database, model: Any, *conditions: Any) -> int:
    with database.session_scope() as session:
        statement = select(func.count()).select_from(model).where(*conditions)
        return int(session.scalar(statement) or 0)


def one(database: Database, model: Any, *conditions: Any) -> Any:
    with database.session_scope() as session:
        return session.scalars(select(model).where(*conditions)).one()


def rows(database: Database, model: Any, *conditions: Any) -> list[Any]:
    with database.session_scope() as session:
        statement = select(model).where(*conditions).order_by(model.version)
        return list(session.scalars(statement))


def test_new_report_period_appends_a_version_and_keeps_history(db: Database) -> None:
    first = payload("financial", biz_key="2026Q3-v1", content=financial(revenue="100.0000"))
    second = payload("financial", biz_key="2026Q3-v2", content=financial(revenue="200.0000"))

    _, _, first_fact = submit(db, first)
    _, _, second_fact = submit(db, second)

    assert first_fact is not None
    assert first_fact.status == "inserted"
    assert first_fact.table == "dwd.stock_financial_snapshot"
    assert first_fact.version == 1
    assert second_fact is not None
    assert second_fact.status == "inserted"
    assert second_fact.version == 2

    stored = rows(db, StockFinancialSnapshot, StockFinancialSnapshot.symbol == SYMBOL)
    assert [row.version for row in stored] == [1, 2]
    assert stored[0].amount_revenue == Decimal("100.0000")
    assert stored[1].amount_revenue == Decimal("200.0000")
    assert stored[0].event_id == first["event_id"]
    assert stored[1].event_id == second["event_id"]
    assert stored[0].fetch_time == datetime(2026, 10, 27, 2, 0, tzinfo=UTC)


def test_replaying_an_event_id_is_skipped_without_a_version_bump(db: Database) -> None:
    body = payload("quote", biz_key="quote-replay", content=quote())

    _, _, first_fact = submit(db, body)
    assert first_fact is not None
    assert first_fact.version == 1

    event = validate_event(body)
    with db.session_scope() as session:
        replay = write_event(session, event, compliance_enabled=False)
    assert replay.status == "skipped"
    assert replay.inserted is False
    assert replay.version is None

    registration, landing, fact = submit(db, body)
    assert registration.duplicate is True
    assert landing is None
    assert fact is None
    assert count(db, StockQuoteSnapshot, StockQuoteSnapshot.symbol == SYMBOL) == 1


@pytest.mark.parametrize(
    ("data_type", "model", "table", "content"),
    [
        ("kline", StockKline, "dwd.stock_kline", kline()),
        ("quote", StockQuoteSnapshot, "dwd.stock_quote_snapshot", quote()),
    ],
)
def test_kline_and_quote_append_versions(
    db: Database, data_type: str, model: Any, table: str, content: dict[str, Any]
) -> None:
    _, _, first_fact = submit(db, payload(data_type, biz_key="v1", content=content))
    _, _, second_fact = submit(db, payload(data_type, biz_key="v2", content=content))

    assert first_fact is not None
    assert (first_fact.status, first_fact.version, first_fact.table) == ("inserted", 1, table)
    assert second_fact is not None
    assert (second_fact.status, second_fact.version, second_fact.table) == ("inserted", 2, table)
    assert count(db, model, model.symbol == SYMBOL) == 2


def test_bad_quality_lands_verbatim(db: Database) -> None:
    quality = {
        "quality_status": "bad",
        "confidence": "low",
        "balance_check_result": {"balanced": False, "diff": "0.0100"},
    }
    body = payload("financial", biz_key="bad-quality", content=financial(quality=quality))

    _, _, fact = submit(db, body)

    assert fact is not None
    assert fact.status == "inserted"
    stored = one(db, StockFinancialSnapshot, StockFinancialSnapshot.event_id == body["event_id"])
    assert stored.quality_status == "bad"
    assert stored.confidence == "low"
    assert stored.balance_check_result == {"balanced": False, "diff": "0.0100"}


def test_precise_amount_round_trips_as_decimal(db: Database) -> None:
    body = payload(
        "financial",
        biz_key="precise-amount",
        content=financial(revenue="1234567890123456.1234", profit="-9999999999999999.9999"),
    )

    _, _, fact = submit(db, body)

    assert fact is not None
    stored = one(db, StockFinancialSnapshot, StockFinancialSnapshot.event_id == body["event_id"])
    assert isinstance(stored.amount_revenue, Decimal)
    assert stored.amount_revenue == Decimal("1234567890123456.1234")
    assert stored.amount_revenue.as_tuple().exponent == -4
    assert not isinstance(stored.amount_revenue, float)
    assert stored.amount_net_profit == Decimal("-9999999999999999.9999")


@pytest.mark.parametrize(
    "revenue",
    ["10000000000000000.0000", "1.00001"],
    ids=["overflow", "too-many-decimals"],
)
def test_unrepresentable_amount_rolls_back_the_whole_ingest(
    db: Database, revenue: str
) -> None:
    body = payload("financial", biz_key="bad-amount", content=financial(revenue=revenue))

    with pytest.raises(InvalidArgumentError) as caught:
        submit(db, body)

    assert caught.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert caught.value.details is not None
    assert caught.value.details["field"] == "content.structured_fields.amount_revenue"

    # The event-log claim, ODS row and DWD row shared one transaction.
    assert count(db, EtlEventLog, EtlEventLog.provider == PROVIDER) == 0
    assert count(db, StockRawData, StockRawData.symbol == SYMBOL) == 0
    assert count(db, StockFinancialSnapshot, StockFinancialSnapshot.symbol == SYMBOL) == 0


def test_compliance_off_writes_both_columns_as_null(db: Database) -> None:
    body = payload(
        "quote",
        biz_key="compliance-off",
        content=quote(trade_date="2026-10-20"),
        source_info={"compliance_tier": "S", "commercial_use": True},
    )

    _, _, fact = submit(db, body, compliance_enabled=False)

    assert fact is not None
    stored = one(db, StockQuoteSnapshot, StockQuoteSnapshot.event_id == body["event_id"])
    assert stored.compliance_tier is None
    assert stored.commercial_use is None


def test_compliance_on_copies_the_event_mark(db: Database) -> None:
    body = payload(
        "quote",
        biz_key="compliance-on",
        content=quote(trade_date="2026-10-21"),
        source_info={"compliance_tier": "s", "commercial_use": True},
    )

    _, _, fact = submit(db, body, compliance_enabled=True)

    assert fact is not None
    stored = one(db, StockQuoteSnapshot, StockQuoteSnapshot.event_id == body["event_id"])
    assert stored.compliance_tier == "S"
    assert stored.commercial_use is True


def test_announcement_same_content_hash_is_not_duplicated(db: Database) -> None:
    text = "Q3 revenue grew 12% year over year"
    first = payload("announcement", biz_key="ann-1", content=announcement(text))
    second = payload("announcement", biz_key="ann-2", content=announcement(text))

    _, _, first_fact = submit(db, first)
    _, second_landing, second_fact = submit(db, second)

    assert first_fact is not None
    assert first_fact.status == "inserted"
    assert first_fact.table == "dwd.stock_announcement"
    assert first_fact.version is None
    # ODS still keeps the re-delivered payload; only the DWD fact is deduplicated.
    assert second_landing is not None
    assert second_fact is not None
    assert second_fact.status == "skipped"

    stored = one(db, StockAnnouncement, StockAnnouncement.symbol == SYMBOL)
    assert count(db, StockAnnouncement, StockAnnouncement.symbol == SYMBOL) == 1
    assert stored.event_id == first["event_id"]
    assert stored.content_hash == hashlib.sha256(text.encode()).hexdigest()
    assert stored.clean_content == text
    assert stored.minio_url is None
    assert stored.ann_date == date(2026, 10, 20)
    assert stored.title == "Q3 earnings"
    assert stored.ann_type == "periodic"


def test_announcement_text_is_not_clipped(db: Database) -> None:
    text = "x" * 20_000
    body = payload("announcement", biz_key="ann-long", content=announcement(text))

    _, _, fact = submit(db, body)

    assert fact is not None
    stored = one(db, StockAnnouncement, StockAnnouncement.event_id == body["event_id"])
    assert stored.clean_content == text
    assert len(stored.clean_content) == 20_000
    assert stored.content_hash == hashlib.sha256(text.encode()).hexdigest()


def test_risk_is_inserted_once_by_event_id(db: Database) -> None:
    body = payload("risk", biz_key="risk-1", content=risk())

    _, _, first_fact = submit(db, body)
    assert first_fact is not None
    assert (first_fact.status, first_fact.table) == ("inserted", "dwd.stock_risk_event")

    event = validate_event(body)
    with db.session_scope() as session:
        replay = write_event(session, event, compliance_enabled=False)
    assert replay.status == "skipped"

    assert count(db, StockRiskEvent, StockRiskEvent.symbol == SYMBOL) == 1
    stored = one(db, StockRiskEvent, StockRiskEvent.event_id == body["event_id"])
    assert stored.quality_status == "suspicious"
    assert stored.source_announcement_ref == "ann-2026-10-01"


def test_cli_ingest_reports_the_dwd_row(
    db: Database, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FC_COMPLIANCE_ENABLED", raising=False)
    body = payload("quote", biz_key="cli-dwd", content=quote())
    event_file = tmp_path / "quote.json"
    event_file.write_bytes(json.dumps(body, ensure_ascii=False).encode("utf-8"))

    first = runner.invoke(
        app, ["--db-url", db.dsn, "ingest", "event", "--file", str(event_file)]
    )
    assert first.exit_code == 0, first.stdout
    first_data = json.loads(first.stdout)["data"]
    assert first_data["duplicate"] is False
    assert first_data["dwd"]["status"] == "inserted"
    assert first_data["dwd"]["table"] == "dwd.stock_quote_snapshot"
    assert first_data["dwd"]["version"] == 1

    second = runner.invoke(
        app, ["--db-url", db.dsn, "ingest", "event", "--file", str(event_file)]
    )
    assert second.exit_code == 0, second.stdout
    second_data = json.loads(second.stdout)["data"]
    assert second_data["duplicate"] is True
    assert second_data["dwd"] is None
    assert count(db, StockQuoteSnapshot, StockQuoteSnapshot.symbol == SYMBOL) == 1


# -- historical reads, deterministic version pick, verify (QUOT-117) ---------


def _show(db: Database, *extra: str):
    return runner.invoke(
        app,
        ["--db-url", db.dsn, "dwd", "show", "--symbol", SYMBOL, *extra],
    )


def _financial_with_statement(
    *, report_date: str = "2026-09-30", statement: str, **kwargs: Any
) -> dict[str, Any]:
    content = financial(report_date=report_date, **kwargs)
    content["structured_fields"]["statement"] = statement
    return content


def test_cli_dwd_show_reads_a_historical_version(db: Database) -> None:
    # B1-04: an incomplete primary key made the version lookup blow up as
    # E_STORAGE; a full-PK query must return the older snapshot instead.
    submit(
        db,
        payload("financial", biz_key="history-v1", content=financial(revenue="100.0000")),
    )
    submit(
        db,
        payload("financial", biz_key="history-v2", content=financial(revenue="200.0000")),
    )

    result = _show(
        db, "--report-date", "2026-09-30", "--report-type", "Q3", "--version", "1"
    )

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["version"] == 1
    assert data["amount_revenue"] == "100.0000"
    assert data["statement"] == ""
    assert data["available_versions"] == [
        {"statement": "", "version": 2},
        {"statement": "", "version": 1},
    ]


def test_cli_dwd_show_missing_version_reports_available_ones(db: Database) -> None:
    submit(db, payload("financial", biz_key="missing-v1", content=financial()))

    result = _show(
        db, "--report-date", "2026-09-30", "--report-type", "Q3", "--version", "9"
    )

    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["error"]["code"] == "E_NOT_FOUND"
    assert envelope["error"]["details"]["available_versions"] == [
        {"statement": "", "version": 1}
    ]


def test_cli_dwd_show_prefers_the_statement_with_more_amounts(db: Database) -> None:
    thin = _financial_with_statement(
        report_date="2026-06-30", statement="", revenue="300.0000", profit=None
    )
    rich = _financial_with_statement(
        report_date="2026-06-30",
        statement="consolidated",
        revenue="100.0000",
        profit="10.0000",
    )
    submit(db, payload("financial", biz_key="amounts-thin", content=thin))
    submit(db, payload("financial", biz_key="amounts-rich", content=rich))

    result = _show(
        db, "--report-date", "2026-06-30", "--report-type", "Q3", "--version", "1"
    )

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["statement"] == "consolidated"
    assert data["amount_revenue"] == "100.0000"


def test_cli_dwd_show_breaks_an_amount_tie_by_statement(db: Database) -> None:
    submit(
        db,
        payload(
            "financial",
            biz_key="tie-plain",
            content=_financial_with_statement(report_date="2026-03-31", statement=""),
        ),
    )
    submit(
        db,
        payload(
            "financial",
            biz_key="tie-consolidated",
            content=_financial_with_statement(
                report_date="2026-03-31", statement="consolidated"
            ),
        ),
    )

    result = _show(
        db, "--report-date", "2026-03-31", "--report-type", "Q3", "--version", "1"
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["data"]["statement"] == ""


def test_cli_dwd_versions_lists_statement_and_version(db: Database) -> None:
    submit(
        db,
        payload(
            "financial",
            biz_key="versions-plain",
            content=_financial_with_statement(report_date="2026-01-31", statement=""),
        ),
    )
    submit(
        db,
        payload(
            "financial",
            biz_key="versions-consolidated",
            content=_financial_with_statement(
                report_date="2026-01-31", statement="consolidated"
            ),
        ),
    )

    result = runner.invoke(
        app,
        [
            "--db-url",
            db.dsn,
            "dwd",
            "versions",
            "--symbol",
            SYMBOL,
            "--report-date",
            "2026-01-31",
            "--report-type",
            "Q3",
        ],
    )

    assert result.exit_code == 0, result.stdout
    versions = json.loads(result.stdout)["data"]["versions"]
    assert [(item["statement"], item["version"]) for item in versions] == [
        ("", 1),
        ("consolidated", 1),
    ]


def test_cli_dwd_verify_flags_a_snapshot_without_any_amount(db: Database) -> None:
    # B1-14: each amount is optional, but a snapshot with neither carries no
    # key figure; verify must report it instead of silently passing.
    submit(
        db,
        payload(
            "financial",
            biz_key="verify-present",
            content=financial(report_date="2026-02-28"),
        ),
    )
    submit(
        db,
        payload(
            "financial",
            biz_key="verify-empty",
            content=financial(report_date="2026-02-28", revenue=None, profit=None),
        ),
    )

    result = runner.invoke(
        app, ["--db-url", db.dsn, "dwd", "verify", "--symbol", SYMBOL]
    )

    assert result.exit_code == 8, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["error"]["code"] == "E_PARTIAL_FAILURE"
    details = envelope["error"]["details"]
    assert details["checked"] == 2
    assert details["failed"] == 1
    assert details["failures"][0]["issues"] == [
        {
            "field": "amount_revenue,amount_net_profit",
            "reason": (
                "missing: both amounts are NULL,"
                " the snapshot carries no financial figure"
            ),
        }
    ]
