"""One event through ODS, DWD (versioned), DWS and the REST surface.

The write path is driven through ``process_payload`` -- the same function the
Redis consumer calls -- so each test exercises the real transaction boundary:
validate, claim the event log, append to ODS, write the DWD fact, mark the
claim done. The DWS build then runs for real, and the last assertions read the
wide row through ``QueryService`` and ``GET /v1/structured/{symbol}``.

Money must stay a decimal string from ODS (JSONB) to DWD (``NUMERIC``) to DWS
(JSONB strings) to JSON: a value that cannot be represented exactly in
``NUMERIC(p, 4)`` is rejected instead of being silently rounded.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from financecoffer.api import create_app
from financecoffer.core.config import Config
from financecoffer.core.errors import InvalidArgumentError
from financecoffer.db.models.dwd import (
    StockAnnouncement,
    StockFinancialSnapshot,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)
from financecoffer.db.models.dws import StockAgentAnalysisWide
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.models.ops import EtlEventLog
from financecoffer.db.session import Database
from financecoffer.dws import DwsService
from financecoffer.etl.processor import ProcessResult, process_payload
from financecoffer.events import compute_event_id, validate_event
from financecoffer.query.service import QueryService

PROVIDER = "t016-layers"
SYMBOL = "920016"
CRAWL_TIME = "2026-10-27T02:00:00Z"
AS_OF = "2026-10-27"

_DWD_MODELS = (
    StockFinancialSnapshot,
    StockAnnouncement,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)

#: Every money/strike column exposed by the warehouse and its required scale.
MONEY_COLUMNS: dict[tuple[str, str], int] = {
    ("stock_financial_snapshot", "amount_revenue"): 4,
    ("stock_financial_snapshot", "amount_net_profit"): 4,
    ("stock_kline", "open"): 4,
    ("stock_kline", "high"): 4,
    ("stock_kline", "low"): 4,
    ("stock_kline", "close"): 4,
    ("stock_kline", "volume"): 4,
    ("stock_kline", "amount"): 4,
    ("stock_quote_snapshot", "close"): 4,
    ("stock_quote_snapshot", "total_market_cap"): 4,
    ("stock_quote_snapshot", "pe_ttm"): 4,
}


@pytest.fixture()
def db(migrated_warehouse: str) -> Iterator[Database]:
    database = Database(migrated_warehouse)
    _clean(database)
    try:
        yield database
    finally:
        try:
            _clean(database)
        finally:
            database.dispose()


def _clean(database: Database) -> None:
    with database.session_scope() as session:
        session.execute(
            delete(StockAgentAnalysisWide).where(StockAgentAnalysisWide.symbol == SYMBOL)
        )
        for model in _DWD_MODELS:
            session.execute(delete(model).where(model.provider == PROVIDER))
        session.execute(delete(StockRawData).where(StockRawData.symbol == SYMBOL))
        session.execute(delete(EtlEventLog).where(EtlEventLog.provider == PROVIDER))


def payload(
    data_type: str,
    *,
    biz_key: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider=PROVIDER, data_type=data_type, symbol=SYMBOL, biz_key=biz_key
        ),
        "biz_key": biz_key,
        "event_time": "2026-10-27T10:00:00+08:00",
        "data_type": data_type,
        "symbol": SYMBOL,
        "market": "CN",
        "source_info": {"provider": PROVIDER, "crawl_time": CRAWL_TIME},
        "content": content,
        "trace_id": "trace-t016",
    }


def financial(
    *,
    revenue: str = "1500000.0000",
    profit: str = "120000.0000",
    report_date: str = "2026-09-30",
    report_type: str = "Q3",
) -> dict[str, Any]:
    return {
        "quality_metrics": {"quality_status": "normal", "confidence": "high"},
        "structured_fields": {
            "report_date": report_date,
            "report_type": report_type,
            "amount_revenue": revenue,
            "amount_net_profit": profit,
        },
    }


def quote(*, trade_date: str = "2026-10-20") -> dict[str, Any]:
    return {
        "quality_metrics": {"quality_status": "normal"},
        "structured_fields": {
            "trade_date": trade_date,
            "close": "11.1100",
            "total_market_cap": "123456789.0100",
            "pe_ttm": "15.2000",
        },
    }


def announcement() -> dict[str, Any]:
    body = "Q3 revenue grew 12% year over year"
    return {
        "quality_metrics": {"quality_status": "normal"},
        "raw_html": f"<p>{body}</p>",
        "clean_content": body,
        "structured_fields": {
            "title": "Q3 earnings",
            "ann_type": "periodic",
            "publish_date": "2026-10-20",
        },
    }


def risk() -> dict[str, Any]:
    return {
        "quality_metrics": {"quality_status": "suspicious", "confidence": "medium"},
        "structured_fields": {
            "risk_type": "litigation",
            "occur_date": "2026-10-01",
            "severity": "high",
            "summary": "material lawsuit disclosed",
        },
        "source_announcement_ref": "ann-2026-10-20",
    }


def ingest(database: Database, body: dict[str, Any], raw_text: str | None = None) -> ProcessResult:
    """The consumer transaction: claim, land to ODS, write DWD, commit once."""

    text_body = raw_text if raw_text is not None else json.dumps(body, ensure_ascii=False)
    with database.session_scope() as session:
        return process_payload(
            session,
            payload=body,
            raw_text=text_body,
            compliance_enabled=False,
        )


def ods_row(session: Session, event_id: str) -> StockRawData:
    return session.scalars(
        select(StockRawData).where(StockRawData.event_id == event_id)
    ).one()


def year_entry(metrics: dict[str, Any], year: int) -> dict[str, Any]:
    return next(
        item for item in metrics["years"] if str(item["year"]) == str(year)
    )


def build_wide(database: Database, *, fiscal_years: int = 3) -> dict[str, Any]:
    service = DwsService(config=Config(db_url=database.dsn), dsn=database.dsn)
    try:
        report = service.build([SYMBOL], as_of=AS_OF, fiscal_years=fiscal_years)
        assert report.counts["built"] == 1, report.payload()
        return service.show(SYMBOL)
    finally:
        service.close()


def test_one_event_is_visible_at_every_layer(db: Database) -> None:
    financial_body = payload(
        "financial", biz_key="2026-09-30|Q3", content=financial()
    )
    quote_body = payload("quote", biz_key="2026-10-20|close", content=quote())
    financial_text = json.dumps(financial_body, ensure_ascii=False, indent=2)

    financial_result = ingest(db, financial_body, financial_text)
    quote_result = ingest(db, quote_body)

    assert financial_result.status == "written"
    assert financial_result.dwd_table == "dwd.stock_financial_snapshot"
    assert financial_result.dwd_version == 1
    assert quote_result.status == "written"

    # -- ODS: immutable raw payload, pending until the consumer finishes --
    with db.session_scope() as session:
        raw = ods_row(session, financial_body["event_id"])
        assert raw.data_type == "financial"
        assert raw.symbol == SYMBOL
        assert raw.source == "manual"
        assert raw.raw_text == financial_text
        assert raw.raw_json == financial_body
        assert raw.etl_status == "pending"
        assert raw.fetch_time == datetime(2026, 10, 27, 2, 0, tzinfo=UTC)

        claim = session.get(EtlEventLog, financial_body["event_id"])
        assert claim is not None
        assert claim.status == "done"
        assert claim.processed_at is not None

        # -- DWD: one version per business key, decimals untouched --
        facts = list(
            session.scalars(
                select(StockFinancialSnapshot).where(
                    StockFinancialSnapshot.symbol == SYMBOL
                )
            )
        )
        assert [fact.version for fact in facts] == [1]
        fact = facts[0]
        assert fact.event_id == financial_body["event_id"]
        assert fact.amount_revenue == Decimal("1500000.0000")
        assert fact.amount_net_profit == Decimal("120000.0000")
        assert fact.quality_status == "normal"
        assert fact.confidence == "high"
        assert fact.fetch_time == datetime(2026, 10, 27, 2, 0, tzinfo=UTC)

        quote_facts = list(
            session.scalars(
                select(StockQuoteSnapshot).where(StockQuoteSnapshot.symbol == SYMBOL)
            )
        )
        assert [item.version for item in quote_facts] == [1]
        assert quote_facts[0].pe_ttm == Decimal("15.2000")

    # -- DWS: the wide row is composed from the DWD facts --
    wide = build_wide(db)
    assert wide["symbol"] == SYMBOL
    assert wide["fiscal_years"] == 3
    assert wide["source_version"]
    assert wide["as_of"].startswith(AS_OF)

    metrics = wide["metrics_json"]
    assert metrics["window"]["from_year"] == 2024
    assert metrics["window"]["to_year"] == 2026
    entry = year_entry(metrics, 2026)
    assert entry["data_status"] == "present"
    assert entry["report_date"] == "2026-09-30"
    assert entry["report_type"] == "Q3"
    assert entry["version"] == 1
    assert entry["revenue"] == "1500000.0000"
    assert entry["net_profit"] == "120000.0000"
    assert entry["quality_status"] == "normal"
    assert entry["confidence"] == "high"
    assert entry["pe"] == 15.2
    assert entry["pe_reason"] is None
    assert metrics["valuation"]["price_close"] == "11.1100"
    assert metrics["valuation"]["pe_source"] == "quote.pe_ttm"
    assert metrics["gaps"] == [2024, 2025]


def test_a_correction_appends_a_version_and_keeps_both_payloads(db: Database) -> None:
    first = payload(
        "financial", biz_key="2026-09-30|Q3", content=financial(revenue="100.0000")
    )
    second = payload(
        "financial",
        biz_key="2026-09-30|Q3-revised",
        content=financial(revenue="200.0000"),
    )
    first_text = json.dumps(first, ensure_ascii=False)
    second_text = json.dumps(second, ensure_ascii=False)

    ingest(db, first, first_text)
    ingest(db, second, second_text)

    with db.session_scope() as session:
        facts = list(
            session.scalars(
                select(StockFinancialSnapshot)
                .where(StockFinancialSnapshot.symbol == SYMBOL)
                .order_by(StockFinancialSnapshot.version)
            )
        )
        assert [fact.version for fact in facts] == [1, 2]
        assert [fact.amount_revenue for fact in facts] == [
            Decimal("100.0000"),
            Decimal("200.0000"),
        ]
        assert [fact.event_id for fact in facts] == [
            first["event_id"],
            second["event_id"],
        ]

        # ODS is append-only: both payloads survive, the first is untouched.
        ods = list(
            session.scalars(
                select(StockRawData)
                .where(StockRawData.symbol == SYMBOL)
                .order_by(StockRawData.id)
            )
        )
        assert [row.event_id for row in ods] == [first["event_id"], second["event_id"]]
        assert ods[0].raw_text == first_text
        assert ods[0].raw_json["content"]["structured_fields"]["amount_revenue"] == "100.0000"
        assert ods[1].raw_json["content"]["structured_fields"]["amount_revenue"] == "200.0000"

    wide = build_wide(db)
    entry = year_entry(wide["metrics_json"], 2026)
    assert entry["version"] == 2
    assert entry["revenue"] == "200.0000"
    assert entry["net_profit"] == "120000.0000"


def test_announcement_and_risk_reach_the_wide_table(db: Database) -> None:
    ingest(db, payload("financial", biz_key="2026-09-30|Q3", content=financial()))
    ingest(db, payload("quote", biz_key="2026-10-20|close", content=quote()))
    announcement_body = payload(
        "announcement", biz_key="ann-2026-10-20", content=announcement()
    )
    risk_body = payload(
        "risk", biz_key="2026-10-01|litigation|ann-2026-10-20", content=risk()
    )

    announcement_result = ingest(db, announcement_body)
    risk_result = ingest(db, risk_body)

    assert announcement_result.dwd_table == "dwd.stock_announcement"
    assert risk_result.dwd_table == "dwd.stock_risk_event"

    with db.session_scope() as session:
        announcements = list(
            session.scalars(
                select(StockAnnouncement).where(StockAnnouncement.symbol == SYMBOL)
            )
        )
        assert len(announcements) == 1
        assert announcements[0].title == "Q3 earnings"
        assert announcements[0].event_id == announcement_body["event_id"]

        risks = list(
            session.scalars(select(StockRiskEvent).where(StockRiskEvent.symbol == SYMBOL))
        )
        assert len(risks) == 1
        assert risks[0].severity == "high"

    wide = build_wide(db)
    titles = [item["title"] for item in wide["latest_announcements"]]
    assert titles == ["Q3 earnings"]

    summary = wide["risk_summary"]
    assert summary["counts"]["total"] == 1
    assert summary["counts"]["high"] == 1
    assert summary["highest_severity"] == "high"
    assert summary["latest_occur_date"] == "2026-10-01"
    assert summary["highlights"][0]["summary"] == "material lawsuit disclosed"


def test_money_columns_are_numeric_never_float(db: Database) -> None:
    with db.session_scope() as session:
        stored = {
            (row[0], row[1]): (row[2], row[3])
            for row in session.execute(
                text(
                    "SELECT table_name, column_name, data_type, numeric_scale "
                    "FROM information_schema.columns "
                    "WHERE table_schema IN ('dwd', 'dws')"
                )
            ).all()
        }
        floating = session.execute(
            text(
                "SELECT table_schema, table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema IN ('dwd', 'dws') "
                "AND data_type IN ('double precision', 'real', 'money')"
            )
        ).all()

    assert floating == []
    for (table, column), scale in MONEY_COLUMNS.items():
        data_type, numeric_scale = stored[(table, column)]
        assert data_type == "numeric", f"{table}.{column} is {data_type}"
        assert numeric_scale == scale, f"{table}.{column} scale is {numeric_scale}"


def test_a_large_amount_survives_every_layer_without_rounding(db: Database) -> None:
    revenue = "1234567890.1234"
    profit = "-0.0001"
    body = payload(
        "financial",
        biz_key="2026-09-30|Q3",
        content=financial(revenue=revenue, profit=profit),
    )
    raw_text = json.dumps(body, ensure_ascii=False)

    event = validate_event(body)
    assert event.raw["content"]["structured_fields"]["amount_revenue"] == revenue

    result = ingest(db, body, raw_text)
    assert result.status == "written"

    with db.session_scope() as session:
        raw = ods_row(session, body["event_id"])
        assert raw.raw_json["content"]["structured_fields"]["amount_revenue"] == revenue

        fact = session.scalars(
            select(StockFinancialSnapshot).where(
                StockFinancialSnapshot.symbol == SYMBOL
            )
        ).one()
        assert fact.amount_revenue == Decimal(revenue)
        assert str(fact.amount_revenue) == revenue
        assert fact.amount_net_profit == Decimal(profit)

    ingest(db, payload("quote", biz_key="2026-10-20|close", content=quote()))
    wide = build_wide(db)
    entry = year_entry(wide["metrics_json"], 2026)
    assert entry["revenue"] == revenue
    assert entry["net_profit"] == profit

    config = Config(db_url=db.dsn, rate_limit_enabled=False)
    query = QueryService(config=config, dsn=db.dsn)
    queried = query.structured(SYMBOL, metric="revenue")
    assert year_entry({"years": queried.data["years"]}, 2026)["revenue"] == revenue
    assert {str(item["year"]): item["value"] for item in queried.data["series"]}["2026"] == revenue

    client = TestClient(create_app(config=config, dsn=db.dsn))
    response = client.get(f"/v1/structured/{SYMBOL}", params={"metric": "revenue"})
    assert response.status_code == 200, response.text
    payload_body = response.json()
    assert payload_body["ok"] is True
    api_entry = year_entry({"years": payload_body["data"]["years"]}, 2026)
    assert api_entry["revenue"] == revenue
    assert isinstance(api_entry["revenue"], str)
    series = {str(item["year"]): item["value"] for item in payload_body["data"]["series"]}
    assert series["2026"] == revenue


def test_an_amount_beyond_four_decimals_is_rejected_not_rounded(db: Database) -> None:
    body = payload(
        "financial",
        biz_key="2026-09-30|Q3",
        content=financial(revenue="1234567890.123456"),
    )

    # The contract accepts the decimal string; the loss would happen on write.
    event = validate_event(body)
    assert event.content.structured_fields.amount_revenue == Decimal(
        "1234567890.123456"
    )

    with pytest.raises(InvalidArgumentError) as caught:
        ingest(db, body)

    assert caught.value.details is not None
    assert caught.value.details["field"] == "content.structured_fields.amount_revenue"
    assert caught.value.details["numeric"] == "NUMERIC(20, 4)"

    with db.session_scope() as session:
        assert session.scalars(
            select(StockRawData).where(StockRawData.symbol == SYMBOL)
        ).first() is None
        assert session.scalars(
            select(StockFinancialSnapshot).where(
                StockFinancialSnapshot.symbol == SYMBOL
            )
        ).first() is None
        assert session.get(EtlEventLog, body["event_id"]) is None
