"""Query service: DWS metrics, DWD risk history and hybrid announcement search.

The four warehouse rules shape this module:

* only reads - no table here is ever written;
* upstream ``quality_status`` / ``confidence`` are passed through, never recomputed;
* history is read as-is (the ETL keeps every version and the newest one wins);
* semantic search is filter-first: ``symbol`` and ``ann_date`` are applied before
  the distance operator so HNSW never scans the whole table (``financecoffer.md``
  section 5, S-02).

Compliance columns are touched only when ``FC_COMPLIANCE_ENABLED`` is on
(DEC-0010); with the switch off they are not selected and no ``compliance`` key
appears in a response.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from financecoffer.core.compliance import ComplianceMark
from financecoffer.core.config import Config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import (
    DependencyError,
    InvalidArgumentError,
    NotFoundError,
)
from financecoffer.core.redaction import redact_text
from financecoffer.core.trace import normalize_trace_id
from financecoffer.db.connection import connect, resolve_dsn

STRUCTURED_METRICS: tuple[str, ...] = ("revenue", "net_profit", "roe", "pe", "pb")
MAX_YEAR_SPAN = 10
MAX_TOP_K = 50
MAX_RISK_LIMIT = 200
MAX_QUERY_CHARS = 2000

_SYMBOL = re.compile(r"^(?:[0-9]{6}|[A-Z][A-Z0-9.\-]{0,9})$")
_YEAR_RANGE = re.compile(r"^(\d{4})-(\d{4})$")
_YEAR_KEYS = ("year", "fiscal_year", "report_year", "fy")
_YEAR_CONTAINERS = ("metrics", "years", "annual", "fiscal_years")
_VALUE_KEYS = ("value", "amount", "ratio")


def normalize_symbol(value: str) -> str:
    """Return the canonical symbol or raise ``E_INVALID_ARGUMENT``.

    A-share codes are six digits; US tickers follow ``[A-Z][A-Z0-9.-]{0,9}``.
    Everything else (including empty input and SQL fragments) is rejected before
    a query is built.
    """

    symbol = (value or "").strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise InvalidArgumentError(
            "symbol must be a 6-digit A-share code or a US ticker",
            details={"field": "symbol"},
        )
    return symbol


def parse_year_range(value: str | None) -> tuple[int, int] | None:
    """Parse ``YYYY-YYYY`` into an inclusive year range of at most 10 years."""

    if value is None:
        return None
    match = _YEAR_RANGE.fullmatch(value.strip())
    if match is None:
        raise InvalidArgumentError(
            "year_range must be YYYY-YYYY",
            details={"field": "year_range", "value": value},
        )
    start, end = int(match.group(1)), int(match.group(2))
    if end < start:
        raise InvalidArgumentError(
            "year_range end must not be earlier than start",
            details={"field": "year_range", "value": value},
        )
    if end - start + 1 > MAX_YEAR_SPAN:
        raise InvalidArgumentError(
            f"year_range must span at most {MAX_YEAR_SPAN} years",
            details={"field": "year_range", "value": value},
        )
    return start, end


def parse_since(value: str | None) -> date | None:
    """Parse ``YYYY-MM-DD`` (the only accepted ``--since`` form)."""

    if value is None:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise InvalidArgumentError(
            "since must be YYYY-MM-DD",
            details={"field": "since", "value": value},
        ) from exc


def _bounded(value: int, *, field_name: str, low: int, high: int) -> int:
    if value < low or value > high:
        raise InvalidArgumentError(
            f"{field_name} must be between {low} and {high}",
            details={"field": field_name, "value": value},
        )
    return value


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _fetch_dicts(cursor: Any) -> list[dict[str, Any]]:
    names = [column[0] for column in (cursor.description or ())]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _unwrap(value: Any) -> Any:
    """Reduce ``{"value": x, "unit": ...}`` cells to their scalar value."""

    if isinstance(value, Mapping):
        for key in _VALUE_KEYS:
            if key in value:
                return value[key]
    return value


def _year_record(item: Mapping[str, Any]) -> dict[str, Any]:
    year: Any = None
    for key in _YEAR_KEYS:
        raw = item.get(key)
        if raw is not None and not isinstance(raw, (Mapping, list, tuple)):
            year = str(raw)
            break
    record: dict[str, Any] = {"year": year}
    for metric in STRUCTURED_METRICS:
        if metric in item:
            record[metric] = _unwrap(item[metric])
    return record


def _year_items(metrics_json: Any) -> list[dict[str, Any]]:
    """Normalize the documented ``metrics_json`` shapes into year records.

    The spec says the payload is a per-year array; the DDL requires a JSON
    object. Both are accepted, as is a year-keyed object, so the DWS builder can
    pick any of them without breaking the read path.
    """

    if isinstance(metrics_json, list):
        return [_year_record(item) for item in metrics_json if isinstance(item, Mapping)]
    if not isinstance(metrics_json, Mapping):
        return []
    for key in _YEAR_CONTAINERS:
        candidate = metrics_json.get(key)
        if isinstance(candidate, list):
            return [_year_record(item) for item in candidate if isinstance(item, Mapping)]
    return [
        _year_record({**value, "year": key})
        for key, value in metrics_json.items()
        if isinstance(value, Mapping)
    ]


def _year_number(record: Mapping[str, Any]) -> int | None:
    year = record.get("year")
    return int(year) if isinstance(year, str) and year.isdigit() else None


@dataclass(frozen=True)
class QueryResult:
    """A service response plus the envelope metadata that belongs with it."""

    data: dict[str, Any]
    source: str
    quality_status: str | None = None
    confidence: str | None = None
    compliance: ComplianceMark | None = None

    def meta(self, trace_id: str | None = None) -> Meta:
        meta = Meta(
            source=self.source,
            quality_status=self.quality_status,
            confidence=self.confidence,
            compliance_tier=self.compliance.tier if self.compliance else None,
        )
        if trace_id is not None:
            meta = meta.model_copy(update={"trace_id": normalize_trace_id(trace_id)})
        return meta


@dataclass(frozen=True)
class QueryService:
    """Read-only façade over the warehouse used by CLI/REST/MCP."""

    config: Config
    dsn: str | None = None
    timeout: int = 30
    connector: Callable[..., Any] = connect

    @property
    def compliance_enabled(self) -> bool:
        return self.config.compliance_enabled

    def _connect(self) -> Any:
        conn = self.connector(
            resolve_dsn(self.dsn or self.config.db_url), timeout=self.timeout
        )
        # ``connect_timeout`` only bounds connection establishment; without a
        # server-side ``statement_timeout`` a slow query (e.g. a vector scan)
        # would run unbounded (S-07). ``set_config`` accepts a bound parameter,
        # plain ``SET`` does not.
        if self.timeout > 0:
            conn.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(int(self.timeout) * 1000),),
            )
        return conn

    def ping(self) -> bool:
        """Return whether the warehouse answers ``SELECT 1`` (never raises)."""

        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - readiness must never crash the probe
            return False

    # -- structured ---------------------------------------------------------

    def structured(
        self,
        symbol: str,
        *,
        metric: str | None = None,
        year_range: str | None = None,
    ) -> QueryResult:
        normalized_symbol = normalize_symbol(symbol)
        normalized_metric = self._metric(metric)
        years = parse_year_range(year_range)

        columns = [
            "symbol",
            "as_of",
            "metrics_json",
            "risk_summary",
            "latest_announcements",
            "fiscal_years",
            "source_version",
            "built_at",
        ]
        if self.compliance_enabled:
            columns.extend(("compliance_tier", "commercial_use"))
        sql = (
            f"SELECT {', '.join(columns)} FROM dws.stock_agent_analysis_wide "
            "WHERE symbol = %s"
        )

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (normalized_symbol,))
            rows = _fetch_dicts(cur)

        if not rows:
            raise NotFoundError(
                "no pre-computed wide row for this symbol",
                details={"symbol": normalized_symbol, "hint": "run `fcoffer dws build`"},
            )
        row = rows[0]

        records = _year_items(row.get("metrics_json"))
        if years is not None:
            start, end = years
            records = [
                record
                for record in records
                if (number := _year_number(record)) is None or start <= number <= end
            ]
        records.sort(key=lambda record: (_year_number(record) is None, _year_number(record) or 0))

        data: dict[str, Any] = {
            "symbol": normalized_symbol,
            "as_of": _iso(row.get("as_of")),
            "built_at": _iso(row.get("built_at")),
            "fiscal_years": row.get("fiscal_years"),
            "source_version": row.get("source_version"),
            "metric": normalized_metric,
            "year_range": list(years) if years is not None else None,
            "years": records,
            "risk_summary": row.get("risk_summary"),
            "latest_announcements": row.get("latest_announcements"),
        }
        if normalized_metric is not None:
            data["series"] = [
                {"year": record.get("year"), "value": record.get(normalized_metric)}
                for record in records
            ]
        mark = self._mark_from_row(row)
        if self.compliance_enabled:
            data["compliance"] = mark.as_dict() if mark else None

        return QueryResult(data=data, source="dws", compliance=mark)

    # -- risk ---------------------------------------------------------------

    def risk(
        self,
        symbol: str,
        *,
        since: str | None = None,
        limit: int = 50,
    ) -> QueryResult:
        normalized_symbol = normalize_symbol(symbol)
        since_date = parse_since(since)
        bounded_limit = _bounded(limit, field_name="limit", low=1, high=MAX_RISK_LIMIT)

        columns = [
            "id",
            "symbol",
            "occur_date",
            "risk_type",
            "severity",
            "summary",
            "source_announcement_ref",
            "quality_status",
            "confidence",
            "fetch_time",
            "event_id",
            "provider",
        ]
        if self.compliance_enabled:
            columns.extend(("compliance_tier", "commercial_use"))

        params: list[Any] = [normalized_symbol]
        where = "symbol = %s"
        if since_date is not None:
            where += " AND occur_date >= %s"
            params.append(since_date)
        params.append(bounded_limit)
        sql = (
            f"SELECT {', '.join(columns)} FROM dwd.stock_risk_event "
            f"WHERE {where} ORDER BY occur_date DESC, id DESC LIMIT %s"
        )

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = _fetch_dicts(cur)

        events: list[dict[str, Any]] = []
        first_mark: ComplianceMark | None = None
        for row in rows:
            mark = self._mark_from_row(row)
            if first_mark is None:
                first_mark = mark
            event = {
                "id": row.get("id"),
                "symbol": row.get("symbol"),
                "occur_date": _iso(row.get("occur_date")),
                "risk_type": row.get("risk_type"),
                "severity": row.get("severity"),
                "summary": row.get("summary"),
                "source_announcement_ref": row.get("source_announcement_ref"),
                "quality_status": row.get("quality_status"),
                "confidence": row.get("confidence"),
                "fetch_time": _iso(row.get("fetch_time")),
                "event_id": row.get("event_id"),
                "provider": row.get("provider"),
            }
            if self.compliance_enabled:
                event["compliance"] = mark.as_dict() if mark else None
            events.append(event)

        data = {
            "symbol": normalized_symbol,
            "since": _iso(since_date),
            "count": len(events),
            "events": events,
        }
        return QueryResult(
            data=data,
            source="dwd",
            quality_status=rows[0].get("quality_status") if rows else None,
            confidence=rows[0].get("confidence") if rows else None,
            compliance=first_mark,
        )

    # -- announcements ------------------------------------------------------

    def search_announcements(
        self,
        symbol: str,
        query_text: str,
        *,
        year_range: str | None = None,
        top_k: int = 10,
    ) -> QueryResult:
        normalized_symbol = normalize_symbol(symbol)
        text = (query_text or "").strip()
        if not text:
            raise InvalidArgumentError(
                "query_text must not be empty",
                details={"field": "query_text"},
            )
        if len(text) > MAX_QUERY_CHARS:
            raise InvalidArgumentError(
                f"query_text must be at most {MAX_QUERY_CHARS} characters",
                details={"field": "query_text", "length": len(text)},
            )
        bounded_top_k = _bounded(top_k, field_name="top_k", low=1, high=MAX_TOP_K)
        years = parse_year_range(year_range)
        if years is None:
            last = date.today().year
            years = (last - 4, last)
        start_date, end_date = date(years[0], 1, 1), date(years[1], 12, 31)

        vector_literal = self._embed(text)

        columns = [
            "c.id",
            "c.symbol",
            "c.ann_date",
            "c.ann_type",
            "c.chunk_index",
            "c.chunk_text",
            "c.dwd_ref_id",
            "c.token_count",
            "c.embedding_model",
            "a.title",
            "a.minio_url",
            "a.quality_status",
            "a.confidence",
        ]
        if self.compliance_enabled:
            columns.extend(("a.compliance_tier", "a.commercial_use"))
        sql = (
            f"SELECT {', '.join(columns)}, "
            "1 - (c.embedding <=> %s::vector) AS score "
            "FROM vector.announcement_chunk AS c "
            "LEFT JOIN dwd.stock_announcement AS a ON a.id = c.dwd_ref_id "
            "WHERE c.symbol = %s AND c.ann_date >= %s AND c.ann_date <= %s "
            "ORDER BY c.embedding <=> %s::vector LIMIT %s"
        )
        params = (
            vector_literal,
            normalized_symbol,
            start_date,
            end_date,
            vector_literal,
            bounded_top_k,
        )

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = _fetch_dicts(cur)

        results: list[dict[str, Any]] = []
        first_mark: ComplianceMark | None = None
        for row in rows:
            mark = self._mark_from_row(row)
            if first_mark is None:
                first_mark = mark
            result = {
                "id": row.get("id"),
                "symbol": row.get("symbol"),
                "ann_date": _iso(row.get("ann_date")),
                "ann_type": row.get("ann_type"),
                "chunk_index": row.get("chunk_index"),
                "chunk_text": row.get("chunk_text"),
                "token_count": row.get("token_count"),
                "embedding_model": row.get("embedding_model"),
                "dwd_ref_id": row.get("dwd_ref_id"),
                "title": row.get("title"),
                "minio_url": row.get("minio_url"),
                "quality_status": row.get("quality_status"),
                "confidence": row.get("confidence"),
                "score": round(float(row["score"]), 6) if row.get("score") is not None else None,
            }
            if self.compliance_enabled:
                result["compliance"] = mark.as_dict() if mark else None
            results.append(result)

        data = {
            "symbol": normalized_symbol,
            "query": text,
            "year_range": [years[0], years[1]],
            "top_k": bounded_top_k,
            "count": len(results),
            "results": results,
        }
        return QueryResult(
            data=data,
            source="vector",
            quality_status=rows[0].get("quality_status") if rows else None,
            confidence=rows[0].get("confidence") if rows else None,
            compliance=first_mark,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _metric(metric: str | None) -> str | None:
        if metric is None:
            return None
        normalized = metric.strip().lower()
        if normalized not in STRUCTURED_METRICS:
            raise InvalidArgumentError(
                "metric must be one of: " + ", ".join(STRUCTURED_METRICS),
                details={"field": "metric", "value": metric},
            )
        return normalized

    def _mark_from_row(self, row: Mapping[str, Any]) -> ComplianceMark | None:
        if not self.compliance_enabled:
            return None
        tier = row.get("compliance_tier")
        if not tier:
            return None
        return ComplianceMark(
            tier=str(tier).strip().upper(),
            commercial_use=bool(row.get("commercial_use")),
        )

    def _embed(self, text: str) -> str:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise DependencyError(
                "httpx is required for semantic search",
                details={"hint": "install financecoffer[vector]"},
            ) from exc

        url = self.config.embedding_url
        payload = {"model": self.config.embedding_model, "input": [text]}
        headers: dict[str, str] = {}
        if self.config.embedding_api_key is not None:
            headers["Authorization"] = f"Bearer {self.config.embedding_api_key.get_secret_value()}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise DependencyError(
                "embedding service is unavailable",
                details={"url": url, "error": redact_text(str(exc).strip())},
            ) from exc
        except ValueError as exc:
            raise DependencyError(
                "embedding service returned invalid JSON",
                details={"url": url},
            ) from exc

        try:
            vector = body["data"][0]["embedding"]
            values = [float(item) for item in vector]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise DependencyError(
                "embedding service returned an unexpected payload",
                details={"url": url},
            ) from exc

        if len(values) != self.config.embedding_dim:
            raise DependencyError(
                "embedding dimension does not match FC_EMBEDDING_DIM",
                details={
                    "expected": self.config.embedding_dim,
                    "actual": len(values),
                    "hint": "align the embedding service with the vector(1024) column",
                },
            )
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm == 0.0:
            raise DependencyError(
                "embedding service returned a zero or non-finite vector",
                details={"url": url},
            )
        return "[" + ",".join(f"{value / norm:.9g}" for value in values) + "]"


__all__ = [
    "MAX_QUERY_CHARS",
    "MAX_RISK_LIMIT",
    "MAX_TOP_K",
    "MAX_YEAR_SPAN",
    "STRUCTURED_METRICS",
    "QueryResult",
    "QueryService",
    "normalize_symbol",
    "parse_since",
    "parse_year_range",
]
