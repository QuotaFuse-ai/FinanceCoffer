"""Read and replay operations over the immutable ODS layer.

``list`` / ``show`` / ``stats`` are reads. ``replay`` is a status reset, not a
rewrite and not a re-publish: the original ODS row is never changed; the
matching ``ops.etl_event_log`` status is reset to ``received`` so a later
redelivery of the same event is processed instead of skipped as ``done``. It
does not push anything into the Redis stream. ``--dry-run`` builds the same
manifest without touching the event log.

Time filters accept ``YYYY-MM-DD`` (UTC midnight; a date-only ``--until`` means
the whole day is included) or an RFC 3339 timestamp that must carry a timezone.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from financecoffer.core.compliance import ComplianceMark
from financecoffer.core.config import Config
from financecoffer.core.errors import ConfigError, InvalidArgumentError, NotFoundError
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.repositories.ods import RawDataRepository
from financecoffer.db.repositories.ops import EventLogRepository
from financecoffer.db.session import Database
from financecoffer.events.models import EventDataType
from financecoffer.query import normalize_symbol

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_REPLAY = 1000
ETL_STATUSES: tuple[str, ...] = ("pending", "done", "failed")
GROUP_BYS: tuple[str, ...] = ("data_type", "source", "etl_status")

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bounded(value: int, *, field_name: str, low: int, high: int) -> int:
    if value < low or value > high:
        raise InvalidArgumentError(
            f"{field_name} must be between {low} and {high}",
            details={"field": field_name, "value": value},
        )
    return value


def parse_instant(
    value: str | None,
    *,
    field_name: str,
    end_of_day: bool = False,
) -> datetime | None:
    """Parse a CLI time filter into a UTC datetime.

    ``end_of_day`` turns a date-only value into the next midnight, so the named
    day itself is inside the half-open ``[since, until)`` window.
    """

    if value is None:
        return None
    cleaned = value.strip()
    if _DATE_ONLY.fullmatch(cleaned):
        try:
            day = date.fromisoformat(cleaned)
        except ValueError as exc:
            raise InvalidArgumentError(
                f"{field_name} must be YYYY-MM-DD or an RFC 3339 timestamp",
                details={"field": field_name, "value": value},
            ) from exc
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return start + timedelta(days=1) if end_of_day else start
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidArgumentError(
            f"{field_name} must be YYYY-MM-DD or an RFC 3339 timestamp",
            details={"field": field_name, "value": value},
        ) from exc
    if parsed.tzinfo is None:
        raise InvalidArgumentError(
            f"{field_name} must include a timezone",
            details={"field": field_name, "value": value},
        )
    return parsed.astimezone(UTC)


def normalize_data_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in {item.value for item in EventDataType}:
        raise InvalidArgumentError(
            "data_type must be one of: " + ", ".join(item.value for item in EventDataType),
            details={"field": "data_type", "value": value},
        )
    return normalized


def normalize_etl_status(value: str | None) -> str | None:
    """Validate ``--etl-status`` against the DDL's check constraint values."""

    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in ETL_STATUSES:
        raise InvalidArgumentError(
            "etl_status must be one of: " + ", ".join(ETL_STATUSES),
            details={"field": "etl_status", "value": value},
        )
    return normalized


def normalize_group_by(value: str | None) -> str:
    if value is None:
        return GROUP_BYS[0]
    normalized = value.strip().lower()
    if normalized not in GROUP_BYS:
        raise InvalidArgumentError(
            "group_by must be one of: " + ", ".join(GROUP_BYS),
            details={"field": "group_by", "value": value},
        )
    return normalized


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _summary(row: StockRawData) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "data_type": row.data_type,
        "symbol": row.symbol,
        "market": row.market,
        "source": row.source,
        "biz_key": row.biz_key,
        "fetch_time": _iso(row.fetch_time),
        "ingested_at": _iso(row.ingested_at),
        "etl_status": row.etl_status,
    }


def _detail(row: StockRawData, *, compliance_enabled: bool) -> dict[str, Any]:
    data = _summary(row)
    data.update(
        {
            "request_params": row.request_params,
            "raw_json": row.raw_json,
            "has_raw_text": row.raw_text is not None,
            "raw_file_path": row.raw_file_path,
        }
    )
    if compliance_enabled:
        mark = None
        if row.compliance_tier:
            mark = ComplianceMark(
                tier=str(row.compliance_tier),
                commercial_use=bool(row.commercial_use),
            )
        data["compliance"] = mark.as_dict() if mark else None
    return data


def _manifest(row: StockRawData) -> dict[str, Any]:
    """The replay manifest entry printed by ``ods replay --dry-run``."""

    return {
        "id": row.id,
        "event_id": row.event_id,
        "data_type": row.data_type,
        "symbol": row.symbol,
        "source": row.source,
        "fetch_time": _iso(row.fetch_time),
        "etl_status": row.etl_status,
    }


