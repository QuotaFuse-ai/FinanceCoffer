from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from financecoffer.core.config import Config
from financecoffer.core.errors import DependencyError, InvalidArgumentError, NotFoundError
from financecoffer.query.service import (
    MAX_QUERY_CHARS,
    MAX_RISK_LIMIT,
    MAX_TOP_K,
    QueryService,
    normalize_symbol,
    parse_since,
    parse_year_range,
)

DSN = "postgresql://fcoffer:secret@127.0.0.1:5432/financecoffer"

STRUCTURED_COLUMNS = [
    "symbol",
    "as_of",
    "metrics_json",
    "risk_summary",
    "latest_announcements",
    "fiscal_years",
    "source_version",
    "built_at",
]
RISK_COLUMNS = [
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
SEARCH_COLUMNS = [
    "id",
    "symbol",
    "ann_date",
    "ann_type",
    "chunk_index",
    "chunk_text",
    "dwd_ref_id",
    "token_count",
    "embedding_model",
    "title",
    "minio_url",
    "quality_status",
    "confidence",
    "score",
]


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]], columns: list[str]) -> None:
        self._rows = rows
        self.description = [(name,) for name in columns]
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, tuple(params)))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def cursor(self) -> _Cursor:
        return self._cursor

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, tuple(params)))


class FakeDB:
    """Minimal psycopg stand-in: records SQL/params and replays fixed rows."""

    def __init__(self, rows: list[tuple[Any, ...]], columns: list[str]) -> None:
        self.cursor = _Cursor(rows, columns)
        self.dsn: str | None = None
        self.timeout: int | None = None
        self.calls = 0
        self.conn: _Conn | None = None

    def __call__(self, dsn: str, *, timeout: int) -> _Conn:
        self.calls += 1
        self.dsn = dsn
        self.timeout = timeout
        self.conn = _Conn(self.cursor)
        return self.conn


def _service(
    rows: list[tuple[Any, ...]],
    columns: list[str],
    *,
    enabled: bool = False,
) -> tuple[QueryService, FakeDB]:
    db = FakeDB(rows, columns)
    config = Config(db_url=DSN, compliance_enabled=enabled)
    return QueryService(config=config, connector=db), db


# -- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("600000", "600000"), (" aapl ", "AAPL"), ("brk-b", "BRK-B"), ("brk.b", "BRK.B")],
)
def test_normalize_symbol_accepts_documented_forms(raw: str, expected: str) -> None:
    assert normalize_symbol(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "12345",
        "1234567",
        "600001;DROP TABLE",
        "AA PL",
        "A" * 12,
        "0700.HK",
        "１１１１１１",
    ],
)
def test_normalize_symbol_rejects_everything_else(raw: str) -> None:
    with pytest.raises(InvalidArgumentError) as excinfo:
        normalize_symbol(raw)

    assert excinfo.value.details == {"field": "symbol"}


def test_year_range_parsing_and_limits() -> None:
    assert parse_year_range(None) is None
    assert parse_year_range("2021-2025") == (2021, 2025)
    assert parse_year_range("2025-2025") == (2025, 2025)

    with pytest.raises(InvalidArgumentError, match="YYYY-YYYY"):
        parse_year_range("2021/2025")
    with pytest.raises(InvalidArgumentError, match="earlier"):
        parse_year_range("2025-2021")
    with pytest.raises(InvalidArgumentError, match="at most 10"):
        parse_year_range("2010-2025")


def test_since_parsing() -> None:
    assert parse_since(None) is None
    assert parse_since("2024-06-01") == date(2024, 6, 1)

    with pytest.raises(InvalidArgumentError, match="YYYY-MM-DD"):
        parse_since("2024/06/01")


# -- structured --------------------------------------------------------------


def _structured_row(metrics_json: Any, **extra: Any) -> tuple[Any, ...]:
    row: dict[str, Any] = {
        "symbol": "600000",
        "as_of": date(2026, 9, 30),
        "metrics_json": metrics_json,
        "risk_summary": {"high": 1},
        "latest_announcements": [],
        "fiscal_years": [2023, 2024],
        "source_version": 3,
        "built_at": datetime(2026, 10, 1, 12, 0, tzinfo=None),
    }
    row.update(extra)
    return tuple(row.values())


def test_structured_off_does_not_touch_compliance_columns() -> None:
    service, db = _service([_structured_row([])], STRUCTURED_COLUMNS)

    result = service.structured("600000")

    sql, params = db.cursor.executed[0]
    assert "compliance" not in sql
    assert params == ("600000",)
    assert "compliance" not in result.data
    assert result.data["symbol"] == "600000"
    assert result.data["as_of"] == "2026-09-30"
    assert result.meta().compliance_tier is None
    assert result.meta().source == "dws"


