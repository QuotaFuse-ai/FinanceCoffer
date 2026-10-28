"""DWD read and replay operations.

``show`` / ``versions`` are reads over ``dwd.stock_financial_snapshot``.
``backfill`` re-applies ODS payloads through the same validation and writer the
consumer uses, so it can only add missing versions: the writer never issues
``UPDATE``/``DELETE`` and a replayed ``event_id`` is skipped by its unique
constraint. ``verify`` compares stored quality columns with the ODS payload and
checks money precision; it never recomputes a business value or the balance
check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from financecoffer.core.config import Config
from financecoffer.core.errors import FinanceCofferError, InvalidArgumentError, NotFoundError
from financecoffer.core.redaction import redact_text
from financecoffer.db.models.dwd import StockFinancialSnapshot
from financecoffer.db.repositories.dwd import FinancialSnapshotRepository
from financecoffer.db.repositories.ods import RawDataRepository
from financecoffer.db.session import Database
from financecoffer.dwd.writer import write_event
from financecoffer.events.validation import validate_event
from financecoffer.ods.service import parse_instant
from financecoffer.query import normalize_symbol

REPORT_TYPE_MAX_CHARS = 20
MAX_VERSIONS = 100
MAX_VERIFY = 1000
MAX_BACKFILL = 10_000
BACKFILL_BATCH = 500
MAX_REPORTED = 50

#: Contract types that have a DWD table; others stay in ODS untouched.
BACKFILL_DATA_TYPES: tuple[str, ...] = (
    "financial",
    "announcement",
    "kline",
    "quote",
    "risk",
)

_SCALE = 4
_QUANTUM = Decimal("0.0001")
_CHECK_PRECISION = 60
_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)


def parse_report_date(value: str | None) -> date | None:
    """Parse ``--report-date`` as a date-only ISO string."""

    if value is None:
        return None
    cleaned = value.strip()
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise InvalidArgumentError(
            "--report-date must be YYYY-MM-DD",
            details={"field": "report_date", "value": value},
        ) from exc
    if parsed.isoformat() != cleaned:
        raise InvalidArgumentError(
            "--report-date must be YYYY-MM-DD",
            details={"field": "report_date", "value": value},
        )
    return parsed


def normalize_report_type(value: str | None) -> str | None:
    """Validate ``--report-type`` against the DDL's ``VARCHAR(20)``.

    The event contract stores the publisher's own label (``Q3``, ``FY``,
    ``Q1-Q3``), so any short non-empty label is accepted; only the column width
    is enforced here.
    """

    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise InvalidArgumentError(
            "--report-type must not be empty",
            details={"field": "report_type", "value": value},
        )
    if len(cleaned) > REPORT_TYPE_MAX_CHARS:
        raise InvalidArgumentError(
            f"--report-type must be at most {REPORT_TYPE_MAX_CHARS} characters",
            details={"field": "report_type", "value": value},
        )
    return cleaned


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    with localcontext() as context:
        context.prec = _CHECK_PRECISION
        return str(value.quantize(_QUANTUM))


def _iso(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return value.isoformat()


def _money_issue(value: Any, *, precision: int) -> str | None:
    """Return why ``value`` would not round-trip ``NUMERIC(precision, 4)``."""

    if value is None:
        return None
    if not isinstance(value, Decimal):
        return f"not a Decimal ({type(value).__name__})"
    with localcontext() as context:
        context.prec = _CHECK_PRECISION
        try:
            exact = value.quantize(_QUANTUM)
            limit = Decimal(1).scaleb(precision - _SCALE)
        except InvalidOperation:
            return f"does not fit NUMERIC({precision}, {_SCALE})"
    if not value.is_finite() or exact != value:
        return f"more than {_SCALE} decimal places"
    if abs(exact) >= limit:
        return f"integer digits exceed NUMERIC({precision}, {_SCALE})"
    return None


def _upstream_quality(raw: Any) -> tuple[str | None, str | None] | None:
    """Read ``quality_metrics`` from a stored ODS payload without re-judging it."""

    if not isinstance(raw, Mapping):
        return None
    content = raw.get("content")
    if not isinstance(content, Mapping):
        return None
    metrics = content.get("quality_metrics")
    if not isinstance(metrics, Mapping):
        return None
    status = metrics.get("quality_status")
    confidence = metrics.get("confidence")
    return (
        str(status) if status is not None else None,
        str(confidence) if confidence is not None else None,
    )


@dataclass
class _Counters:
    scanned: int = 0
    inserted: int = 0
    skipped: int = 0
    invalid: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "invalid": self.invalid,
            "failed": self.failed,
        }


class DwdService:
    """Read financial snapshots and replay ODS payloads into DWD."""

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

    def __enter__(self) -> DwdService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def show(
        self,
        symbol: str,
        *,
        report_date: str | None = None,
        report_type: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        canonical = normalize_symbol(symbol)
        day = parse_report_date(report_date)
        label = normalize_report_type(report_type)
        if version is not None and version < 1:
            raise InvalidArgumentError(
                "--version must be >= 1",
                details={"field": "version", "value": version},
            )

        with self.database.session() as session:
            resolved_date, resolved_type = self._resolve_key(
                session,
                symbol=canonical,
                report_date=day,
                report_type=label,
            )
            key = {
                "symbol": canonical,
                "report_date": resolved_date,
                "report_type": resolved_type,
            }
            if version is None:
                row = FinancialSnapshotRepository(session).latest(**key)
            else:
                row = session.get(StockFinancialSnapshot, {**key, "version": version})

            if row is None:
                raise NotFoundError(
                    "financial snapshot not found",
                    details={
                        "symbol": canonical,
                        "report_date": resolved_date.isoformat(),
                        "report_type": resolved_type,
                        "version": version,
                        "available_versions": self._versions(session, key),
                        "hint": "run `fcoffer dwd versions` to list stored versions",
                    },
                )
            payload = self._snapshot(row)
            payload["available_versions"] = self._versions(session, key)
            return payload

    def versions(
        self,
        symbol: str,
        *,
        report_date: str,
        report_type: str,
    ) -> dict[str, Any]:
        canonical = normalize_symbol(symbol)
        day = parse_report_date(report_date)
        label = normalize_report_type(report_type)
        if day is None or label is None:
            raise InvalidArgumentError(
                "--report-date and --report-type are required",
                details={"field": "report_date"},
            )

        with self.database.session() as session:
            rows = self._version_rows(
                session,
                {"symbol": canonical, "report_date": day, "report_type": label},
            )
            if not rows:
                raise NotFoundError(
                    "no versions stored for the requested snapshot",
                    details={
                        "symbol": canonical,
                        "report_date": day.isoformat(),
                        "report_type": label,
                    },
                )
            return {
                "symbol": canonical,
                "report_date": day.isoformat(),
                "report_type": label,
                "count": len(rows),
                "versions": rows,
            }

    def backfill(
        self,
        *,
        from_ods: bool,
        symbol: str | None = None,
        since: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Replay ODS payloads into DWD; existing versions are never touched."""

        if not from_ods:
            raise InvalidArgumentError(
                "--from-ods is required",
                details={"field": "from_ods", "hint": "dwd backfill --from-ods"},
            )
        canonical = normalize_symbol(symbol) if symbol is not None else None
        since_at = parse_instant(since, field_name="since")
        start = since_at if since_at is not None else datetime(1970, 1, 1, tzinfo=UTC)

        totals = _Counters()
        per_type = {name: _Counters() for name in BACKFILL_DATA_TYPES}
        failures: list[dict[str, Any]] = []
        would_write = 0
        truncated = False

        with self.database.session_scope() as session:
            repository = RawDataRepository(session)
            for data_type in BACKFILL_DATA_TYPES:
                offset = 0
                while True:
                    if totals.scanned >= MAX_BACKFILL:
                        truncated = True
                        break
                    batch = repository.iter_range(
                        start,
                        _FAR_FUTURE,
                        data_type=data_type,
                        symbol=canonical,
                        limit=BACKFILL_BATCH,
                        offset=offset,
                    )
                    if not batch:
                        break
                    for row in batch:
                        if totals.scanned >= MAX_BACKFILL:
                            truncated = True
                            break
                        totals.scanned += 1
                        per_type[data_type].scanned += 1
                        outcome = self._replay_row(
                            session,
                            row,
                            dry_run=dry_run,
                            failures=failures,
                            totals=totals,
                            type_counters=per_type[data_type],
                        )
                        if outcome == "valid":
                            would_write += 1
                    offset += len(batch)
                    if truncated or len(batch) < BACKFILL_BATCH:
                        break

        return {
            "dry_run": dry_run,
            "from_ods": True,
            "symbol": canonical,
            "since": _iso(since_at),
            "would_write": would_write if dry_run else None,
            **totals.as_dict(),
            "truncated": truncated,
            "limit": MAX_BACKFILL,
            "by_data_type": {name: counter.as_dict() for name, counter in per_type.items()},
            "failures": failures[:MAX_REPORTED],
        }

    def verify(
        self,
        *,
        symbol: str | None = None,
        report_date: str | None = None,
    ) -> dict[str, Any]:
        """Check quality passthrough and money precision on stored snapshots."""

        canonical = normalize_symbol(symbol) if symbol is not None else None
        day = parse_report_date(report_date)

        failures: list[dict[str, Any]] = []
        checked = passed = unverified = 0
        truncated = False

        with self.database.session() as session:
            rows = self._verify_rows(session, symbol=canonical, report_date=day)
            truncated = len(rows) >= MAX_VERIFY
            repository = RawDataRepository(session)
            for row in rows:
                checked += 1
                issues: list[dict[str, str]] = []

                for field_name, precision in (
                    ("amount_revenue", 20),
                    ("amount_net_profit", 20),
                ):
                    reason = _money_issue(getattr(row, field_name), precision=precision)
                    if reason is not None:
                        issues.append({"field": field_name, "reason": reason})

                raw_rows = repository.by_event_id(row.event_id, limit=1)
                upstream = _upstream_quality(raw_rows[0].raw_json) if raw_rows else None
                if upstream is None:
                    unverified += 1
                else:
                    expected_status, expected_confidence = upstream
                    if expected_status != row.quality_status:
                        issues.append(
                            {
                                "field": "quality_status",
                                "reason": (
                                    f"stored={row.quality_status!r} upstream={expected_status!r}"
                                ),
                            }
                        )
                    if expected_confidence != row.confidence:
                        issues.append(
                            {
                                "field": "confidence",
                                "reason": (
                                    f"stored={row.confidence!r} upstream={expected_confidence!r}"
                                ),
                            }
                        )

                if issues:
                    failures.append(
                        {
                            "symbol": row.symbol,
                            "report_date": row.report_date.isoformat(),
                            "report_type": row.report_type,
                            "version": row.version,
                            "event_id": row.event_id,
                            "issues": issues,
                        }
                    )
                else:
                    passed += 1

        return {
            "symbol": canonical,
            "report_date": _iso(day),
            "checked": checked,
            "passed": passed,
            "failed": len(failures),
            "unverified": unverified,
            "truncated": truncated,
            "limit": MAX_VERIFY,
            "checks": [
                "quality_status passthrough",
                "confidence passthrough",
                "amount fits NUMERIC(20, 4)",
            ],
            "failures": failures[:MAX_REPORTED],
        }

    def _replay_row(
        self,
        session: Session,
        row: Any,
        *,
        dry_run: bool,
        failures: list[dict[str, Any]],
        totals: _Counters,
        type_counters: _Counters,
    ) -> str:
        """Validate and (unless ``dry_run``) write one ODS row."""

        if row.raw_json is None:
            self._record_failure(
                failures,
                totals,
                type_counters,
                row,
                stage="validate",
                reason="ODS row has no inline payload (raw_file_path only)",
                bucket="invalid",
            )
            return "invalid"
        try:
            validated = validate_event(
                row.raw_json,
                compliance_enabled=self.compliance_enabled,
            )
        except FinanceCofferError as exc:
            self._record_failure(
                failures,
                totals,
                type_counters,
                row,
                stage="validate",
                reason=exc.message,
                bucket="invalid",
            )
            return "invalid"

        if dry_run:
            return "valid"

        try:
            with session.begin_nested():
                outcome = write_event(
                    session,
                    validated,
                    compliance_enabled=self.compliance_enabled,
                )
        except FinanceCofferError as exc:
            self._record_failure(
                failures,
                totals,
                type_counters,
                row,
                stage="write",
                reason=exc.message,
                bucket="failed",
            )
            return "failed"
        except SQLAlchemyError as exc:
            self._record_failure(
                failures,
                totals,
                type_counters,
                row,
                stage="write",
                reason=redact_text(str(exc).strip())[:200],
                bucket="failed",
            )
            return "failed"

        if outcome.inserted:
            totals.inserted += 1
            type_counters.inserted += 1
        else:
            totals.skipped += 1
            type_counters.skipped += 1
        return "inserted" if outcome.inserted else "skipped"

    @staticmethod
    def _record_failure(
        failures: list[dict[str, Any]],
        totals: _Counters,
        type_counters: _Counters,
        row: Any,
        *,
        stage: str,
        reason: str,
        bucket: str,
    ) -> None:
        if bucket == "invalid":
            totals.invalid += 1
            type_counters.invalid += 1
        else:
            totals.failed += 1
            type_counters.failed += 1
        failures.append(
            {
                "id": row.id,
                "event_id": row.event_id,
                "data_type": row.data_type,
                "stage": stage,
                "reason": reason,
            }
        )

    @staticmethod
    def _resolve_key(
        session: Session,
        *,
        symbol: str,
        report_date: date | None,
        report_type: str | None,
    ) -> tuple[date, str]:
        """Fill in the newest report date and type when the caller omitted them."""

        day = report_date
        if day is None:
            statement = select(func.max(StockFinancialSnapshot.report_date)).where(
                StockFinancialSnapshot.symbol == symbol
            )
            if report_type is not None:
                statement = statement.where(StockFinancialSnapshot.report_type == report_type)
            day = session.scalar(statement)
            if day is None:
                raise NotFoundError(
                    "no financial snapshot for this symbol",
                    details={"symbol": symbol, "report_type": report_type},
                )

        label = report_type
        if label is None:
            statement = (
                select(StockFinancialSnapshot.report_type)
                .where(
                    StockFinancialSnapshot.symbol == symbol,
                    StockFinancialSnapshot.report_date == day,
                )
                .order_by(
                    StockFinancialSnapshot.ingested_at.desc(),
                    StockFinancialSnapshot.version.desc(),
                    StockFinancialSnapshot.report_type,
                )
                .limit(1)
            )
            label = session.scalar(statement)
            if label is None:
                raise NotFoundError(
                    "no financial snapshot for this report date",
                    details={"symbol": symbol, "report_date": day.isoformat()},
                )
        return day, label

    @staticmethod
    def _versions(session: Session, key: Mapping[str, Any]) -> list[int]:
        statement = (
            select(StockFinancialSnapshot.version)
            .where(*(getattr(StockFinancialSnapshot, name) == value for name, value in key.items()))
            .order_by(StockFinancialSnapshot.version.desc())
            .limit(MAX_VERSIONS)
        )
        return [int(version) for version in session.scalars(statement)]

    @staticmethod
    def _version_rows(session: Session, key: Mapping[str, Any]) -> list[dict[str, Any]]:
        statement = (
            select(StockFinancialSnapshot)
            .where(*(getattr(StockFinancialSnapshot, name) == value for name, value in key.items()))
            .order_by(StockFinancialSnapshot.version.desc())
            .limit(MAX_VERSIONS)
        )
        return [
            {
                "version": row.version,
                "ingested_at": _iso(row.ingested_at),
                "fetch_time": _iso(row.fetch_time),
                "quality_status": row.quality_status,
                "confidence": row.confidence,
                "event_id": row.event_id,
                "provider": row.provider,
            }
            for row in session.scalars(statement)
        ]

    def _snapshot(self, row: StockFinancialSnapshot) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": row.symbol,
            "report_date": row.report_date.isoformat(),
            "report_type": row.report_type,
            "version": row.version,
            "amount_revenue": _money(row.amount_revenue),
            "amount_net_profit": _money(row.amount_net_profit),
            "quality_status": row.quality_status,
            "confidence": row.confidence,
            "balance_check_result": row.balance_check_result,
            "fetch_time": _iso(row.fetch_time),
            "ingested_at": _iso(row.ingested_at),
            "event_id": row.event_id,
            "provider": row.provider,
        }
        if self.compliance_enabled:
            payload["compliance"] = {
                "tier": row.compliance_tier,
                "commercial_use": row.commercial_use,
            }
        return payload

    @staticmethod
    def _verify_rows(
        session: Session,
        *,
        symbol: str | None,
        report_date: date | None,
    ) -> list[StockFinancialSnapshot]:
        statement = select(StockFinancialSnapshot)
        if symbol is not None:
            statement = statement.where(StockFinancialSnapshot.symbol == symbol)
        if report_date is not None:
            statement = statement.where(StockFinancialSnapshot.report_date == report_date)
        statement = statement.order_by(
            StockFinancialSnapshot.symbol,
            StockFinancialSnapshot.report_date,
            StockFinancialSnapshot.report_type,
            StockFinancialSnapshot.version,
        ).limit(MAX_VERIFY)
        return list(session.scalars(statement))


__all__ = [
    "BACKFILL_DATA_TYPES",
    "DwdService",
    "MAX_BACKFILL",
    "MAX_VERIFY",
    "normalize_report_type",
    "parse_report_date",
]
