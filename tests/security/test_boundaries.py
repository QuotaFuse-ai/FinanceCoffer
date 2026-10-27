"""Regression tests for REST / query-layer security boundaries.

Covers S-01 (SQL injection), S-02 (unbounded vector scan), S-03 (REST bind
address), S-06 (secret leakage), S-07 (large-query DoS) and S-10 (audit /
trace propagation). No live PostgreSQL is required: the query layer is driven
through an in-memory connector, and the REST layer through ``TestClient``.
"""

from __future__ import annotations

import io
import json
from datetime import date
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from financecoffer.api import create_app
from financecoffer.cli import app as cli_app
from financecoffer.core.config import Config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import InvalidArgumentError, NotFoundError
from financecoffer.core.logging import configure_logging, get_logger
from financecoffer.core.redaction import REDACTED
from financecoffer.core.trace import normalize_trace_id
from financecoffer.query.service import (
    MAX_QUERY_CHARS,
    MAX_RISK_LIMIT,
    MAX_TOP_K,
    MAX_YEAR_SPAN,
    QueryResult,
    QueryService,
)

DSN = "postgresql://fcoffer:sup3r-s3cret@127.0.0.1:5432/financecoffer"
INJECTION_PAYLOADS = (
    "600000';DROP TABLE ods.stock_raw_data;--",
    'AAPL" OR 1=1--',
    "600000' OR '1'='1",
    "1;DELETE FROM dwd.stock_risk_event",
    "A" * (MAX_QUERY_CHARS + 1),
)


# -- fakes -------------------------------------------------------------------


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
        self.session: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def cursor(self) -> _Cursor:
        return self._cursor

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.session.append((sql, tuple(params)))


class FakeDB:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None, columns: list[str] | None = None):
        self.cursor = _Cursor(rows or [], columns or [])
        self.calls = 0
        self.conn: _Conn | None = None

    def __call__(self, dsn: str, *, timeout: int) -> _Conn:
        self.calls += 1
        self.conn = _Conn(self.cursor)
        return self.conn


