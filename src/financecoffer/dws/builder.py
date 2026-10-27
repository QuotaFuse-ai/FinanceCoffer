"""DWS wide-table builder.

``DwsService.build`` recomputes one ``dws.stock_agent_analysis_wide`` row per
symbol from DWD facts under these rules:

* store don't compute: amounts move as :class:`~decimal.Decimal` and are written
  as fixed-point strings; nothing is annualized, reconciled or adjusted;
* upstream quality is never re-validated: a ``bad`` year still lands and is only
  flagged in ``metrics_json.quality``;
* the wide row is a snapshot: a whole-row upsert replaces the previous build and
  no DWD row is ever written;
* missing is missing: an empty window year is an explicit ``null`` record with
  ``data_status="missing"``, never ``0`` or an interpolated value;
* deterministic: every query has a total ``ORDER BY`` and only ``as_of`` (never
  ``now()``) drives time logic, so two runs over the same input and ``as_of``
  produce byte-identical JSONB (``built_at`` aside).

``AgentAnalysisWideRepository.upsert`` cannot express the ``as_of`` guard from
spec section 6.4, so the builder takes a per-symbol transaction advisory lock and
compares the stored ``as_of`` before writing. The lock serialises concurrent
builds of the same symbol, which makes the read-then-write check sufficient.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from financecoffer.core.config import Config
from financecoffer.core.errors import (
    InvalidArgumentError,
    NotFoundError,
    StorageError,
)
from financecoffer.db.models.dwd import (
    StockAnnouncement,
    StockFinancialSnapshot,
    StockQuoteSnapshot,
    StockRiskEvent,
)
from financecoffer.db.models.dws import StockAgentAnalysisWide
from financecoffer.db.repositories.dws import AgentAnalysisWideRepository
from financecoffer.db.session import Database
from financecoffer.query import normalize_symbol

_log = logging.getLogger(__name__)

SOURCE_VERSION = "dws-1"
METRICS_SCHEMA = "dws.metrics.v1"
DEFAULT_FISCAL_YEARS = 5
MAX_FISCAL_YEARS = 10

ANNUAL_REPORT_TYPES = frozenset({"FY", "ANNUAL", "年报", "A"})
SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
RISK_WINDOW_YEARS = 3
RISK_HIGHLIGHT_LIMIT = 10
RISK_SUMMARY_CHARS = 200
RISK_TEXT_CHARS = 2000
ANNOUNCEMENT_LIMIT = 20
TITLE_CHARS = 200
PRICE_GAP_DAYS = 366

_QUANT = Decimal("0.0001")
_MONEY_PATTERN = re.compile(r"^-?\d{1,16}(\.\d{1,4})?$")

BuildStatus = Literal["built", "skipped", "failed"]


class _Skip(Exception):
    """Internal control flow: the symbol produced no wide row, but not an error."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Fail(Exception):
    """Internal control flow: the symbol failed and must not be written."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class SymbolResult:
    """Outcome of building one symbol."""

    symbol: str
    status: BuildStatus
    reason: str | None = None


@dataclass(frozen=True)
class BuildReport:
    """Outcome of one ``dws build`` run."""

    as_of: date
    fiscal_years: int
    results: tuple[SymbolResult, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "built": sum(1 for item in self.results if item.status == "built"),
            "skipped": sum(1 for item in self.results if item.status == "skipped"),
            "failed": sum(1 for item in self.results if item.status == "failed"),
        }

    @property
    def failed(self) -> bool:
        return any(item.status == "failed" for item in self.results)

    def payload(self) -> dict[str, Any]:
        def rows(status: BuildStatus) -> list[dict[str, Any]]:
            return [
                {"symbol": item.symbol, "reason": item.reason}
                for item in self.results
                if item.status == status
            ]

        return {
            "as_of": self.as_of.isoformat(),
            "fiscal_years": self.fiscal_years,
            "counts": self.counts,
            "built": [item.symbol for item in self.results if item.status == "built"],
            "skipped": rows("skipped"),
            "failed": rows("failed"),
        }


@dataclass(frozen=True)
class _YearSelection:
    year: int
    row: StockFinancialSnapshot | None
    basis: str | None
    ambiguous: bool


def parse_as_of(value: str | None, *, today: date | None = None) -> date:
    """Return the cutoff day; ``None`` means today in UTC (spec section 3.1)."""

    if value is None or not value.strip():
        return today if today is not None else datetime.now(UTC).date()
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise InvalidArgumentError(
            "as-of must be YYYY-MM-DD",
            details={"field": "as-of", "value": value},
        ) from exc


def validate_fiscal_years(value: int) -> int:
    """Reject a window size the DDL would refuse instead of clipping it."""

    if value < 1 or value > MAX_FISCAL_YEARS:
        raise InvalidArgumentError(
            f"years must be between 1 and {MAX_FISCAL_YEARS}",
            details={"field": "years", "value": value},
        )
    return value


def years_back(day: date, years: int) -> date:
    """Return ``day`` minus ``years`` calendar years (Feb 29 falls back to 28)."""

    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def money(value: Decimal | None) -> str | None:
    """Render a DWD amount as a fixed-point string with 4 decimals."""

    if value is None:
        return None
    with localcontext() as context:
        context.prec = 60
        return str(value.quantize(_QUANT))


def rate(value: Decimal | None) -> float | None:
    """Render a ratio as a 4-decimal JSON number."""

    if value is None:
        return None
    with localcontext() as context:
        context.prec = 60
        return float(value.quantize(_QUANT))


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_of_timestamp(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _missing_year(year: int) -> dict[str, Any]:
    return {
        "year": year,
        "report_date": None,
        "report_type": None,
        "version": None,
        "report_basis": None,
        "revenue": None,
        "net_profit": None,
        "roe": None,
        "roe_reason": None,
        "pe": None,
        "pe_reason": None,
        "pb": None,
        "pb_reason": None,
        "quality_status": None,
        "confidence": None,
        "data_status": "missing",
    }


def _present_year(
    row: StockFinancialSnapshot,
    basis: str,
    *,
    pe: float | None,
    pe_reason: str | None,
) -> dict[str, Any]:
    return {
        "year": row.report_date.year,
        "report_date": row.report_date.isoformat(),
        "report_type": row.report_type,
        "version": row.version,
        "report_basis": basis,
        "revenue": money(row.amount_revenue),
        "net_profit": money(row.amount_net_profit),
        "roe": None,
        "roe_reason": "equity_unavailable",
        "pe": pe,
        "pe_reason": pe_reason,
        "pb": None,
        "pb_reason": "no_equity",
        "quality_status": row.quality_status,
        "confidence": row.confidence,
        "data_status": "present",
    }


def _amount_rank(row: StockFinancialSnapshot) -> tuple[int, int, str]:
    """Rank a financial row: how many amounts it carries, then its version.

    One report is published as several statements (balance / income / cashflow)
    that share a report date and type. The wide row reports revenue and net
    profit, which only the income statement carries, so a row that actually holds
    them wins over a later revision of a statement that does not -- and over the
    rows ingested before the statement was stored, where the three statements had
    collapsed into one version chain. The statement name only breaks ties, so two
    rows that agree on amounts and version still order deterministically.
    """

    carried = int(row.amount_revenue is not None) + int(row.amount_net_profit is not None)
    return carried, row.version, row.statement


def _select_years(
    rows: Sequence[StockFinancialSnapshot],
    *,
    day: date,
    fiscal_years: int,
) -> tuple[list[_YearSelection], list[int]]:
    """Pick one report per window year: annual first, else the latest period."""

    # Reduce every (report date, report type) to the row that best answers the
    # wide row's question -- revenue and net profit -- with the newest revision
    # of that statement winning.
    by_report: dict[tuple[date, str], StockFinancialSnapshot] = {}
    for row in rows:
        key = (row.report_date, row.report_type)
        current = by_report.get(key)
        if current is None or _amount_rank(row) > _amount_rank(current):
            by_report[key] = row

    by_year: dict[int, list[StockFinancialSnapshot]] = {}
    for row in by_report.values():
        by_year.setdefault(row.report_date.year, []).append(row)

    from_year = day.year - fiscal_years + 1
    selections: list[_YearSelection] = []
    missing: list[int] = []
    for year in range(day.year, from_year - 1, -1):
        candidates = by_year.get(year, [])
        if not candidates:
            selections.append(_YearSelection(year, None, None, False))
            missing.append(year)
            continue
        annual = [
            row
            for row in candidates
            if row.report_type.strip().upper() in ANNUAL_REPORT_TYPES
        ]
        pool = annual or candidates
        chosen = max(pool, key=lambda item: (item.report_date, item.version, item.report_type))
        ambiguous = (
            len(
                {
                    row.report_type
                    for row in candidates
                    if row.report_date == chosen.report_date
                }
            )
            > 1
        )
        selections.append(
            _YearSelection(year, chosen, "annual" if annual else "period", ambiguous)
        )

    missing.sort()
    return selections, missing


def _pe_for_year(
    quote: StockQuoteSnapshot | None,
    report_date: date,
    net_profit: Decimal | None,
) -> tuple[float | None, str | None, str | None]:
    """Return ``(pe, reason, source)`` for one year (spec section 4.4.4)."""

    if quote is None or (quote.close is None and quote.total_market_cap is None):
        return None, "no_price", None
    if abs((quote.trade_date - report_date).days) > PRICE_GAP_DAYS:
        return None, "no_price", None
    if quote.pe_ttm is not None and quote.pe_ttm > 0:
        return rate(quote.pe_ttm), None, "quote.pe_ttm"
    if net_profit is None:
        return None, "no_earnings", None
    if net_profit <= 0:
        return None, "non_positive_earnings", None
    if quote.total_market_cap is None:
        return None, "no_price", None
    with localcontext() as context:
        context.prec = 60
        value = quote.total_market_cap / net_profit
    return rate(value), None, "market_cap/net_profit"


def _risk_summary(
    events: Sequence[StockRiskEvent],
    *,
    since: date,
    until: date,
) -> dict[str, Any]:
    counts = {severity: 0 for severity in SEVERITIES}
    deduped: list[StockRiskEvent] = []
    seen: set[str] = set()
    for event in events:
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        if event.severity in counts:
            counts[event.severity] += 1
        deduped.append(event)

    total = sum(counts.values())
    highest = next((severity for severity in SEVERITIES if counts[severity]), None)
    latest = deduped[0] if deduped else None

    highlights: list[dict[str, Any]] = []
    for event in deduped:
        if event.severity not in ("high", "critical"):
            continue
        highlights.append(
            {
                "event_id": event.event_id,
                "occur_date": event.occur_date.isoformat(),
                "severity": event.severity,
                "risk_type": event.risk_type,
                "summary": _risk_text_for(event),
                "source_announcement_ref": event.source_announcement_ref,
                "quality_status": event.quality_status,
                "confidence": event.confidence,
            }
        )
        if len(highlights) == RISK_HIGHLIGHT_LIMIT:
            break

    text = (
        f"近 {RISK_WINDOW_YEARS} 年共 {total} 条风险事件："
        f"critical {counts['critical']} 条、high {counts['high']} 条、"
        f"medium {counts['medium']} 条、low {counts['low']} 条。"
    )
    if latest is not None:
        text += (
            f"最近一次为 {latest.occur_date.isoformat()} 的 {latest.risk_type}"
            f"（{latest.severity}）：{_risk_text_for(latest)}。"
        )
    truncated = len(text) > RISK_TEXT_CHARS
    if truncated:
        text = text[: RISK_TEXT_CHARS - 1] + "…"

    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "as_of": until.isoformat(),
        "counts": {**counts, "total": total},
        "highest_severity": highest,
        "latest_occur_date": latest.occur_date.isoformat() if latest else None,
        "highlights": highlights,
        "truncated": truncated,
        "text": text,
    }


def _risk_text_for(event: StockRiskEvent) -> str:
    summary = (event.summary or "").strip()
    if not summary:
        return f"{event.risk_type}（未提供摘要）"
    return _clip(summary, RISK_SUMMARY_CHARS)


def _announcements(rows: Sequence[StockAnnouncement]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    payload: list[dict[str, Any]] = []
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        title = row.title
        entry: dict[str, Any] = {
            "id": row.id,
            "ann_date": row.ann_date.isoformat(),
            "ann_type": row.ann_type,
            "title": _clip(title, TITLE_CHARS) if title is not None else None,
            "minio_url": row.minio_url,
            "quality_status": row.quality_status,
            "confidence": row.confidence,
        }
        if title is not None and len(title) > TITLE_CHARS:
            entry["title_truncated"] = True
        payload.append(entry)
    return payload


def _lock(session: Session, symbol: str) -> None:
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"dws:{symbol}")))
    )


class DwsService:
    """Build and read ``dws.stock_agent_analysis_wide`` rows."""

    def __init__(
        self,
        *,
        config: Config,
        dsn: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.config = config
        self.dsn = dsn or config.db_url
        self.timeout = timeout
        self._database: Database | None = None

    @property
    def compliance_enabled(self) -> bool:
        return self.config.compliance_enabled

    @property
    def database(self) -> Database:
        if self._database is None:
            self._database = Database(self.dsn)
        return self._database

    def close(self) -> None:
        if self._database is not None:
            self._database.dispose()
            self._database = None

    def __enter__(self) -> DwsService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def build(
        self,
        symbols: Sequence[str] = (),
        *,
        build_all: bool = False,
        as_of: str | None = None,
        fiscal_years: int = DEFAULT_FISCAL_YEARS,
    ) -> BuildReport:
        validate_fiscal_years(fiscal_years)
        day = parse_as_of(as_of)
        targets = self._targets(symbols, build_all=build_all)
        results = tuple(self._build_one(symbol, day, fiscal_years) for symbol in targets)
        return BuildReport(as_of=day, fiscal_years=fiscal_years, results=results)

    def show(self, symbol: str) -> dict[str, Any]:
        canonical = normalize_symbol(symbol)
        with self.database.session() as session:
            row = session.get(StockAgentAnalysisWide, canonical)
            if row is None:
                raise NotFoundError(
                    f"no wide row for {canonical}",
                    details={"symbol": canonical, "hint": "run `fcoffer dws build`"},
                )
            payload: dict[str, Any] = {
                "symbol": row.symbol,
                "as_of": _iso(row.as_of),
                "metrics_json": row.metrics_json,
                "risk_summary": row.risk_summary,
                "latest_announcements": row.latest_announcements,
                "fiscal_years": row.fiscal_years,
                "source_version": row.source_version,
                "built_at": _iso(row.built_at),
            }
            if self.compliance_enabled:
                payload["compliance"] = {
                    "tier": row.compliance_tier,
                    "commercial_use": row.commercial_use,
                }
            return payload

    def _targets(self, symbols: Sequence[str], *, build_all: bool) -> list[str]:
        if symbols and not build_all:
            targets: list[str] = []
            seen: set[str] = set()
            for raw in symbols:
                symbol = normalize_symbol(raw)
                if symbol not in seen:
                    seen.add(symbol)
                    targets.append(symbol)
            return targets
        try:
            with self.database.session() as session:
                statement = (
                    select(StockFinancialSnapshot.symbol)
                    .distinct()
                    .order_by(StockFinancialSnapshot.symbol)
                )
                return list(session.scalars(statement))
        except SQLAlchemyError as exc:
            raise StorageError(f"cannot list DWD symbols: {exc}") from exc

    def _build_one(self, symbol: str, day: date, fiscal_years: int) -> SymbolResult:
        try:
            with self.database.session_scope() as session:
                _lock(session, symbol)
                existing = session.get(StockAgentAnalysisWide, symbol)
                if existing is not None and _stored_as_of(existing) > day:
                    raise _Skip("stale_as_of")
                values = self._compose(session, symbol, day, fiscal_years)
                self._check_row(values, fiscal_years=fiscal_years, day=day)
                AgentAnalysisWideRepository(session).upsert(**values)
        except _Skip as exc:
            return SymbolResult(symbol=symbol, status="skipped", reason=exc.reason)
        except _Fail as exc:
            if exc.detail:
                _log.warning("dws build for %s failed: %s", symbol, exc.detail)
            return SymbolResult(symbol=symbol, status="failed", reason=exc.reason)
        except SQLAlchemyError as exc:
            _log.warning("dws build for %s hit a storage error: %s", symbol, exc)
            return SymbolResult(symbol=symbol, status="failed", reason="storage_error")
        return SymbolResult(symbol=symbol, status="built")

    def _compose(
        self,
        session: Session,
        symbol: str,
        day: date,
        fiscal_years: int,
    ) -> dict[str, Any]:
        financial_rows = list(
            session.scalars(
                select(StockFinancialSnapshot)
                .where(
                    StockFinancialSnapshot.symbol == symbol,
                    StockFinancialSnapshot.report_date <= day,
                )
                .order_by(
                    StockFinancialSnapshot.report_date.desc(),
                    StockFinancialSnapshot.report_type.asc(),
                    StockFinancialSnapshot.version.desc(),
                    StockFinancialSnapshot.statement.asc(),
                )
            )
        )
        if not financial_rows:
            raise _Skip("no_financial_data")

        selections, missing = _select_years(
            financial_rows, day=day, fiscal_years=fiscal_years
        )
        if not any(selection.row is not None for selection in selections):
            raise _Skip("no_financial_data_in_window")

        quote = session.scalars(
            select(StockQuoteSnapshot)
            .where(
                StockQuoteSnapshot.symbol == symbol,
                StockQuoteSnapshot.trade_date <= day,
            )
            .order_by(
                StockQuoteSnapshot.trade_date.desc(),
                StockQuoteSnapshot.version.desc(),
            )
            .limit(1)
        ).first()

        years: list[dict[str, Any]] = []
        latest_pe: float | None = None
        latest_pe_reason: str | None = None
        latest_pe_source: str | None = None
        captured_latest = False
        chosen_rows: list[StockFinancialSnapshot] = []
        for selection in selections:
            if selection.row is None:
                years.append(_missing_year(selection.year))
                continue
            row = selection.row
            pe, pe_reason, pe_source = _pe_for_year(
                quote, row.report_date, row.amount_net_profit
            )
            if not captured_latest:
                latest_pe, latest_pe_reason, latest_pe_source = pe, pe_reason, pe_source
                captured_latest = True
            chosen_rows.append(row)
            years.append(
                _present_year(row, selection.basis or "period", pe=pe, pe_reason=pe_reason)
            )

        risk_since = years_back(day, RISK_WINDOW_YEARS)
        risk_events = list(
            session.scalars(
                select(StockRiskEvent)
                .where(
                    StockRiskEvent.symbol == symbol,
                    StockRiskEvent.occur_date > risk_since,
                    StockRiskEvent.occur_date <= day,
                )
                .order_by(
                    StockRiskEvent.occur_date.desc(),
                    StockRiskEvent.id.desc(),
                )
            )
        )
        announcement_rows = list(
            session.scalars(
                select(StockAnnouncement)
                .where(
                    StockAnnouncement.symbol == symbol,
                    StockAnnouncement.ann_date <= day,
                )
                .order_by(
                    StockAnnouncement.ann_date.desc(),
                    StockAnnouncement.id.desc(),
                )
                .limit(ANNOUNCEMENT_LIMIT)
            )
        )

        metrics = {
            "schema": METRICS_SCHEMA,
            "basis": "annual",
            "window": {
                "from_year": day.year - fiscal_years + 1,
                "to_year": day.year,
                "fiscal_years": fiscal_years,
                "as_of": day.isoformat(),
            },
            "years": years,
            "gaps": missing,
            "valuation": {
                "as_of": day.isoformat(),
                "price_date": _iso(quote.trade_date) if quote is not None else None,
                "price_close": money(quote.close) if quote is not None else None,
                "pe_source": latest_pe_source,
                "pe": latest_pe,
                "pe_reason": latest_pe_reason,
                "pb": None,
                "pb_reason": "no_equity",
            },
            "quality": _quality_block(selections, missing),
        }

        compliance_tier, commercial_use = self._compliance(
            [
                *chosen_rows,
                *risk_events,
                *announcement_rows,
                *([quote] if quote is not None else []),
            ]
        )

        return {
            "symbol": symbol,
            "as_of": _as_of_timestamp(day),
            "metrics_json": metrics,
            "risk_summary": _risk_summary(
                risk_events, since=risk_since, until=day
            ),
            "latest_announcements": _announcements(announcement_rows),
            "fiscal_years": fiscal_years,
            "source_version": SOURCE_VERSION,
            "compliance_tier": compliance_tier,
            "commercial_use": commercial_use,
        }

    def _compliance(self, rows: Sequence[Any]) -> tuple[str | None, bool | None]:
        if not self.compliance_enabled:
            return None, None
        marks = {(row.compliance_tier, row.commercial_use) for row in rows}
        if any(tier is None for tier, _ in marks):
            raise _Fail("compliance_missing")
        if len(marks) > 1:
            raise _Fail("compliance_mismatch")
        tier, commercial_use = marks.pop()
        return tier, commercial_use

    def _check_row(
        self,
        values: dict[str, Any],
        *,
        fiscal_years: int,
        day: date,
    ) -> None:
        """Fail closed before the write when a built row breaks the contract."""

        metrics = values["metrics_json"]
        _require(isinstance(metrics, dict), "metrics_json must be an object")
        years = metrics["years"]
        _require(isinstance(years, list), "metrics_json.years must be an array")
        _require(len(years) <= fiscal_years, "metrics_json.years is longer than the window")

        from_year = day.year - fiscal_years + 1
        previous_year: int | None = None
        seen_years: set[int] = set()
        for record in years:
            year = record["year"]
            _require(
                isinstance(year, int) and not isinstance(year, bool),
                "year must be an integer",
            )
            _require(from_year <= year <= day.year, f"year {year} is outside the window")
            _require(year not in seen_years, f"duplicate year {year}")
            _require(
                previous_year is None or year < previous_year,
                "years must be strictly descending",
            )
            seen_years.add(year)
            previous_year = year

            for name in ("revenue", "net_profit"):
                amount = record[name]
                _require(
                    amount is None
                    or (
                        isinstance(amount, str)
                        and _MONEY_PATTERN.fullmatch(amount) is not None
                    ),
                    f"{name} must be a fixed-point string or null",
                )
            for name in ("roe", "pe", "pb"):
                value = record[name]
                _require(
                    value is None
                    or (
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    ),
                    f"{name} must be a number or null",
                )
            _require(record["pe"] is None or record["pe"] >= 0, "pe must not be negative")
            _require(record["pb"] is None or record["pb"] >= 0, "pb must not be negative")
            _require(
                record["roe"] is None or -10 <= record["roe"] <= 10,
                "roe is outside [-10, 10]",
            )
            if record["data_status"] == "missing":
                _require(
                    all(
                        record[name] is None
                        for name in ("revenue", "net_profit", "roe", "pe", "pb")
                    ),
                    "a missing year must not carry metric values",
                )

        risk = values["risk_summary"]
        counts = risk["counts"]
        _require(
            counts["total"] == sum(counts[severity] for severity in SEVERITIES),
            "risk counts.total must equal the four severity buckets",
        )
        _require(
            len(risk["highlights"]) <= RISK_HIGHLIGHT_LIMIT,
            "too many risk highlights",
        )

        announcements = values["latest_announcements"]
        _require(
            len(announcements) <= ANNOUNCEMENT_LIMIT,
            "too many announcements",
        )
        order = [(item["ann_date"], item["id"]) for item in announcements]
        _require(
            order == sorted(order, reverse=True),
            "announcements must be ordered by ann_date DESC, id DESC",
        )

        _require(
            1 <= values["fiscal_years"] <= MAX_FISCAL_YEARS,
            "fiscal_years outside [1, 10]",
        )
        _require(
            values["as_of"] == _as_of_timestamp(day),
            "as_of must be the UTC midnight of the cutoff day",
        )
        if not self.compliance_enabled:
            _require(
                values["compliance_tier"] is None
                and values["commercial_use"] is None,
                "compliance columns must stay NULL when the switch is off",
            )


def _quality_block(
    selections: Sequence[_YearSelection],
    missing: Sequence[int],
) -> dict[str, Any]:
    bad = sorted(
        selection.year
        for selection in selections
        if selection.row is not None and selection.row.quality_status == "bad"
    )
    suspicious = sorted(
        selection.year
        for selection in selections
        if selection.row is not None and selection.row.quality_status == "suspicious"
    )
    block: dict[str, Any] = {
        "bad_years": bad,
        "suspicious_years": suspicious,
        "missing_years": list(missing),
    }
    if any(selection.ambiguous for selection in selections):
        block["ambiguous_report"] = True
    return block


def _stored_as_of(row: StockAgentAnalysisWide) -> date:
    value = row.as_of
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(UTC).date()


def _require(condition: object, message: str) -> None:
    if not condition:
        raise _Fail("self_check_failed", message)