@pytest.mark.parametrize(
    ("metrics_json", "expected"),
    [
        pytest.param(
            [
                {"year": 2024, "revenue": {"value": 1200.0}},
                {"year": 2023, "revenue": 1000.0},
            ],
            [(2023, 1000.0), (2024, 1200.0)],
            id="list",
        ),
        pytest.param(
            {"metrics": [{"fiscal_year": "2024", "revenue": {"amount": 1200.0}}]},
            [(2024, 1200.0)],
            id="metrics-container",
        ),
        pytest.param(
            {"years": [{"fy": 2023, "revenue": 1000.0}, {"fy": 2024, "revenue": 1200.0}]},
            [(2023, 1000.0), (2024, 1200.0)],
            id="years-container",
        ),
        pytest.param(
            {"2024": {"revenue": 1200.0}, "2023": {"revenue": 1000.0}},
            [(2023, 1000.0), (2024, 1200.0)],
            id="year-keyed-object",
        ),
    ],
)
def test_structured_accepts_every_documented_metrics_shape(
    metrics_json: Any, expected: list[tuple[int, float]]
) -> None:
    service, _ = _service([_structured_row(metrics_json)], STRUCTURED_COLUMNS)

    years = service.structured("600000").data["years"]

    assert [(int(record["year"]), record["revenue"]) for record in years] == expected


def test_structured_filters_and_projects_a_metric() -> None:
    metrics = [{"year": year, "roe": float(year - 2000)} for year in range(2018, 2026)]
    service, _ = _service([_structured_row(metrics)], STRUCTURED_COLUMNS)

    result = service.structured("600000", metric="ROE", year_range="2023-2025")

    assert result.data["metric"] == "roe"
    assert result.data["year_range"] == [2023, 2025]
    assert [record["year"] for record in result.data["years"]] == ["2023", "2024", "2025"]
    assert result.data["series"] == [
        {"year": "2023", "value": 23.0},
        {"year": "2024", "value": 24.0},
        {"year": "2025", "value": 25.0},
    ]


def test_structured_enabled_selects_and_returns_the_mark() -> None:
    columns = STRUCTURED_COLUMNS + ["compliance_tier", "commercial_use"]
    row = _structured_row([], compliance_tier="C", commercial_use=False)
    service, db = _service([row], columns, enabled=True)

    result = service.structured("600000")

    sql, _ = db.cursor.executed[0]
    assert "compliance_tier" in sql
    assert result.data["compliance"] == {"compliance_tier": "C", "commercial_use": False}
    assert result.meta().compliance_tier == "C"


def test_structured_enabled_tolerates_rows_ingested_while_off() -> None:
    columns = STRUCTURED_COLUMNS + ["compliance_tier", "commercial_use"]
    row = _structured_row([], compliance_tier=None, commercial_use=None)
    service, _ = _service([row], columns, enabled=True)

    result = service.structured("600000")

    assert result.data["compliance"] is None
    assert result.meta().compliance_tier is None


def test_structured_missing_symbol_is_not_found() -> None:
    service, _ = _service([], STRUCTURED_COLUMNS)

    with pytest.raises(NotFoundError) as excinfo:
        service.structured("600000")

    assert excinfo.value.details is not None
    assert excinfo.value.details["symbol"] == "600000"


def test_structured_rejects_unknown_metric_before_connecting() -> None:
    service, db = _service([], STRUCTURED_COLUMNS)

    with pytest.raises(InvalidArgumentError, match="metric must be one of"):
        service.structured("600000", metric="ebitda")

    assert db.calls == 0


# -- risk --------------------------------------------------------------------


def _risk_row(**extra: Any) -> tuple[Any, ...]:
    row: dict[str, Any] = {
        "id": 7,
        "symbol": "600000",
        "occur_date": date(2025, 4, 1),
        "risk_type": "litigation",
        "severity": "high",
        "summary": "major lawsuit",
        "source_announcement_ref": 11,
        "quality_status": "normal",
        "confidence": "high",
        "fetch_time": datetime(2025, 4, 2, 9, 30),
        "event_id": "evt_abc",
        "provider": "eastmoney",
    }
    row.update(extra)
    return tuple(row.values())


def test_risk_applies_since_and_limit_in_sql() -> None:
    service, db = _service([_risk_row()], RISK_COLUMNS)

    result = service.risk("600000", since="2025-01-01", limit=20)

    sql, params = db.cursor.executed[0]
    assert "occur_date >= %s" in sql
    assert "ORDER BY occur_date DESC, id DESC LIMIT %s" in sql
    assert params == ("600000", date(2025, 1, 1), 20)
    assert result.data["count"] == 1
    assert result.data["since"] == "2025-01-01"
    event = result.data["events"][0]
    assert event["occur_date"] == "2025-04-01"
    assert event["severity"] == "high"
    assert "compliance" not in event
    assert result.meta().quality_status == "normal"