class Tripwire:
    """Fails loudly if any request reaches the database layer."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, dsn: str, *, timeout: int) -> Any:
        self.calls += 1
        raise AssertionError("database must not be touched for a rejected request")


class FakeService:
    def __init__(self, *, result: QueryResult | None = None) -> None:
        self.config = Config(db_url=DSN)
        self.result = result or QueryResult(data={"symbol": "600000"}, source="dws")
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def ping(self) -> bool:
        return True

    def _answer(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> QueryResult:
        self.calls.append((name, args, kwargs))
        return self.result

    def structured(self, symbol: str, **kwargs: Any) -> QueryResult:
        return self._answer("structured", (symbol,), kwargs)

    def risk(self, symbol: str, **kwargs: Any) -> QueryResult:
        return self._answer("risk", (symbol,), kwargs)

    def search_announcements(self, symbol: str, text: str, **kwargs: Any) -> QueryResult:
        return self._answer("search_announcements", (symbol, text), kwargs)


def _http(service: Any) -> TestClient:
    return TestClient(create_app(config=Config(db_url=DSN), service=service))


def _real_service(connector: Any) -> QueryService:
    return QueryService(config=Config(db_url=DSN), connector=connector)


# -- S-01: SQL injection -----------------------------------------------------


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_injection_payloads_are_rejected_before_sql(payload: str) -> None:
    tripwire = Tripwire()
    service = _real_service(tripwire)

    for call in (
        lambda: service.structured(payload),
        lambda: service.risk(payload),
        lambda: service.search_announcements(payload, "revenue"),
    ):
        with pytest.raises(InvalidArgumentError) as excinfo:
            call()
        assert excinfo.value.details == {"field": "symbol"}

    assert tripwire.calls == 0


def test_injection_payloads_get_a_400_from_the_rest_surface() -> None:
    tripwire = Tripwire()
    client = TestClient(
        create_app(config=Config(db_url=DSN), service=_real_service(tripwire))
    )

    for payload in INJECTION_PAYLOADS:
        response = client.get(f"/v1/structured/{quote(payload, safe='')}")
        assert response.status_code == 400, payload
        body = response.json()
        assert body["error"]["code"] == "E_INVALID_ARGUMENT"
        assert body["error"]["details"]["field"] == "symbol"

    assert tripwire.calls == 0


def test_valid_symbols_are_passed_as_parameters_never_interpolated() -> None:
    db = FakeDB(columns=["symbol"])
    service = _real_service(db)

    with pytest.raises(NotFoundError):
        service.structured("600000")

    sql, params = db.cursor.executed[0]
    assert "600000" not in sql
    assert params == ("600000",)


# -- S-02: vector search must be filtered by symbol --------------------------


def test_search_without_a_symbol_is_a_400() -> None:
    service = FakeService()
    client = _http(service)

    body = {"query_text": "revenue growth", "top_k": 5}
    response = client.post("/v1/search/announcements", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_INVALID_ARGUMENT"
    assert service.calls == []


def test_search_rejects_an_empty_symbol_before_connecting() -> None:
    tripwire = Tripwire()
    service = _real_service(tripwire)

    with pytest.raises(InvalidArgumentError):
        service.search_announcements("", "revenue growth")

    assert tripwire.calls == 0


def test_vector_query_filters_by_symbol_and_dates_before_ordering(monkeypatch) -> None:
    monkeypatch.setattr(QueryService, "_embed", lambda self, text: "[0.1,0.2]")
    db = FakeDB()
    service = _real_service(db)

    service.search_announcements("600000", "revenue growth", year_range="2023-2024")

    sql, params = db.cursor.executed[0]
    assert "WHERE c.symbol = %s" in sql
    assert sql.index("WHERE c.symbol = %s") < sql.index("ORDER BY")
    assert params[1:4] == ("600000", date(2023, 1, 1), date(2024, 12, 31))


# -- S-03: serve must stay on loopback ---------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::", "example.com", " 127.0.0.2 "])
def test_serve_api_refuses_a_non_loopback_bind(host: str) -> None:
    result = CliRunner().invoke(cli_app, ["serve", "api", "--host", host])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["host"] == host


def test_serve_api_documents_the_loopback_default() -> None:
    result = CliRunner().invoke(cli_app, ["serve", "api", "--help"])

    assert result.exit_code == 0
    assert "127.0.0.1" in result.stdout
    assert "loopback" in result.stdout


# -- S-06: secrets must never reach logs or config output --------------------


def test_dsn_password_is_redacted_in_structured_logs() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    get_logger("security").warning("connecting to %s", DSN, extra={"db_url": DSN})

    text = stream.getvalue()
    assert "sup3r-s3cret" not in text
    assert REDACTED in text
    payload = json.loads(text.strip().splitlines()[-1])
    assert payload["db_url"] == "postgresql://fcoffer:***@127.0.0.1:5432/financecoffer"


def test_config_redacted_dict_hides_every_secret() -> None:
    config = Config(
        db_url=DSN,
        minio_access_key="minio-access",
        minio_secret_key="minio-s3cret",
        embedding_api_key="sk-embedding-s3cret",
        mcp_token="mcp-s3cret",
    )

    shown = config.redacted_dict()
    rendered = json.dumps(shown)

    secrets = ("sup3r-s3cret", "minio-access", "minio-s3cret", "sk-embedding-s3cret", "mcp-s3cret")
    for secret in secrets:
        assert secret not in rendered
    assert shown["minio_secret_key"] == REDACTED
    assert shown["mcp_token"] == REDACTED


def test_embedding_failure_does_not_echo_the_api_key() -> None:
    config = Config(
        db_url=DSN,
        embedding_api_key="sk-embedding-s3cret",
        embedding_url="http://127.0.0.1:1/embed",
    )
    service = QueryService(config=config, connector=FakeDB())

    with pytest.raises(Exception) as excinfo:
        service._embed("revenue growth")

    assert "sk-embedding-s3cret" not in str(excinfo.value)
    assert "sk-embedding-s3cret" not in repr(getattr(excinfo.value, "details", None))


# -- S-07: bounded inputs and a real server-side timeout ---------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"top_k": 0}, "top_k"),
        ({"top_k": MAX_TOP_K + 1}, "top_k"),
        ({"year_range": f"2000-{2000 + MAX_YEAR_SPAN}"}, "at most"),
        ({"query_text": "x" * (MAX_QUERY_CHARS + 1)}, "query_text"),
    ],
)
def test_search_limits_are_enforced_before_connecting(
    monkeypatch, kwargs: dict[str, Any], match: str
) -> None:
    monkeypatch.setattr(QueryService, "_embed", lambda self, text: "[0.1,0.2]")
    tripwire = Tripwire()
    service = _real_service(tripwire)
    query = kwargs.pop("query_text", "revenue growth")

    with pytest.raises(InvalidArgumentError, match=match):
        service.search_announcements("600000", query, **kwargs)

    assert tripwire.calls == 0


def test_risk_limit_is_capped_before_connecting() -> None:
    tripwire = Tripwire()
    service = _real_service(tripwire)

    with pytest.raises(InvalidArgumentError, match="limit"):
        service.risk("600000", limit=MAX_RISK_LIMIT + 1)

    assert tripwire.calls == 0


@pytest.mark.parametrize("top_k", [0, MAX_TOP_K + 1])
def test_rest_rejects_out_of_contract_top_k(top_k: int) -> None:
    service = FakeService()
    client = _http(service)

    response = client.post(
        "/v1/search/announcements",
        json={"symbol": "600000", "query_text": "revenue", "top_k": top_k},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_INVALID_ARGUMENT"
    assert service.calls == []


def test_rest_rejects_an_oversized_query_text() -> None:
    service = FakeService()
    client = _http(service)

    response = client.post(
        "/v1/search/announcements",
        json={
            "symbol": "600000",
            "query_text": "x" * (MAX_QUERY_CHARS + 1),
        },
    )

    assert response.status_code == 400
    assert service.calls == []


def test_rest_rejects_a_risk_limit_above_the_cap() -> None:
    service = FakeService()
    client = _http(service)

    response = client.get(f"/v1/risk/600000?limit={MAX_RISK_LIMIT + 1}")

    assert response.status_code == 400
    assert service.calls == []


def test_query_timeout_is_applied_as_a_server_side_statement_timeout() -> None:
    db = FakeDB(columns=["symbol"])
    service = QueryService(config=Config(db_url=DSN), timeout=30, connector=db)

    with pytest.raises(NotFoundError):
        service.structured("600000")

    assert db.conn is not None
    statement, params = db.conn.session[0]
    assert "set_config('statement_timeout'" in statement
    assert params == (str(30 * 1000),)


# -- S-10: trace ids propagate through every response -----------------------


def _assert_usable_trace(value: str) -> None:
    assert normalize_trace_id(value) == value


def test_a_valid_trace_id_is_echoed_in_header_and_envelope() -> None:
    client = _http(FakeService())

    response = client.get("/v1/structured/600000", headers={"X-Trace-Id": "req-abc.123:4"})

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "req-abc.123:4"
    assert response.json()["meta"]["trace_id"] == "req-abc.123:4"


def test_an_invalid_trace_id_is_replaced_never_echoed() -> None:
    client = _http(FakeService())

    response = client.get("/v1/structured/600000", headers={"X-Trace-Id": "bad id!"})

    assert response.status_code == 200
    trace = response.headers["X-Trace-Id"]
    assert trace != "bad id!"
    _assert_usable_trace(trace)
    assert response.json()["meta"]["trace_id"] == trace


def test_error_responses_still_carry_a_trace_id() -> None:
    client = _http(FakeService())

    response = client.get("/v1/risk/600000?limit=0")

    assert response.status_code == 400
    trace = response.headers["X-Trace-Id"]
    _assert_usable_trace(trace)
    assert response.json()["meta"]["trace_id"] == trace


def test_query_meta_carries_the_request_trace() -> None:
    result = QueryResult(data={"symbol": "600000"}, source="dws")
    assert result.meta("trace-1").trace_id == "trace-1"
    assert isinstance(Meta(), Meta)