@dataclass(frozen=True)
class OdsService:
    """Facade over ODS reads and event-log status resets."""

    config: Config
    dsn: str | None = None
    database_factory: Callable[[str], Database] = Database

    @property
    def compliance_enabled(self) -> bool:
        return self.config.compliance_enabled

    @contextmanager
    def _session(self) -> Iterator[Session]:
        database = self._connect()
        try:
            with database.session_scope() as session:
                yield session
        finally:
            database.dispose()

    def _connect(self) -> Database:
        dsn = self.dsn or self.config.db_url
        if not dsn:
            raise ConfigError(
                "database URL is not configured",
                details={"hint": "set FC_DB_URL or pass --db-url"},
            )
        return self.database_factory(dsn)

    def listing(
        self,
        *,
        data_type: str | None = None,
        symbol: str | None = None,
        since: str | None = None,
        until: str | None = None,
        etl_status: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Newest-first records inside an optional time window."""

        normalized_type = normalize_data_type(data_type)
        normalized_status = normalize_etl_status(etl_status)
        normalized_symbol = normalize_symbol(symbol) if symbol is not None else None
        since_at = parse_instant(since, field_name="since")
        until_at = parse_instant(until, field_name="until", end_of_day=True)
        if since_at is not None and until_at is not None and until_at <= since_at:
            raise InvalidArgumentError(
                "until must be later than since",
                details={"field": "until", "value": until},
            )
        bounded_limit = _bounded(limit, field_name="limit", low=1, high=MAX_LIMIT)

        with self._session() as session:
            rows = RawDataRepository(session).search(
                since=since_at,
                until=until_at,
                data_type=normalized_type,
                symbol=normalized_symbol,
                etl_status=normalized_status,
                limit=bounded_limit,
            )
            records = [_summary(row) for row in rows]

        return {
            "data_type": normalized_type,
            "symbol": normalized_symbol,
            "since": _iso(since_at),
            "until": _iso(until_at),
            "etl_status": normalized_status,
            "limit": bounded_limit,
            "count": len(records),
            "records": records,
        }

    def show(self, record_id: int) -> dict[str, Any]:
        with self._session() as session:
            row = self._get(session, record_id)
            return _detail(row, compliance_enabled=self.compliance_enabled)

    def raw_text(self, record_id: int) -> str:
        """Return the exact upstream document for ``--raw``.

        Rows landed by ``ingest event`` always carry ``raw_text``. Older rows
        written through ``raw_json``/``raw_file_path`` have no original text;
        they fall back to compact JSON of the parsed payload.
        """

        with self._session() as session:
            row = self._get(session, record_id)
            if row.raw_text is not None:
                return row.raw_text
            if row.raw_json is None:
                raise NotFoundError(
                    "record has no inline payload",
                    details={"id": record_id, "hint": "the payload lives in raw_file_path"},
                )
            return json.dumps(row.raw_json, ensure_ascii=False, separators=(",", ":"))

    def stats(self, *, group_by: str | None = None) -> dict[str, Any]:
        column = normalize_group_by(group_by)
        with self._session() as session:
            counts = RawDataRepository(session).statistics(column)
        return {
            "group_by": column,
            "groups": [{"value": value, "count": count} for value, count in counts],
            "total": sum(count for _, count in counts),
        }

    def replay(
        self,
        *,
        record_ids: Sequence[int] = (),
        all_failed: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reset event-log state for ODS records without modifying them.

        A real replay resets ``ops.etl_event_log.status`` to ``received``; it
        never touches ``ods.stock_raw_data``. ``dry_run`` only builds the
        manifest and leaves every row, including the event log, untouched.
        """

        if bool(record_ids) == bool(all_failed):
            raise InvalidArgumentError(
                "pass either a record id or --all-failed",
                details={"field": "id"},
            )

        with self._session() as session:
            repository = RawDataRepository(session)
            if all_failed:
                rows: list[StockRawData] = list(repository.failed(limit=MAX_REPLAY))
            else:
                rows = []
                missing: list[int] = []
                for record_id in record_ids:
                    row = repository.by_id(record_id)
                    if row is None:
                        missing.append(record_id)
                    else:
                        rows.append(row)
                if missing:
                    raise NotFoundError(
                        "ODS record not found",
                        details={"ids": missing, "hint": "run `fcoffer ods list`"},
                    )

            manifest = [_manifest(row) for row in rows]
            truncated = bool(all_failed and len(rows) == MAX_REPLAY)
            if dry_run:
                return {
                    "dry_run": True,
                    "count": len(manifest),
                    "truncated": truncated,
                    "records": manifest,
                }

            event_log = EventLogRepository(session)
            requeued = 0
            unmatched: list[str] = []
            for row in rows:
                if event_log.requeue(row.event_id) is not None:
                    requeued += 1
                else:
                    unmatched.append(row.event_id)

        return {
            "dry_run": False,
            "count": len(manifest),
            "truncated": truncated,
            "requeued": requeued,
            "unmatched": unmatched,
            "records": manifest,
        }

    @staticmethod
    def _get(session: Session, record_id: int) -> StockRawData:
        row = RawDataRepository(session).by_id(record_id)
        if row is None:
            raise NotFoundError(
                "ODS record not found",
                details={"id": record_id, "hint": "run `fcoffer ods list`"},
            )
        return row


__all__ = [
    "DEFAULT_LIMIT",
    "ETL_STATUSES",
    "GROUP_BYS",
    "MAX_LIMIT",
    "MAX_REPLAY",
    "OdsService",
    "normalize_data_type",
    "normalize_etl_status",
    "normalize_group_by",
    "parse_instant",
]