def test_risk_enabled_marks_each_event() -> None:
    columns = RISK_COLUMNS + ["compliance_tier", "commercial_use"]
    rows = [
        _risk_row(compliance_tier="B", commercial_use=True),
        _risk_row(id=6, compliance_tier=None, commercial_use=None),
    ]
    service, _ = _service(rows, columns, enabled=True)

    result = service.risk("600000")

    assert [event["compliance"] for event in result.data["events"]] == [
        {"compliance_tier": "B", "commercial_use": True},
        None,
    ]
    assert result.meta().compliance_tier == "B"


def test_risk_rejects_limits_outside_the_contract() -> None:
    service, _ = _service([], RISK_COLUMNS)

    for limit in (0, MAX_RISK_LIMIT + 1):
        with pytest.raises(InvalidArgumentError, match="limit must be between"):
            service.risk("600000", limit=limit)


# -- announcement search -----------------------------------------------------


def _search_row(score: float = 0.93, **extra: Any) -> tuple[Any, ...]:
    row: dict[str, Any] = {
        "id": 1,
        "symbol": "600000",
        "ann_date": date(2025, 4, 1),
        "ann_type": "annual_report",
        "chunk_index": 0,
        "chunk_text": "revenue grew 12%",
        "dwd_ref_id": 11,
        "token_count": 128,
        "embedding_model": "BAAI/bge-m3",
        "title": "2024 annual report",
        "minio_url": "minio://financecoffer/a.pdf",
        "quality_status": "normal",
        "confidence": "high",
        "score": score,
    }
    row.update(extra)
    return tuple(row.values())


def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        QueryService,
        "_embed",
        lambda self, text: "[0.1,0.2]",
    )


def test_search_is_filter_first_and_defaults_to_five_years(monkeypatch) -> None:
    _stub_embedding(monkeypatch)
    service, db = _service([_search_row()], SEARCH_COLUMNS)

    result = service.search_announcements("600000", "revenue growth", top_k=5)

    sql, params = db.cursor.executed[0]
    assert sql.index("WHERE c.symbol = %s") < sql.index("ORDER BY")
    assert "LEFT JOIN dwd.stock_announcement" in sql
    this_year = date.today().year
    assert params == (
        "[0.1,0.2]",
        "600000",
        date(this_year - 4, 1, 1),
        date(this_year, 12, 31),
        "[0.1,0.2]",
        5,
    )
    assert result.data["year_range"] == [this_year - 4, this_year]
    assert result.data["top_k"] == 5
    assert result.data["count"] == 1
    assert result.data["results"][0]["score"] == 0.93
    assert result.meta().source == "vector"


def test_search_honours_year_range_and_compliance(monkeypatch) -> None:
    _stub_embedding(monkeypatch)
    columns = SEARCH_COLUMNS + ["compliance_tier", "commercial_use"]
    row = _search_row(compliance_tier="D", commercial_use=False)
    service, db = _service([row], columns, enabled=True)

    result = service.search_announcements(
        "600000", "revenue growth", year_range="2023-2024", top_k=3
    )

    sql, params = db.cursor.executed[0]
    assert "a.compliance_tier" in sql
    assert params[2:4] == (date(2023, 1, 1), date(2024, 12, 31))
    hit = result.data["results"][0]
    assert hit["compliance"] == {"compliance_tier": "D", "commercial_use": False}
    assert result.meta().compliance_tier == "D"


@pytest.mark.parametrize("query_text", ["", "   ", "x" * (MAX_QUERY_CHARS + 1)])
def test_search_rejects_empty_or_oversized_queries(monkeypatch, query_text: str) -> None:
    _stub_embedding(monkeypatch)
    service, db = _service([], SEARCH_COLUMNS)

    with pytest.raises(InvalidArgumentError, match="query_text"):
        service.search_announcements("600000", query_text)

    assert db.calls == 0


def test_search_rejects_top_k_outside_the_contract(monkeypatch) -> None:
    _stub_embedding(monkeypatch)
    service, db = _service([], SEARCH_COLUMNS)

    with pytest.raises(InvalidArgumentError, match="top_k must be between"):
        service.search_announcements("600000", "revenue", top_k=MAX_TOP_K + 1)

    assert db.calls == 0


# -- embedding client --------------------------------------------------------


