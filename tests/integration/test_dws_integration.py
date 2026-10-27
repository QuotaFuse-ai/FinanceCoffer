"""DWS wide-table build against a migrated PostgreSQL (T-008).

Facts are seeded through the DWD repositories (``insert_version`` /
``insert_once``), exactly the rows the T-006 write path would leave behind. The
builder then runs for real, and each test asserts one block of
``docs/tasks/TASK-008/dws-metrics-spec.md`` section 9.

Every test uses its own ``81xxxx`` symbols so the shared scratch database stays
isolated from the DWD integration suite (which uses ``900xxx``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from financecoffer.cli import app
from financecoffer.core.config import Config
from financecoffer.core.errors import ErrorCode, NotFoundError
from financecoffer.db.models.dwd import StockFinancialSnapshot
from financecoffer.db.models.dws import StockAgentAnalysisWide
from financecoffer.db.repositories.dwd import (
    AnnouncementRepository,
    FinancialSnapshotRepository,
    QuoteSnapshotRepository,
    RiskEventRepository,
)
from financecoffer.db.session import Database
from financecoffer.dws import DwsService

AS_OF = "2026-10-27"
PROVIDER = "t008-dws"
FETCH = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)

runner = CliRunner()


@pytest.fixture()
def db(migrated_warehouse: str) -> Iterator[Database]:
    database = Database(migrated_warehouse)
    try:
        yield database
    finally:
        database.dispose()


def _event_id(kind: str, symbol: str, marker: str) -> str:
    return f"{PROVIDER}:{kind}:{symbol}:{marker}"


def _hash(marker: str) -> str:
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()


def seed_financial(
    db: Database,
    symbol: str,
    *,
    report_date: str,
    report_type: str = "FY",
    revenue: Decimal | None = Decimal("1000000.0000"),
    profit: Decimal | None = Decimal("100000.0000"),
    quality: str = "normal",
    confidence: str | None = "high",
    tier: str | None = None,
    commercial: bool | None = None,
) -> None:
    with db.session_scope() as session:
        FinancialSnapshotRepository(session).insert_version(
            symbol=symbol,
            report_date=date.fromisoformat(report_date),
            report_type=report_type,
            amount_revenue=revenue,
            amount_net_profit=profit,
            quality_status=quality,
            confidence=confidence,
            fetch_time=FETCH,
            event_id=_event_id(
                "fin",
                symbol,
                f"{report_date}:{report_type}:{quality}:{confidence}:{revenue}:{profit}",
            ),
            provider=PROVIDER,
            compliance_tier=tier,
            commercial_use=commercial,
        )


def seed_quote(
    db: Database,
    symbol: str,
    *,
    trade_date: str,
    close: Decimal | None = Decimal("10.0000"),
    market_cap: Decimal | None = None,
    pe_ttm: Decimal | None = None,
    tier: str | None = None,
    commercial: bool | None = None,
) -> None:
    with db.session_scope() as session:
        QuoteSnapshotRepository(session).insert_version(
            symbol=symbol,
            trade_date=date.fromisoformat(trade_date),
            close=close,
            total_market_cap=market_cap,
            pe_ttm=pe_ttm,
            quality_status="normal",
            confidence="high",
            fetch_time=FETCH,
            event_id=_event_id("quote", symbol, trade_date),
            provider=PROVIDER,
            compliance_tier=tier,
            commercial_use=commercial,
        )


def seed_announcement(
    db: Database,
    symbol: str,
    *,
    ann_date: str,
    title: str = "quarterly report",
    ann_type: str = "periodic",
    quality: str = "normal",
    minio_url: str | None = "s3://financecoffer/ann/x.pdf",
    tier: str | None = None,
    commercial: bool | None = None,
) -> int:
    marker = f"{symbol}:{ann_date}:{title}:{ann_type}"
    with db.session_scope() as session:
        row = AnnouncementRepository(session).insert_once(
            symbol=symbol,
            ann_date=date.fromisoformat(ann_date),
            ann_type=ann_type,
            title=title,
            content_hash=_hash(marker),
            clean_content="body that must never reach the wide table",
            minio_url=minio_url,
            quality_status=quality,
            confidence="high",
            fetch_time=FETCH,
            event_id=_event_id("ann", symbol, _hash(marker)),
            provider=PROVIDER,
            compliance_tier=tier,
            commercial_use=commercial,
        )
        assert row is not None
        return row.id


def seed_risk(
    db: Database,
    symbol: str,
    *,
    occur_date: str,
    severity: str,
    risk_type: str = "litigation",
    summary: str | None = "a risk event",
    quality: str = "normal",
    index: int = 0,
    tier: str | None = None,
    commercial: bool | None = None,
) -> int:
    marker = f"{occur_date}:{severity}:{index}"
    with db.session_scope() as session:
        row = RiskEventRepository(session).insert_once(
            symbol=symbol,
            occur_date=date.fromisoformat(occur_date),
            risk_type=risk_type,
            severity=severity,
            summary=summary,
            source_announcement_ref=f"dwd:stock_announcement:{index}",
            quality_status=quality,
            confidence="medium",
            fetch_time=FETCH,
            event_id=_event_id("risk", symbol, marker),
            provider=PROVIDER,
            compliance_tier=tier,
            commercial_use=commercial,
        )
        assert row is not None
        return row.id


def service(db: Database, *, compliance: bool = False) -> DwsService:
    return DwsService(
        config=Config(db_url=db.dsn, compliance_enabled=compliance),
        dsn=db.dsn,
    )


def wide_row(db: Database, symbol: str) -> StockAgentAnalysisWide | None:
    with db.session() as session:
        return session.get(StockAgentAnalysisWide, symbol)


def jsonb_md5s(db: Database, symbol: str) -> tuple[str, str, str, datetime]:
    statement = text(
        "SELECT md5(metrics_json::text), md5(risk_summary::text), "
        "md5(latest_announcements::text), built_at "
        "FROM dws.stock_agent_analysis_wide WHERE symbol = :symbol"
    )
    with db.session() as session:
        return session.execute(statement, {"symbol": symbol}).one()  # type: ignore[return-value]


def year_record(metrics: dict, year: int) -> dict:
    for record in metrics["years"]:
        if record["year"] == year:
            return record
    raise AssertionError(f"year {year} missing from {[r['year'] for r in metrics['years']]}")


def test_five_year_series(db: Database) -> None:
    symbol = "810001"
    seed_financial(db, symbol, report_date="2022-12-31", revenue=Decimal("1000000.1234"))
    seed_financial(
        db, symbol, report_date="2023-12-31", quality="suspicious", confidence="medium"
    )
    # 2024 intentionally has no report.
    seed_financial(db, symbol, report_date="2025-12-31", quality="bad", confidence="low")
    seed_financial(
        db,
        symbol,
        report_date="2026-09-30",
        report_type="Q3",
        revenue=Decimal("1400000.0000"),
        profit=Decimal("140000.0000"),
    )

    with service(db) as svc:
        report = svc.build([symbol], as_of=AS_OF)

    assert report.counts == {"built": 1, "skipped": 0, "failed": 0}
    row = wide_row(db, symbol)
    assert row is not None
    assert row.as_of.astimezone(UTC) == datetime(2026, 10, 27, tzinfo=UTC)
    assert row.fiscal_years == 5
    assert row.source_version == "dws-1"

    metrics = row.metrics_json
    assert metrics["schema"] == "dws.metrics.v1"
    assert metrics["basis"] == "annual"
    assert metrics["window"] == {
        "from_year": 2022,
        "to_year": 2026,
        "fiscal_years": 5,
        "as_of": AS_OF,
    }
    assert [record["year"] for record in metrics["years"]] == [2026, 2025, 2024, 2023, 2022]
    assert metrics["gaps"] == [2024]
    assert metrics["quality"] == {
        "bad_years": [2025],
        "suspicious_years": [2023],
        "missing_years": [2024],
    }

    missing = year_record(metrics, 2024)
    assert missing["data_status"] == "missing"
    assert all(
        missing[name] is None
        for name in ("revenue", "net_profit", "roe", "pe", "pb", "report_date")
    )

    y2022 = year_record(metrics, 2022)
    assert y2022["revenue"] == "1000000.1234"
    assert isinstance(y2022["revenue"], str)
    assert y2022["net_profit"] == "100000.0000"
    assert y2022["report_type"] == "FY"
    assert y2022["report_basis"] == "annual"
    assert y2022["version"] == 1
    assert y2022["roe"] is None and y2022["roe_reason"] == "equity_unavailable"
    assert y2022["pb"] is None and y2022["pb_reason"] == "no_equity"

    y2025 = year_record(metrics, 2025)
    assert y2025["quality_status"] == "bad"
    assert y2025["confidence"] == "low"
    assert y2025["revenue"] == "1000000.0000"

    y2023 = year_record(metrics, 2023)
    assert y2023["quality_status"] == "suspicious"

    y2026 = year_record(metrics, 2026)
    assert y2026["report_basis"] == "period"
    assert y2026["report_type"] == "Q3"
    assert y2026["revenue"] == "1400000.0000"

    assert metrics["valuation"] == {
        "as_of": AS_OF,
        "price_date": None,
        "price_close": None,
        "pe_source": None,
        "pe": None,
        "pe_reason": "no_price",
        "pb": None,
        "pb_reason": "no_equity",
    }
    for year in (2022, 2023, 2025, 2026):
        assert year_record(metrics, year)["pe_reason"] == "no_price"

    assert row.risk_summary["counts"] == {
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "total": 0,
    }
    assert row.risk_summary["highlights"] == []
    assert row.risk_summary["highest_severity"] is None
    assert row.risk_summary["text"].startswith("近 3 年共 0 条风险事件")
    assert row.latest_announcements == []


def test_only_latest_version_and_annual_report_wins(db: Database) -> None:
    symbol = "810002"
    seed_financial(
        db, symbol, report_date="2025-12-31", report_type="FY", revenue=Decimal("100.0000")
    )
    seed_financial(
        db, symbol, report_date="2025-12-31", report_type="FY", revenue=Decimal("200.0000")
    )
    seed_financial(
        db,
        symbol,
        report_date="2025-09-30",
        report_type="Q3",
        revenue=Decimal("50.0000"),
        profit=Decimal("5.0000"),
    )

    with service(db) as svc:
        report = svc.build([symbol], as_of="2026-01-15")

    assert report.counts["built"] == 1
    row = wide_row(db, symbol)
    assert row is not None
    years = [record for record in row.metrics_json["years"] if record["year"] == 2025]
    assert len(years) == 1
    picked = years[0]
    assert picked["report_date"] == "2025-12-31"
    assert picked["report_type"] == "FY"
    assert picked["version"] == 2
    assert picked["report_basis"] == "annual"
    assert picked["revenue"] == "200.0000"


def test_period_reports_are_not_annualized_and_flag_ambiguity(db: Database) -> None:
    symbol = "810023"
    seed_financial(db, symbol, report_date="2025-09-30", report_type="Q3")
    seed_financial(db, symbol, report_date="2025-09-30", report_type="Q1-Q3")

    with service(db) as svc:
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1

    row = wide_row(db, symbol)
    assert row is not None
    record = year_record(row.metrics_json, 2025)
    assert record["report_basis"] == "period"
    assert record["revenue"] == "1000000.0000"
    assert row.metrics_json["quality"]["ambiguous_report"] is True


def test_pe_ladder(db: Database) -> None:
    """pe_ttm passthrough, market-cap fallback, and every null reason."""
    symbols = ["810003", "810004", "810005", "810006", "810007", "810008", "810009"]
    for symbol in symbols:
        profit: Decimal | None = Decimal("100000.0000")
        if symbol == "810007":
            profit = Decimal("-1000.0000")
        elif symbol == "810008":
            profit = None
        seed_financial(
            db,
            symbol,
            report_date="2022-12-31" if symbol == "810009" else "2025-12-31",
            profit=profit,
        )

    seed_quote(db, "810003", trade_date="2025-12-31", pe_ttm=Decimal("15.1234"))
    seed_quote(
        db,
        "810004",
        trade_date="2025-12-31",
        market_cap=Decimal("2000000.0000"),
    )
    seed_quote(
        db,
        "810005",
        trade_date="2025-12-31",
        close=Decimal("10.0000"),
        market_cap=None,
        pe_ttm=Decimal("-3.0000"),
    )
    seed_quote(
        db,
        "810007",
        trade_date="2025-12-31",
        market_cap=Decimal("2000000.0000"),
    )
    seed_quote(
        db,
        "810008",
        trade_date="2025-12-31",
        market_cap=Decimal("2000000.0000"),
    )
    seed_quote(db, "810009", trade_date="2025-12-31", pe_ttm=Decimal("9.0000"))

    with service(db) as svc:
        report = svc.build(symbols, as_of=AS_OF)
    assert report.counts["built"] == len(symbols)

    def pe(symbol: str) -> tuple:
        row = wide_row(db, symbol)
        assert row is not None
        record = year_record(row.metrics_json, 2022 if symbol == "810009" else 2025)
        return (
            record["pe"],
            record["pe_reason"],
            row.metrics_json["valuation"]["pe_source"],
            row.metrics_json["valuation"]["price_date"],
        )

    assert pe("810003") == (15.1234, None, "quote.pe_ttm", "2025-12-31")
    assert pe("810004") == (20.0, None, "market_cap/net_profit", "2025-12-31")
    assert pe("810005") == (None, "no_price", None, "2025-12-31")
    assert pe("810006") == (None, "no_price", None, None)
    assert pe("810007") == (None, "non_positive_earnings", None, "2025-12-31")
    assert pe("810008") == (None, "no_earnings", None, "2025-12-31")
    assert pe("810009") == (None, "no_price", None, "2025-12-31")

    row = wide_row(db, "810003")
    assert row is not None
    assert row.metrics_json["valuation"]["price_close"] == "10.0000"
    assert row.metrics_json["valuation"]["pe"] == 15.1234
    assert row.metrics_json["valuation"]["pb"] is None
    assert row.metrics_json["valuation"]["pb_reason"] == "no_equity"


def test_risk_summary_window_counts_and_highlights(db: Database) -> None:
    symbol = "810010"
    seed_financial(db, symbol, report_date="2025-12-31")
    seed_risk(
        db,
        symbol,
        occur_date="2026-10-01",
        severity="critical",
        risk_type="goodwill_impairment",
        summary=None,
    )
    for offset in range(11):
        seed_risk(
            db,
            symbol,
            occur_date=(date(2026, 9, 1) + timedelta(days=offset)).isoformat(),
            severity="high",
            index=offset,
        )
    for offset in range(2):
        seed_risk(
            db,
            symbol,
            occur_date=(date(2025, 5, 1) + timedelta(days=offset)).isoformat(),
            severity="medium",
            risk_type="litigation",
            index=offset,
        )
    seed_risk(db, symbol, occur_date="2024-03-01", severity="low", index=99)
    # Left-open boundary: exactly as_of - 3 years is out of the window.
    seed_risk(db, symbol, occur_date="2023-10-27", severity="critical", index=100)
    # Future events relative to as_of are out too.
    seed_risk(db, symbol, occur_date="2026-10-28", severity="critical", index=101)

    with service(db) as svc:
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1

    row = wide_row(db, symbol)
    assert row is not None
    summary = row.risk_summary
    assert summary["window"] == {"since": "2023-10-27", "until": AS_OF}
    assert summary["as_of"] == AS_OF
    assert summary["counts"] == {
        "critical": 1,
        "high": 11,
        "medium": 2,
        "low": 1,
        "total": 15,
    }
    assert summary["highest_severity"] == "critical"
    assert summary["latest_occur_date"] == "2026-10-01"
    assert len(summary["highlights"]) == 10
    assert summary["highlights"][0]["event_id"].endswith("2026-10-01:critical:0")
    assert summary["highlights"][0]["summary"] == "goodwill_impairment（未提供摘要）"
    assert {item["severity"] for item in summary["highlights"]} == {"critical", "high"}
    assert summary["truncated"] is False
    assert summary["text"] == (
        "近 3 年共 15 条风险事件：critical 1 条、high 11 条、medium 2 条、low 1 条。"
        "最近一次为 2026-10-01 的 goodwill_impairment（critical）："
        "goodwill_impairment（未提供摘要）。"
    )


def test_announcements_top_twenty_ordered_and_whitelisted(db: Database) -> None:
    symbol = "810011"
    seed_financial(db, symbol, report_date="2025-12-31")
    ids: list[int] = []
    for offset in range(20):
        ids.append(
            seed_announcement(
                db, symbol, ann_date=(date(2026, 10, 1) + timedelta(days=offset)).isoformat()
            )
        )
    long_title = "T" * 201
    # Same ann_date, so id DESC decides: the later insert has the larger id.
    older = seed_announcement(db, symbol, ann_date="2026-10-21", title="same day")
    newer = seed_announcement(
        db, symbol, ann_date="2026-10-21", title=long_title, quality="bad"
    )
    seed_announcement(db, symbol, ann_date="2026-11-01", title="future announcement")

    with service(db) as svc:
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1

    row = wide_row(db, symbol)
    assert row is not None
    payload = row.latest_announcements
    assert len(payload) == 20
    assert payload[0]["id"] == newer
    assert payload[1]["id"] == older
    assert (payload[0]["ann_date"], payload[0]["id"]) > (payload[1]["ann_date"], payload[1]["id"])
    orders = [(item["ann_date"], item["id"]) for item in payload]
    assert orders == sorted(orders, reverse=True)
    assert payload[0]["quality_status"] == "bad"
    assert payload[0]["title"] == "T" * 199 + "…"
    assert payload[0]["title_truncated"] is True
    assert set(payload[1]) == {
        "id",
        "ann_date",
        "ann_type",
        "title",
        "minio_url",
        "quality_status",
        "confidence",
    }
    assert "clean_content" not in json.dumps(payload)
    assert ids[0] not in {item["id"] for item in payload}
    assert all(item["ann_date"] != "2026-11-01" for item in payload)


def test_two_builds_produce_identical_jsonb(db: Database) -> None:
    symbol = "810012"
    seed_financial(db, symbol, report_date="2025-12-31")
    seed_quote(db, symbol, trade_date="2025-12-31", pe_ttm=Decimal("12.5000"))
    seed_announcement(db, symbol, ann_date="2026-10-20")
    seed_risk(db, symbol, occur_date="2026-09-01", severity="high")

    with service(db) as svc:
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1
        first = jsonb_md5s(db, symbol)
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1
        second = jsonb_md5s(db, symbol)

    assert first[:3] == second[:3]
    assert first[3] != second[3]


def test_compliance_switch_off_writes_null_columns(db: Database) -> None:
    symbol = "810013"
    seed_financial(db, symbol, report_date="2025-12-31")

    with service(db) as svc:
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1
        shown = svc.show(symbol)

    row = wide_row(db, symbol)
    assert row is not None
    assert row.compliance_tier is None
    assert row.commercial_use is None
    assert "compliance" not in shown


def test_compliance_switch_on_passes_through_consistent_tier(db: Database) -> None:
    symbol = "810014"
    seed_financial(db, symbol, report_date="2025-12-31", tier="S", commercial=True)
    seed_quote(db, symbol, trade_date="2025-12-31", tier="S", commercial=True)
    seed_announcement(db, symbol, ann_date="2026-10-20", tier="S", commercial=True)
    seed_risk(db, symbol, occur_date="2026-09-01", severity="high", tier="S", commercial=True)

    with service(db, compliance=True) as svc:
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1
        shown = svc.show(symbol)

    row = wide_row(db, symbol)
    assert row is not None
    assert row.compliance_tier == "S"
    assert row.commercial_use is True
    assert shown["compliance"] == {"tier": "S", "commercial_use": True}


def test_compliance_mismatch_and_missing_fail_closed(db: Database) -> None:
    mismatch = "810015"
    seed_financial(db, mismatch, report_date="2025-12-31", tier="S", commercial=True)
    seed_announcement(
        db, mismatch, ann_date="2026-10-20", tier="B", commercial=False
    )

    missing = "810016"
    seed_financial(db, missing, report_date="2025-12-31")

    with service(db, compliance=True) as svc:
        report = svc.build([mismatch, missing], as_of=AS_OF)

    assert report.counts == {"built": 0, "skipped": 0, "failed": 2}
    reasons = {item.symbol: item.reason for item in report.results}
    assert reasons == {mismatch: "compliance_mismatch", missing: "compliance_missing"}
    assert wide_row(db, mismatch) is None
    assert wide_row(db, missing) is None


def test_one_failure_does_not_block_other_symbols(db: Database) -> None:
    good, broken, empty = "810027", "810028", "810029"
    seed_financial(db, good, report_date="2025-12-31", tier="S", commercial=True)
    seed_financial(db, broken, report_date="2025-12-31", tier="S", commercial=True)
    seed_announcement(db, broken, ann_date="2026-10-20", tier="B", commercial=False)

    with service(db, compliance=True) as svc:
        report = svc.build([good, broken, empty], as_of=AS_OF)

    assert report.counts == {"built": 1, "skipped": 1, "failed": 1}
    payload = report.payload()
    assert payload["built"] == [good]
    assert payload["skipped"] == [{"symbol": empty, "reason": "no_financial_data"}]
    assert payload["failed"] == [{"symbol": broken, "reason": "compliance_mismatch"}]
    assert wide_row(db, good) is not None
    assert wide_row(db, broken) is None


def test_older_as_of_does_not_overwrite_newer_snapshot(db: Database) -> None:
    symbol = "810020"
    seed_financial(db, symbol, report_date="2025-12-31")

    with service(db) as svc:
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1
        stale = svc.build([symbol], as_of="2026-10-26")
        assert stale.counts == {"built": 0, "skipped": 1, "failed": 0}
        assert stale.results[0].reason == "stale_as_of"
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1

    row = wide_row(db, symbol)
    assert row is not None
    assert row.as_of.astimezone(UTC) == datetime(2026, 10, 27, tzinfo=UTC)


def test_show_matches_the_stored_row_and_rejects_unknown_symbols(db: Database) -> None:
    symbol = "810021"
    seed_financial(db, symbol, report_date="2025-12-31")
    seed_announcement(db, symbol, ann_date="2026-10-20")

    with service(db) as svc:
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1
        shown = svc.show(symbol)
        with pytest.raises(NotFoundError) as missing:
            svc.show("810099")

    row = wide_row(db, symbol)
    assert row is not None
    assert shown["symbol"] == symbol
    assert shown["metrics_json"] == row.metrics_json
    assert shown["risk_summary"] == row.risk_summary
    assert shown["latest_announcements"] == row.latest_announcements
    assert shown["fiscal_years"] == row.fiscal_years
    assert shown["source_version"] == "dws-1"
    assert missing.value.code is ErrorCode.E_NOT_FOUND


def test_cli_build_show_and_argument_errors(db: Database) -> None:
    symbol = "810022"
    seed_financial(db, symbol, report_date="2025-12-31")

    build = runner.invoke(
        app, ["--db-url", db.dsn, "dws", "build", "--symbol", symbol, "--as-of", AS_OF]
    )
    assert build.exit_code == 0, build.output
    payload = json.loads(build.stdout)
    assert payload["ok"] is True
    assert payload["data"]["counts"] == {"built": 1, "skipped": 0, "failed": 0}
    assert payload["data"]["as_of"] == AS_OF
    assert payload["meta"]["source"] == "dws"

    shown = runner.invoke(app, ["--db-url", db.dsn, "dws", "show", symbol])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["data"]["symbol"] == symbol

    unknown = runner.invoke(app, ["--db-url", db.dsn, "dws", "show", "810099"])
    assert unknown.exit_code == 1
    assert json.loads(unknown.stdout)["error"]["code"] == "E_NOT_FOUND"

    neither = runner.invoke(app, ["--db-url", db.dsn, "dws", "build"])
    assert neither.exit_code == 2
    assert json.loads(neither.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"

    both = runner.invoke(app, ["--db-url", db.dsn, "dws", "build", "--all", "--symbol", symbol])
    assert both.exit_code == 2
    assert json.loads(both.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"

    bad_date = runner.invoke(
        app, ["--db-url", db.dsn, "dws", "build", "--symbol", symbol, "--as-of", "2026-13-01"]
    )
    assert bad_date.exit_code == 2
    assert json.loads(bad_date.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"

    rebuild = runner.invoke(app, ["--db-url", db.dsn, "dws", "rebuild", "--as-of", AS_OF])
    assert rebuild.exit_code == 0, rebuild.output
    assert json.loads(rebuild.stdout)["data"]["counts"]["failed"] == 0


def test_cli_partial_failure_exits_eight(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    symbol = "810031"
    seed_financial(db, symbol, report_date="2025-12-31", tier="S", commercial=True)
    seed_announcement(db, symbol, ann_date="2026-10-20", tier="B", commercial=False)
    monkeypatch.setenv("FC_COMPLIANCE_ENABLED", "true")

    result = runner.invoke(
        app, ["--db-url", db.dsn, "dws", "build", "--symbol", symbol, "--as-of", AS_OF]
    )

    assert result.exit_code == 8
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_PARTIAL_FAILURE"
    assert payload["error"]["details"]["counts"] == {"built": 0, "skipped": 0, "failed": 1}
    assert payload["error"]["details"]["failed"] == [
        {"symbol": symbol, "reason": "compliance_mismatch"}
    ]


def test_an_unknown_symbol_with_no_financial_rows_is_skipped(db: Database) -> None:
    with service(db) as svc:
        report = svc.build(["810024"], as_of=AS_OF)

    assert report.counts == {"built": 0, "skipped": 1, "failed": 0}
    assert report.results[0].reason == "no_financial_data"
    assert wide_row(db, "810024") is None


def test_build_all_targets_every_symbol_in_dwd(db: Database) -> None:
    symbol = "810030"
    seed_financial(db, symbol, report_date="2025-12-31")

    with db.session() as session:
        symbols = set(session.scalars(select(StockFinancialSnapshot.symbol).distinct()).all())
    assert symbol in symbols

    with service(db) as svc:
        report = svc.build(build_all=True, as_of=AS_OF)

    assert report.counts["failed"] == 0
    assert symbol in report.payload()["built"]
    for built in report.payload()["built"]:
        assert wide_row(db, built) is not None


def test_dws_models_and_dwd_rows_are_the_only_tables_touched(db: Database) -> None:
    """The builder must never write a DWD row."""
    symbol = "810025"
    seed_financial(db, symbol, report_date="2025-12-31")
    with db.session() as session:
        before = session.execute(
            text(
                "SELECT count(*) FROM dwd.stock_financial_snapshot "
                "UNION ALL SELECT count(*) FROM dwd.stock_announcement "
                "UNION ALL SELECT count(*) FROM dwd.stock_risk_event "
                "UNION ALL SELECT count(*) FROM dwd.stock_quote_snapshot"
            )
        ).scalars().all()

    with service(db) as svc:
        assert svc.build([symbol], as_of=AS_OF).counts["built"] == 1

    with db.session() as session:
        after = session.execute(
            text(
                "SELECT count(*) FROM dwd.stock_financial_snapshot "
                "UNION ALL SELECT count(*) FROM dwd.stock_announcement "
                "UNION ALL SELECT count(*) FROM dwd.stock_risk_event "
                "UNION ALL SELECT count(*) FROM dwd.stock_quote_snapshot"
            )
        ).scalars().all()
    assert before == after
