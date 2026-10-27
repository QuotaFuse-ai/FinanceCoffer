"""Unit tests for the MCP layer (no database, Redis or embedding service)."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from typer.testing import CliRunner

from financecoffer.cli import app as cli_app
from financecoffer.core.errors import (
    ConfigError,
    DependencyError,
    InvalidArgumentError,
)
from financecoffer.mcp.server import (
    BearerAuthMiddleware,
    build_http_app,
    create_server,
    normalize_allowed_hosts,
    run_http,
    run_stdio,
    validate_http_options,
)
from financecoffer.mcp.tools import TOOL_SPECS
from financecoffer.query import MAX_QUERY_CHARS, QueryResult, normalize_symbol

TOOL_NAMES = [
    "query_structured_data",
    "search_announcements_semantically",
    "get_risk_history",
]
TOOL_PARAMS = {
    "query_structured_data": ["symbol", "metric", "year_range"],
    "search_announcements_semantically": ["symbol", "query_text", "year_range"],
    "get_risk_history": ["symbol"],
}


class FakeService:
    """In-memory ``QueryService`` stand-in with the same validation surface."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.error = error

    def _answer(
        self,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        data: dict[str, Any],
    ) -> QueryResult:
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error
        return QueryResult(data=data, source="dws", quality_status="ok", confidence="high")

    def structured(
        self, symbol: str, *, metric: str | None = None, year_range: str | None = None
    ) -> QueryResult:
        normalized = normalize_symbol(symbol)
        return self._answer(
            "structured",
            (normalized,),
            {"metric": metric, "year_range": year_range},
            {"symbol": normalized, "years": [{"year": 2024}]},
        )

    def risk(self, symbol: str, *, since: str | None = None, limit: int = 50) -> QueryResult:
        normalized = normalize_symbol(symbol)
        return self._answer(
            "risk",
            (normalized,),
            {"since": since, "limit": limit},
            {"symbol": normalized, "count": 1, "events": [{"id": 1}]},
        )

    def search_announcements(
        self,
        symbol: str,
        query_text: str,
        *,
        year_range: str | None = None,
        top_k: int = 10,
    ) -> QueryResult:
        normalized = normalize_symbol(symbol)
        text = (query_text or "").strip()
        if len(text) > MAX_QUERY_CHARS:
            raise InvalidArgumentError(
                f"query_text must be at most {MAX_QUERY_CHARS} characters",
                details={"field": "query_text", "length": len(text)},
            )
        return self._answer(
            "search_announcements",
            (normalized, text),
            {"year_range": year_range, "top_k": top_k},
            {"symbol": normalized, "count": 1, "results": [{"id": 1}]},
        )


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _audit_records() -> Iterator[list[logging.LogRecord]]:
    logger = logging.getLogger("financecoffer.audit")
    handler = _Capture()
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _call(name: str, arguments: dict[str, Any], service: FakeService | None = None) -> Any:
    server = create_server(service or FakeService())
    return anyio.run(lambda: server.call_tool(name, arguments))


def _payload(result: Any, *, error: bool = False) -> dict[str, Any]:
    assert bool(result.is_error) is error
    assert result.content[0].type == "text"
    return json.loads(result.content[0].text)


def test_tool_catalogue_is_fixed() -> None:
    assert [spec["name"] for spec in TOOL_SPECS] == TOOL_NAMES
    for spec in TOOL_SPECS:
        assert list(spec["inputSchema"]["properties"]) == TOOL_PARAMS[spec["name"]]
        assert spec["inputSchema"]["additionalProperties"] is False
    required = {spec["name"]: spec["inputSchema"]["required"] for spec in TOOL_SPECS}
    assert required == {
        "query_structured_data": ["symbol"],
        "search_announcements_semantically": ["symbol", "query_text"],
        "get_risk_history": ["symbol"],
    }


def test_registered_tools_match_the_catalogue() -> None:
    server = create_server(FakeService())

    tools = anyio.run(server.list_tools)

    assert [tool.name for tool in tools] == TOOL_NAMES
    for tool, spec in zip(tools, TOOL_SPECS, strict=True):
        assert tool.description == spec["description"]
        assert list(tool.input_schema["properties"]) == TOOL_PARAMS[tool.name]
        assert tool.input_schema["required"] == spec["inputSchema"]["required"]