class _EmbeddingResponse:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"data": [{"embedding": self._vector}]}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8001/v1/embeddings", False),
        ("http://localhost:8001/v1/embeddings", False),
        ("https://api.openai.com/v1/embeddings", True),
    ],
)
def test_embed_bypasses_the_system_proxy_for_loopback(
    monkeypatch: pytest.MonkeyPatch, url: str, expected: bool
) -> None:
    import httpx

    seen: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc_info: Any) -> bool:
            return False

        def post(self, *args: Any, **kwargs: Any) -> _EmbeddingResponse:
            return _EmbeddingResponse([0.0] * 1023 + [1.0])

    monkeypatch.setattr(httpx, "Client", FakeClient)
    service = QueryService(
        config=Config(db_url=DSN, embedding_url=url),
        connector=FakeDB([], STRUCTURED_COLUMNS),
    )

    vector = service._embed("revenue")

    assert seen["trust_env"] is expected
    assert vector.count(",") == 1023


# -- statement timeout -------------------------------------------------------


def test_connect_enforces_a_server_side_statement_timeout() -> None:
    service, db = _service([_structured_row([])], STRUCTURED_COLUMNS)

    service.structured("600000")

    assert db.conn is not None
    sql, params = db.conn.executed[0]
    assert "set_config('statement_timeout'" in sql
    assert params == ("30000",)


def test_connect_converts_the_timeout_to_milliseconds() -> None:
    db = FakeDB([_structured_row([])], STRUCTURED_COLUMNS)
    service = QueryService(
        config=Config(db_url=DSN), timeout=7, connector=db
    )

    service.risk("600000")

    assert db.conn is not None
    assert db.conn.executed[0][1] == ("7000",)


def test_connect_can_disable_the_statement_timeout() -> None:
    db = FakeDB([_structured_row([])], STRUCTURED_COLUMNS)
    service = QueryService(config=Config(db_url=DSN), timeout=0, connector=db)

    service.structured("600000")

    assert db.conn is not None
    assert db.conn.executed == []


# -- readiness ---------------------------------------------------------------


class FailingConnector:
    def __call__(self, dsn: str, *, timeout: int) -> Any:
        raise DependencyError("cannot connect to PostgreSQL")


def test_ping_never_raises() -> None:
    healthy, _ = _service([], STRUCTURED_COLUMNS)
    assert healthy.ping() is True

    broken = QueryService(
        config=Config(db_url=DSN),
        connector=FailingConnector(),  # type: ignore[arg-type]
    )
    assert broken.ping() is False


# -- business metrics --------------------------------------------------------


class _MetricsCursor:
    """Answers each roll-up in ``business_metrics`` with rows of its own."""

    def __init__(self, pending: tuple[int, float | None]) -> None:
        self._pending = pending
        self.executed: list[str] = []
        self._last = ""

    def __enter__(self) -> _MetricsCursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append(sql)
        self._last = sql

    def fetchall(self) -> list[tuple[Any, ...]]:
        if "etl_dlq" in self._last:
            return [("event_id", 1)]
        return []

    def fetchone(self) -> tuple[int, float | None]:
        return self._pending


class _MetricsConnector:
    def __init__(self, cursor: _MetricsCursor) -> None:
        self._cursor = cursor

    def __call__(self, dsn: str, *, timeout: int) -> _MetricsConnection:
        return _MetricsConnection(self._cursor)


class _MetricsConnection:
    def __init__(self, cursor: _MetricsCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _MetricsConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def cursor(self) -> _MetricsCursor:
        return self._cursor

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        pass


def test_consume_lag_measures_queue_wait_not_publisher_time() -> None:
    """``event_time`` is the publisher's business clock: a legitimate backfill of
    an old report would read as days of lag the moment it is queued. The gauge
    therefore has to follow ``received_at``."""

    cursor = _MetricsCursor((1, 12.5))
    service = QueryService(
        config=Config(db_url=DSN),
        connector=_MetricsConnector(cursor),  # type: ignore[arg-type]
    )

    metrics = service.business_metrics()

    assert metrics is not None
    assert metrics.consume_lag_seconds == 12.5
    lag_sql = next(sql for sql in cursor.executed if "min(" in sql)
    assert "min(received_at)" in lag_sql
    assert "event_time" not in lag_sql
    assert "'received'" in lag_sql and "'processing'" in lag_sql


def test_consume_lag_is_zero_when_nothing_is_queued() -> None:
    cursor = _MetricsCursor((0, None))
    service = QueryService(
        config=Config(db_url=DSN),
        connector=_MetricsConnector(cursor),  # type: ignore[arg-type]
    )

    metrics = service.business_metrics()

    assert metrics is not None
    assert metrics.consume_lag_seconds == 0.0


def test_consume_lag_is_omitted_when_the_wait_cannot_be_measured() -> None:
    cursor = _MetricsCursor((2, None))
    service = QueryService(
        config=Config(db_url=DSN),
        connector=_MetricsConnector(cursor),  # type: ignore[arg-type]
    )

    metrics = service.business_metrics()

    assert metrics is not None
    assert metrics.consume_lag_seconds is None
