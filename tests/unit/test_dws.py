"""Unit tests for the pure DWS helpers (T-008).

Only functions that need no database live here: the value formatting rules, the
``as_of``/window parsing, the per-year report selection and the PE ladder. The
end-to-end contract is covered by ``tests/integration/test_dws_integration.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from financecoffer.core.config import Config
from financecoffer.core.errors import ErrorCode, InvalidArgumentError
from financecoffer.db.models.dwd import StockFinancialSnapshot, StockQuoteSnapshot
from financecoffer.dws import (
    BuildReport,
    DwsService,
    SymbolResult,
    money,
    parse_as_of,
    rate,
    resolve_default_basis,
    validate_fiscal_years,
    years_back,
)
from financecoffer.dws.builder import (
    NON_A_SHARE_REASON,
    _latest_deducted_quarter,
    _pe_for_year,
    _reference_valuation,
    _select_years,
    is_a_share_symbol,
)

FETCH = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)


def _financial(
    report_date: str,
    *,
    report_type: str = "FY",
    version: int = 1,
    revenue: str = "1.0000",
    profit: str | None = "1.0000",
    deducted: str | None = None,
    quality: str = "normal",
) -> StockFinancialSnapshot:
    return StockFinancialSnapshot(
        symbol="600000",
        report_date=date.fromisoformat(report_date),
        report_type=report_type,
        version=version,
        amount_revenue=Decimal(revenue),
        amount_net_profit=None if profit is None else Decimal(profit),
        amount_net_profit_deducted=None if deducted is None else Decimal(deducted),
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
    pb: str | None = None,
) -> StockQuoteSnapshot:
    return StockQuoteSnapshot(
        symbol="600000",
        trade_date=date.fromisoformat(trade_date),
        version=1,
        close=None if close is None else Decimal(close),
        total_market_cap=None if market_cap is None else Decimal(market_cap),
        pe_ttm=None if pe_ttm is None else Decimal(pe_ttm),
        pb_mrq=None if pb is None else Decimal(pb),
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


def test_latest_deducted_quarter_takes_q1_cumulative_as_is() -> None:
    rows = [_financial("2025-03-31", report_type="Q1", deducted="30000.0000")]

    quarter, reason, report = _latest_deducted_quarter(rows)

    assert (quarter, reason) == (Decimal("30000.0000"), None)
    assert report is not None and report.report_type == "Q1"


def test_latest_deducted_quarter_subtracts_the_previous_cumulative() -> None:
    rows = [
        _financial("2025-06-30", report_type="HY", deducted="60000.0000"),
        _financial("2025-09-30", report_type="Q3", deducted="110000.0000"),
    ]

    quarter, reason, report = _latest_deducted_quarter(rows)

    assert (quarter, reason) == (Decimal("50000.0000"), None)
    assert report is not None and report.report_type == "Q3"


def test_latest_deducted_quarter_prefers_the_newest_revision() -> None:
    rows = [
        _financial("2024-12-31", deducted="100000.0000", version=1),
        _financial("2024-12-31", deducted="120000.0000", version=2),
        _financial("2025-03-31", report_type="Q1", deducted="30000.0000"),
    ]

    # A restated prior year does not leak into the next fiscal year.
    quarter, reason, _ = _latest_deducted_quarter(rows)
    assert (quarter, reason) == (Decimal("30000.0000"), None)


def test_latest_deducted_quarter_reports_missing_inputs_without_guessing() -> None:
    assert _latest_deducted_quarter([_financial("2025-03-31")]) == (
        None,
        "no_deducted_earnings",
        None,
    )
    quarter, reason, report = _latest_deducted_quarter(
        [_financial("2025-09-30", report_type="Q3", deducted="110000.0000")]
    )
    assert (quarter, reason) == (None, "no_prior_cumulative")
    assert report is not None
    quarter, reason, report = _latest_deducted_quarter(
        [_financial("2025-02-28", report_type="Q4", deducted="110000.0000")]
    )
    assert (quarter, reason) == (None, "unsupported_report_period")
    assert report is not None


def test_reference_valuation_annualizes_the_deducted_quarter() -> None:
    report = _financial("2025-09-30", report_type="Q3", deducted="110000.0000")
    quote = _quote(
        "2025-10-10",
        market_cap="2000000.0000",
        pe_ttm="15.1234",
        pb="1.5000",
    )

    basis = _reference_valuation(quote, Decimal("50000.0000"), None, report)

    assert basis["pe"] == 10.0  # 2,000,000 / (50,000 x 4)
    assert basis["pe_source"] == "deducted_annualized"
    assert basis["pe_reason"] is None
    assert basis["pe_cross"] == 15.1234
    assert basis["pe_cross_source"] == "quote.pe_ttm"
    assert basis["pb"] == 1.5
    assert basis["pb_reason"] is None
    assert basis["roe"] is None
    assert basis["roe_reason"] == "equity_unavailable"
    assert basis["report_date"] == "2025-09-30"
    assert basis["report_type"] == "Q3"
    assert basis["quarter_basis"] == "single_quarter_deducted_x4"


def test_reference_valuation_explains_every_refusal() -> None:
    report = _financial("2025-09-30", report_type="Q3", deducted="1.0000")
    quote = _quote("2025-10-10", market_cap="2000000.0000", pb="-1.0000")

    empty = _reference_valuation(None, Decimal("1.0000"), None, report)
    assert (empty["pe"], empty["pe_reason"]) == (None, "no_price")
    assert (empty["pb"], empty["pb_reason"]) == (None, "no_vendor_pb")

    missing_quarter = _reference_valuation(quote, None, "no_prior_cumulative", report)
    assert (missing_quarter["pe"], missing_quarter["pe_reason"]) == (
        None,
        "no_prior_cumulative",
    )

    loss = _reference_valuation(quote, Decimal("0"), None, report)
    assert (loss["pe"], loss["pe_reason"]) == (None, "non_positive_earnings")

    no_cap = _reference_valuation(
        _quote("2025-10-10"), Decimal("1.0000"), None, report
    )
    assert (no_cap["pe"], no_cap["pe_reason"]) == (None, "no_market_cap")

    negative_pb = _reference_valuation(quote, Decimal("1.0000"), None, report)
    assert (negative_pb["pb"], negative_pb["pb_reason"]) == (None, "no_vendor_pb")


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


# -- build-time default basis (QUOT-244) ------------------------------------


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _StubSession:
    """Just enough ``Session`` for ``_compose``: one scripted result per read."""

    def __init__(self, *results: list[Any]) -> None:
        self._results = list(results)

    def scalars(self, _statement: Any) -> _Scalars:
        return _Scalars(self._results.pop(0))


def _compose_values(default_basis: str) -> tuple[DwsService, dict[str, Any]]:
    session = _StubSession(
        [
            _financial("2025-09-30", report_type="Q3", deducted="110000.0000"),
            _financial("2025-06-30", report_type="Q2", deducted="60000.0000"),
            _financial("2025-12-31", profit="100000.0000"),
        ],
        [_quote("2026-01-05", market_cap="2000000.0000", pe_ttm="15.0000", pb="1.5000")],
        [],  # risk events
        [],  # announcements
    )
    service = DwsService(config=Config())
    return service, service._compose(session, "600000", date(2026, 10, 27), 5, default_basis)


def test_resolve_default_basis_precedence() -> None:
    assert resolve_default_basis(None, Config()) == "snapshot"
    configured = Config(metrics_profile="reference")
    assert resolve_default_basis(None, configured) == "reference"
    assert resolve_default_basis(" Snapshot ", configured) == "snapshot"
    assert resolve_default_basis("REFERENCE", Config()) == "reference"

    with pytest.raises(InvalidArgumentError) as excinfo:
        resolve_default_basis("vibe", Config())
    assert excinfo.value.details == {"field": "valuation_basis", "value": "vibe"}


def test_build_rejects_an_unknown_basis_before_touching_the_database() -> None:
    service = DwsService(config=Config())

    with pytest.raises(InvalidArgumentError, match="valuation_basis"):
        service.build(["600000"], default_basis="vibe")


# -- market gate (QUOT-267) --------------------------------------------------


def test_market_gate_accepts_the_six_digit_shape_only() -> None:
    """The gate tests the six-digit shape, not an exchange-prefix whitelist."""

    assert is_a_share_symbol("600000")
    assert is_a_share_symbol("810024")
    assert not is_a_share_symbol("AAPL")
    assert not is_a_share_symbol("BRK.B")
    assert not is_a_share_symbol("60000")
    assert not is_a_share_symbol("6000000")


def test_build_skips_a_non_a_share_symbol_without_touching_the_database() -> None:
    service = DwsService(config=Config())

    report = service.build(["AAPL"], as_of="2026-10-27")

    assert report.counts == {"built": 0, "skipped": 1, "failed": 0}
    assert report.payload()["skipped"] == [{"symbol": "AAPL", "reason": NON_A_SHARE_REASON}]
    # A skipped symbol is not a failure: `dws rebuild` must still exit 0.
    assert report.failed is False


def test_compose_writes_the_configured_default_basis_and_its_mirror() -> None:
    service, values = _compose_values("reference")

    valuation = values["metrics_json"]["valuation"]
    assert valuation["default_basis"] == "reference"
    assert set(valuation["bases"]) == {"snapshot", "reference"}
    # Both bases are still computed; only the default pointer moves.
    assert valuation["bases"]["snapshot"]["pe"] == 15.0
    assert valuation["bases"]["reference"]["pe"] == 10.0
    # The flat keys mirror the default basis (spec §4.4.5).
    assert valuation["pe"] == 10.0
    assert valuation["pe_source"] == "deducted_annualized"
    assert valuation["pb"] == 1.5
    assert valuation["pb_reason"] is None
    assert valuation["pe"] == valuation["bases"][valuation["default_basis"]]["pe"]

    service._check_row(values, fiscal_years=5, day=date(2026, 10, 27))


def test_compose_keeps_the_snapshot_mirror_by_default() -> None:
    _service, values = _compose_values("snapshot")

    valuation = values["metrics_json"]["valuation"]
    assert valuation["default_basis"] == "snapshot"
    assert valuation["pe"] == 15.0
    assert valuation["pb"] is None
    assert valuation["pb_reason"] == "no_equity"