def test_structured_tool_returns_envelope() -> None:
    service = FakeService()

    payload = _payload(
        _call("query_structured_data", {"symbol": "600519", "metric": "roe"}, service)
    )

    assert payload["ok"] is True
    assert payload["data"]["symbol"] == "600519"
    assert payload["meta"]["trace_id"]
    assert service.calls[0][0] == "structured"
    assert service.calls[0][2] == {"metric": "roe", "year_range": None}


def test_search_tool_passes_query_and_defaults_top_k() -> None:
    service = FakeService()

    payload = _payload(
        _call(
            "search_announcements_semantically",
            {"symbol": "AAPL", "query_text": "dividend", "year_range": "2020-2024"},
            service,
        )
    )

    assert payload["ok"] is True
    assert service.calls[0][0] == "search_announcements"
    assert service.calls[0][1] == ("AAPL", "dividend")
    assert service.calls[0][2] == {"year_range": "2020-2024", "top_k": 10}


def test_risk_tool_returns_envelope() -> None:
    service = FakeService()

    payload = _payload(_call("get_risk_history", {"symbol": "600519"}, service))

    assert payload["ok"] is True
    assert payload["data"]["count"] == 1
    assert service.calls[0][0] == "risk"


def test_invalid_symbol_maps_to_invalid_argument() -> None:
    payload = _payload(
        _call("query_structured_data", {"symbol": "600519; DROP TABLE"}),
        error=True,
    )

    assert payload["code"] == "E_INVALID_ARGUMENT"
    assert payload["trace_id"]
    assert payload["details"]["field"] == "symbol"


def test_overlong_query_text_is_rejected_not_truncated() -> None:
    too_long = "x" * (MAX_QUERY_CHARS + 1)

    payload = _payload(
        _call(
            "search_announcements_semantically",
            {"symbol": "600519", "query_text": too_long},
        ),
        error=True,
    )

    assert payload["code"] == "E_INVALID_ARGUMENT"
    assert payload["details"]["length"] == MAX_QUERY_CHARS + 1
    assert too_long not in json.dumps(payload)


def test_embedding_unavailable_maps_to_dependency() -> None:
    service = FakeService(
        error=DependencyError(
            "embedding service is unavailable",
            details={"url": "http://127.0.0.1:8080/v1/embeddings"},
        )
    )

    payload = _payload(
        _call(
            "search_announcements_semantically",
            {"symbol": "600519", "query_text": "dividend"},
            service,
        ),
        error=True,
    )

    assert payload["code"] == "E_DEPENDENCY"
    assert payload["details"]["url"].startswith("http://127.0.0.1:8080")


def test_unexpected_exception_does_not_leak_raw_text() -> None:
    raw = "connection to 127.0.0.1:5432 failed: password=hunter2"
    service = FakeService(error=RuntimeError(raw))

    result = _call("get_risk_history", {"symbol": "600519"}, service)
    text = result.content[0].text

    assert result.is_error is True
    payload = json.loads(text)
    assert payload["code"] == "E_INTERNAL"
    assert payload["message"] == "unexpected server error"
    assert payload["details"] is None
    assert "hunter2" not in text
    assert "5432" not in text


def test_audit_records_metadata_without_query_text() -> None:
    with _audit_records() as records:
        _payload(
            _call(
                "search_announcements_semantically",
                {"symbol": "600519", "query_text": "insider selling pressure"},
            )
        )

    assert len(records) == 1
    record = records[0]
    assert record.tool == "search_announcements_semantically"
    assert record.symbol == "600519"
    assert record.top_k == 10
    assert record.hits == 1
    assert record.outcome == "ok"
    assert record.trace_id
    assert record.elapsed_ms >= 0
    assert not hasattr(record, "query_text")
    assert "insider selling pressure" not in record.getMessage()


def test_audit_records_failures_with_the_error_code() -> None:
    with _audit_records() as records:
        _payload(_call("query_structured_data", {"symbol": "not a symbol"}), error=True)

    assert len(records) == 1
    assert records[0].outcome == "E_INVALID_ARGUMENT"
    assert records[0].hits is None


