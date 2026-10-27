"""Object layout, cold-row decisions and canonical row exports.

The archive keeps one object per DWD row. Keys are deterministic so a rerun
lands on the same object: ``<prefix>/dwd/<table>/<symbol>/<year>/<leaf>``.
Announcement objects store the original body bytes; versioned facts and risk
events store the full row as canonical JSON.

Nothing here touches S3 or PostgreSQL, which keeps the decision rules and key
layout unit-testable in isolation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from financecoffer.core.errors import StorageError
from financecoffer.db.models.dwd import (
    StockAnnouncement,
    StockFinancialSnapshot,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)

Kind = Literal["announcement", "versioned", "risk"]

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"


@dataclass(frozen=True, slots=True)
class TableSpec:
    """What the archive needs to know about one DWD table."""

    name: str
    model: Any
    kind: Kind
    date_column: str
    pk_columns: tuple[str, ...]
    big_column: str | None = None
    big_is_json: bool = False

    @property
    def date_field(self) -> Any:
        return getattr(self.model, self.date_column)

    @property
    def pk_fields(self) -> tuple[Any, ...]:
        return tuple(getattr(self.model, name) for name in self.pk_columns)

    @property
    def content_type(self) -> str:
        return TEXT_CONTENT_TYPE if self.kind == "announcement" else JSON_CONTENT_TYPE

    def row_dict(self, row: Any) -> dict[str, Any]:
        return {column.key: getattr(row, column.key) for column in self.model.__table__.columns}


TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        name="stock_announcement",
        model=StockAnnouncement,
        kind="announcement",
        date_column="ann_date",
        pk_columns=("id",),
        big_column="clean_content",
    ),
    TableSpec(
        name="stock_financial_snapshot",
        model=StockFinancialSnapshot,
        kind="versioned",
        date_column="report_date",
        pk_columns=("symbol", "report_date", "report_type", "version"),
        big_column="balance_check_result",
        big_is_json=True,
    ),
    TableSpec(
        name="stock_kline",
        model=StockKline,
        kind="versioned",
        date_column="trade_date",
        pk_columns=("symbol", "trade_date", "adjust", "version"),
    ),
    TableSpec(
        name="stock_quote_snapshot",
        model=StockQuoteSnapshot,
        kind="versioned",
        date_column="trade_date",
        pk_columns=("symbol", "trade_date", "version"),
    ),
    TableSpec(
        name="stock_risk_event",
        model=StockRiskEvent,
        kind="risk",
        date_column="occur_date",
        pk_columns=("id",),
        big_column="summary",
    ),
)


@dataclass(frozen=True, slots=True)
class ArchiveCandidate:
    """One row ready to upload, plus the marker that will go back to PG."""

    spec: TableSpec
    primary_key: dict[str, Any]
    symbol: str
    reference_date: date
    object_key: str
    minio_url: str
    payload: bytes
    expected_sha256: str
    integrity_error: str | None = None

    @property
    def content_type(self) -> str:
        return self.spec.content_type


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def years_ago(today: date, years: int) -> date:
    """Return ``today`` minus ``years``; Feb 29 falls back to Feb 28."""

    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def cold_cutoff(today: date, older_than_years: int) -> date:
    """Rows dated strictly before this cutoff are cold by age."""

    return years_ago(today, older_than_years)


def _segment(value: Any) -> str:
    text = str(value).strip()
    for bad in ("/", "\\", "\x00"):
        text = text.replace(bad, "_")
    return text or "unknown"


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def plain_mapping(values: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe copy of a primary key for failures and object metadata."""

    return {key: _plain(value) for key, value in values.items()}


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def canonical_json(values: dict[str, Any]) -> bytes:
    return json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def minio_url(bucket: str, key: str) -> str:
    return f"minio://{bucket}/{key}"


def key_from_url(url: str, bucket: str) -> str:
    """Reverse :func:`minio_url`; the bucket must be the configured one."""

    prefixes = (f"minio://{bucket}/", f"s3://{bucket}/")
    for prefix in prefixes:
        if url.startswith(prefix):
            return url[len(prefix) :]
    raise StorageError(
        "minio_url does not point at the configured bucket",
        details={"minio_url": url, "bucket": bucket},
    )


def object_key(spec: TableSpec, row: Any, *, prefix: str = "") -> str:
    """Build the stable key for one row (table + symbol + year + primary key)."""

    row_date = getattr(row, spec.date_column)
    leaf: str
    if spec.name == "stock_announcement":
        leaf = f"{_segment(row.id)}.txt"
    elif spec.name == "stock_financial_snapshot":
        leaf = (
            f"{_segment(row.report_date)}_{_segment(row.report_type)}"
            f"_{_segment(row.version)}.json"
        )
    elif spec.name == "stock_kline":
        leaf = (
            f"{_segment(row.trade_date)}_{_segment(row.adjust)}"
            f"_{_segment(row.version)}.json"
        )
    elif spec.name == "stock_quote_snapshot":
        leaf = f"{_segment(row.trade_date)}_{_segment(row.version)}.json"
    else:
        leaf = f"{_segment(row.id)}.json"

    parts = [
        prefix.strip("/"),
        "dwd",
        spec.name,
        _segment(row.symbol),
        str(row_date.year),
        leaf,
    ]
    return "/".join(part for part in parts if part)


def build_candidate(
    spec: TableSpec,
    row: Any,
    *,
    bucket: str,
    prefix: str = "",
) -> ArchiveCandidate:
    """Turn a row into the exact bytes that go to MinIO and their checksum."""

    key = object_key(spec, row, prefix=prefix)
    url = minio_url(bucket, key)
    primary_key = {name: getattr(row, name) for name in spec.pk_columns}
    error: str | None = None

    if spec.kind == "announcement":
        body = row.clean_content
        if body is None:
            raise StorageError(
                "announcement has no body to archive",
                details={"table": spec.name, "primary_key": plain_mapping(primary_key)},
            )
        payload = body.encode("utf-8")
        expected = sha256_hex(payload)
        if expected != row.content_hash:
            error = "content_hash does not match the stored body"
    else:
        values = spec.row_dict(row)
        values["is_cold"] = True
        values["minio_url"] = url
        payload = canonical_json(values)
        expected = sha256_hex(payload)

    return ArchiveCandidate(
        spec=spec,
        primary_key=primary_key,
        symbol=row.symbol,
        reference_date=getattr(row, spec.date_column),
        object_key=key,
        minio_url=url,
        payload=payload,
        expected_sha256=expected,
        integrity_error=error,
    )
