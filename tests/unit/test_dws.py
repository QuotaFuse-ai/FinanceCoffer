"""Unit tests for the pure DWS helpers (T-008).

Only functions that need no database live here: the value formatting rules, the
``as_of``/window parsing, the per-year report selection and the PE ladder. The
end-to-end contract is covered by ``tests/integration/test_dws_integration.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from financecoffer.core.errors import ErrorCode, InvalidArgumentError
from financecoffer.db.models.dwd import StockFinancialSnapshot, StockQuoteSnapshot
from financecoffer.dws import (
    BuildReport,
    SymbolResult,
    money,
    parse_as_of,
    rate,
    validate_fiscal_years,
    years_back,
)
from financecoffer.dws.builder import _pe_for_year, _select_years

FETCH = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)


def _financial(
    report_date: str,
    *,
    report_type: str = "FY",
    version: int = 1,
    revenue: str = "1.0000",
    profit: str | None = "1.0000",
    quality: str = "normal",
) -> StockFinancialSnapshot:
    return StockFinancialSnapshot(
        symbol="600000",
        report_date=date.fromisoformat(report_date),
        report_type=report_type,
        version=version,
        amount_revenue=Decimal(revenue),
        amount_net_profit=None if profit is None else Decimal(profit),
        quality_status=quality,
        confidence="high",
        fetch_time=FETCH,
        event_id=f"event:{report_date}:{report_type}:{version}",
        provider="test",
    )


def _quote(
    trade_date: str,
    *,
    close: str | None = "10.0000",
    market_cap: str | None = None,
    pe_ttm: str | None = None,
) -> StockQuoteSnapshot:
    return StockQuoteSnapshot(
        symbol="600000",
        trade_date=date.fromisoformat(trade_date),
        version=1,
        close=None if close is None else Decimal(close),
        total_market_cap=None if market_cap is None else Decimal(market_cap),
        pe_ttm=None if pe_ttm is None else Decimal(pe_ttm),
        quality_status="normal",
        confidence="high",
        fetch_time=FETCH,
        event_id=f"quote:{trade_date}",
        provider="test",
    )


def test_money_renders_fixed_point_strings() -> None:
    assert money(None) is None
    assert money(Decimal("100")) == "100.0000"
    assert money(Decimal("1000000.1234")) == "1000000.1234"
    assert money(Decimal("-0.5")) == "-0.5000"
    assert isinstance(money(Decimal("7")), str)


def test_rate_renders_four_decimal_numbers() -> None:
    assert rate(None) is None
    assert rate(Decimal("2")) == 2.0
    assert rate(Decimal("15.1234")) == 15.1234


def test_parse_as_of_defaults_strips_and_rejects() -> None:
    today = date(2026, 10, 27)
    assert parse_as_of(None, today=today) == today
    assert parse_as_of("   ", today=today) == today
    assert parse_as_of(" 2026-01-15 ", today=today) == date(2026, 1, 15)

    with pytest.raises(InvalidArgumentError) as excinfo:
        parse_as_of("2026-13-01")
    assert excinfo.value.code is ErrorCode.E_INVALID_ARGUMENT
    assert excinfo.value.details == {"field": "as-of", "value": "2026-13-01"}

    with pytest.raises(InvalidArgumentError):
        parse_as_of("2026/10/27")


def test_validate_fiscal_years_accepts_the_ddl_range_only() -> None:
    assert validate_fiscal_years(1) == 1
    assert validate_fiscal_years(10) == 10

    for bad in (0, 11):
        with pytest.raises(InvalidArgumentError) as excinfo:
            validate_fiscal_years(bad)
        assert excinfo.value.details == {"field": "years", "value": bad}


def test_years_back_handles_leap_days() -> None:
    assert years_back(date(2026, 10, 27), 3) == date(2023, 10, 27)
    assert years_back(date(2024, 2, 29), 1) == date(2023, 2, 28)


def test_select_years_prefers_the_newest_annual_report() -> None:
    rows = [
        _financial("2025-12-31", report_type="FY", version=2, revenue="200.0000"),
        _financial("2025-12-31", report_type="FY", version=1, revenue="100.0000"),
        _financial("2025-09-30", report_type="Q3", revenue="50.0000"),
        _financial("2023-12-31", revenue="10.0000"),
    ]

    selections, missing = _select_years(rows, day=date(2025, 12, 31), fiscal_years=3)

    assert [item.year for item in selections] == [2025, 2024, 2023]
    assert selections[0].row is not None
    assert selections[0].row.version == 2
    assert selections[0].row.amount_revenue == Decimal("200.0000")
    assert selections[0].basis == "annual"
    assert selections[0].ambiguous is False
    assert selections[1].row is None
    assert selections[2].basis == "annual"
    assert missing == [2024]


def test_select_years_falls_back_to_a_period_report() -> None:
    rows = [_financial("2025-09-30", report_type="Q3", revenue="50.0000")]

    selections, _ = _select_years(rows, day=date(2025, 12, 31), fiscal_years=1)

    assert selections[0].row is not None
    assert selections[0].basis == "period"
    assert selections[0].ambiguous is False


def test_select_years_flags_two_period_types_for_one_year() -> None:
    rows = [
        _financial("2025-09-30", report_type="Q3"),
        _financial("2025-09-30", report_type="Q1-Q3"),
    ]

    selections, _ = _select_years(rows, day=date(2025, 12, 31), fiscal_years=1)

    assert selections[0].row is not None
    assert selections[0].row.report_type == "Q3"
    assert selections[0].ambiguous is True


def test_pe_ladder() -> None:
    report_date = date(2024, 12, 31)
    profit = Decimal("100000.0000")

    assert _pe_for_year(None, report_date, profit) == (None, "no_price", None)
    assert _pe_for_year(_quote("2024-12-31", close=None), report_date, profit) == (
        None,
        "no_price",
        None,
    )
    assert _pe_for_year(_quote("2024-12-31", pe_ttm="15.1234"), report_date, profit) == (
        15.1234,
        None,
        "quote.pe_ttm",
    )
    assert _pe_for_year(
        _quote("2024-12-31", market_cap="2000000.0000"), report_date, profit
    ) == (20.0, None, "market_cap/net_profit")
    assert _pe_for_year(
        _quote("2024-12-31", pe_ttm="-3.0000"), report_date, profit
    ) == (None, "no_price", None)
    assert _pe_for_year(
        _quote("2024-12-31", market_cap="2000000.0000"), report_date, None
    ) == (None, "no_earnings", None)
    assert _pe_for_year(
        _quote("2024-12-31", market_cap="2000000.0000"),
        report_date,
        Decimal("-1000.0000"),
    ) == (None, "non_positive_earnings", None)
    # A quote more than one year away cannot price this report.
    assert _pe_for_year(
        _quote("2026-10-27", market_cap="2000000.0000"), report_date, profit
    ) == (None, "no_price", None)


def test_build_report_counts_and_payload() -> None:
    report = BuildReport(
        as_of=date(2026, 10, 27),
        fiscal_years=5,
        results=(
            SymbolResult("600000", "built"),
            SymbolResult("600001", "skipped", "no_financial_data"),
            SymbolResult("600002", "failed", "compliance_mismatch"),
        ),
    )

    assert report.counts == {"built": 1, "skipped": 1, "failed": 1}
    assert report.failed is True
    assert report.payload() == {
        "as_of": "2026-10-27",
        "fiscal_years": 5,
        "counts": {"built": 1, "skipped": 1, "failed": 1},
        "built": ["600000"],
        "skipped": [{"symbol": "600001", "reason": "no_financial_data"}],
        "failed": [{"symbol": "600002", "reason": "compliance_mismatch"}],
    }