def test_non_loopback_without_token_is_rejected() -> None:
    with pytest.raises(ConfigError) as raised:
        validate_http_options(host="0.0.0.0", token=None, allowed_hosts=("mcp.example.com",))

    assert raised.value.code.value == "E_CONFIG"


def test_non_loopback_without_allowlist_is_rejected() -> None:
    with pytest.raises(ConfigError):
        validate_http_options(host="0.0.0.0", token="secret", allowed_hosts=())


def test_loopback_needs_neither_token_nor_allowlist() -> None:
    assert validate_http_options(host="127.0.0.1", token=None, allowed_hosts=()) == ()


def test_non_loopback_with_token_and_allowlist_is_accepted() -> None:
    hosts = validate_http_options(
        host="0.0.0.0", token="secret", allowed_hosts=("MCP.example.com",)
    )

    assert hosts == ("mcp.example.com:*",)
    assert normalize_allowed_hosts(["127.0.0.1:8100", " "]) == ("127.0.0.1:8100",)


def test_run_http_validates_before_building_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*_: Any, **__: Any) -> Any:
        raise AssertionError("the server must not be built for an invalid bind")

    monkeypatch.setattr("financecoffer.mcp.server.create_server", _fail)
    monkeypatch.setattr("financecoffer.mcp.server.uvicorn.run", _fail)

    with pytest.raises(ConfigError):
        run_http(FakeService(), host="0.0.0.0", port=8100)


def test_build_http_app_wraps_the_transport_with_bearer_auth() -> None:
    server = create_server(FakeService())

    app = build_http_app(server, host="127.0.0.1", token="s3cret", allowed_hosts=())

    assert isinstance(app, BearerAuthMiddleware)


def _asgi_call(inner: Any, headers: dict[str, str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        events.append(message)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8100),
    }
    anyio.run(lambda: inner(scope, receive, send))
    return events


def _body(events: list[dict[str, Any]]) -> bytes:
    return b"".join(event.get("body", b"") for event in events)


def test_bearer_middleware_rejects_missing_or_wrong_token() -> None:
    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerAuthMiddleware(inner, "s3cret")

    for headers in ({}, {"authorization": "Bearer wrong"}):
        events = _asgi_call(app, headers)
        assert events[0]["status"] == 401
        payload = json.loads(_body(events))
        assert payload["ok"] is False
        assert payload["error"]["code"] == "E_AUTH"

    assert _asgi_call(app, {"authorization": "Bearer s3cret"})[0]["status"] == 204


def test_stdio_session_initialize_list_and_call() -> None:
    service = FakeService()
    server = create_server(service)

    async def scenario() -> None:
        async with InMemoryTransport(server) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "financecoffer"

                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == TOOL_NAMES

                result = await session.call_tool("get_risk_history", {"symbol": "600519"})
                assert not result.is_error
                payload = json.loads(result.content[0].text)
                assert payload["ok"] is True
                assert payload["data"]["symbol"] == "600519"

    anyio.run(scenario)
    assert service.calls[0][0] == "risk"


def test_run_stdio_uses_the_sdk_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    transports: list[str] = []

    class _FakeServer:
        def run(self, transport: str = "stdio") -> None:
            transports.append(transport)

    monkeypatch.setattr(
        "financecoffer.mcp.server.create_server", lambda service: _FakeServer()
    )

    run_stdio(FakeService())

    assert transports == ["stdio"]


def test_cli_tools_json_lists_the_three_tools() -> None:
    result = CliRunner().invoke(cli_app, ["mcp", "tools", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert [tool["name"] for tool in payload["data"]["tools"]] == TOOL_NAMES
    assert payload["meta"]["trace_id"]


def test_cli_tools_text_matches_the_spec_signatures() -> None:
    result = CliRunner().invoke(cli_app, ["mcp", "tools"])

    assert result.exit_code == 0
    for line in (
        "query_structured_data(symbol, metric, year_range)",
        "search_announcements_semantically(symbol, query_text, year_range)",
        "get_risk_history(symbol)",
    ):
        assert line in result.stdout
