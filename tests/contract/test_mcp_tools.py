"""Contract tests for the MCP surface (``financecoffer.md`` section 6.1).

The tests drive the real SDK over ``InMemoryTransport``: ``initialize`` →
``tools/list`` → ``tools/call``. The tool catalogue in
:mod:`financecoffer.mcp.tools` is the source of truth for names, parameter
names and required arguments; every tool must answer with the shared
``{ok, data, meta, error}`` envelope, and a rejected call must surface as an
``isError`` result carrying a stable ``E_*`` code plus a trace id.
"""

from __future__ import annotations

import json
import re
from typing import Any

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from financecoffer.core.errors import InvalidArgumentError
from financecoffer.mcp.server import create_server
from financecoffer.mcp.tools import TOOL_SPECS
from financecoffer.query import STRUCTURED_METRICS, QueryResult, normalize_symbol

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
_REQUIRED = {
    "query_structured_data": ["symbol"],
    "search_announcements_semantically": ["symbol", "query_text"],
    "get_risk_history": ["symbol"],
}
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_META_KEYS = {
    "source",
    "fetched_at",
    "quality_status",
    "confidence",
    "compliance_tier",
    "trace_id",
}


class StubService:
    """``QueryService`` stand-in with the same input validation and data shapes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def structured(
        self, symbol: str, *, metric: str | None = None, year_range: str | None = None
    ) -> QueryResult:
        normalized = normalize_symbol(symbol)
        self.calls.append(("structured", (normalized,), {"metric": metric, "year_range": year_range}))
        return QueryResult(
            data={"symbol": normalized, "years": [{"year": 2023, "revenue": "123456789.0000"}]},
            source="dws",
            quality_status="normal",
            confidence="high",
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
        self.calls.append(
            ("search_announcements", (normalized, query_text), {"year_range": year_range, "top_k": top_k})
        )
        return QueryResult(
            data={"symbol": normalized, "count": 1, "results": [{"dwd_ref_id": 1, "score": 0.9}]},
            source="vector",
            quality_status="normal",
            confidence="high",
        )

    def risk(self, symbol: str, *, since: str | None = None, limit: int = 50) -> QueryResult:
        normalized = normalize_symbol(symbol)
        self.calls.append(("risk", (normalized,), {"since": since, "limit": limit}))
        return QueryResult(
            data={"symbol": normalized, "count": 1, "events": [{"id": 1, "risk_type": "goodwill_impairment"}]},
            source="dwd",
            quality_status="suspicious",
            confidence="medium",
        )


def _envelope(result: Any) -> dict[str, Any]:
    assert result.is_error is False
    assert result.content[0].type == "text"
    payload = json.loads(result.content[0].text)
    assert set(payload) == {"ok", "data", "meta", "error"}
    assert payload["ok"] is True
    assert payload["error"] is None
    assert set(payload["meta"]) == _META_KEYS
    assert _TRACE_ID.fullmatch(payload["meta"]["trace_id"])
    assert _RFC3339.fullmatch(payload["meta"]["fetched_at"])
    return payload


def _error_frame(result: Any) -> dict[str, Any]:
    assert result.is_error is True
    assert result.content[0].type == "text"
    payload = json.loads(result.content[0].text)
    assert set(payload) == {"code", "message", "details", "trace_id"}
    assert re.fullmatch(r"^E_[A-Z0-9_]+$", payload["code"])
    assert _TRACE_ID.fullmatch(payload["trace_id"])
    return payload


def _run(scenario: Any) -> Any:
    service = StubService()
    server = create_server(service)
    outcome: dict[str, Any] = {}

    async def runner() -> None:
        async with InMemoryTransport(server) as (read, write):
            async with ClientSession(read, write) as session:
                await scenario(session, outcome)

    anyio.run(runner)
    outcome["service"] = service
    return outcome


# -- protocol frames ---------------------------------------------------------


def test_initialize_announces_the_server_and_tool_capability() -> None:
    async def scenario(session: ClientSession, outcome: dict[str, Any]) -> None:
        outcome["init"] = await session.initialize()

    initialized = _run(scenario)["init"]

    assert initialized.server_info.name == "financecoffer"
    assert initialized.protocol_version
    assert initialized.capabilities.tools is not None
    assert "{ok, data, meta, error}" in (initialized.instructions or "")
    assert "trace_id" in (initialized.instructions or "")


def test_tools_list_matches_the_catalogue() -> None:
    async def scenario(session: ClientSession, outcome: dict[str, Any]) -> None:
        await session.initialize()
        outcome["tools"] = (await session.list_tools()).tools

    tools = _run(scenario)["tools"]
    specs = {spec["name"]: spec for spec in TOOL_SPECS}

    assert [tool.name for tool in tools] == TOOL_NAMES
    for tool in tools:
        spec = specs[tool.name]
        schema = tool.input_schema
        assert tool.description == spec["description"]
        assert list(schema["properties"]) == TOOL_PARAMS[tool.name]
        assert schema["required"] == _REQUIRED[tool.name]
        assert schema["type"] == "object"
        for name, property_schema in schema["properties"].items():
            expected = spec["inputSchema"]["properties"][name]
            if expected.get("type") == "string":
                assert property_schema["type"] == "string"
            else:
                assert property_schema["anyOf"] == [{"type": "string"}, {"type": "null"}]
                assert property_schema["default"] is None


def test_catalogue_only_constraints_are_not_in_the_generated_schema() -> None:
    """The function-signature schema cannot express the catalogue's enum and
    ``additionalProperties``; both stay documented but unenforced at the frame
    level. The runtime schema must still reject a missing required argument
    (covered below), and the enum is enforced by ``STRUCTURED_METRICS`` when a
    metric is projected."""

    specs = {spec["name"]: spec for spec in TOOL_SPECS}
    metric = specs["query_structured_data"]["inputSchema"]["properties"]["metric"]
    assert metric["anyOf"][0]["enum"] == list(STRUCTURED_METRICS)
    assert specs["query_structured_data"]["inputSchema"]["additionalProperties"] is False

    async def scenario(session: ClientSession, outcome: dict[str, Any]) -> None:
        await session.initialize()
        outcome["schema"] = (await session.list_tools()).tools[0].input_schema

    schema = _run(scenario)["schema"]
    assert "enum" not in schema["properties"]["metric"]
    assert "additionalProperties" not in schema


# -- tools/call responses ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "arguments", "data_key"),
    [
        ("query_structured_data", {"symbol": "600519", "metric": "roe"}, "years"),
        (
            "search_announcements_semantically",
            {"symbol": "600519", "query_text": "分红方案", "year_range": "2020-2023"},
            "results",
        ),
        ("get_risk_history", {"symbol": "600519"}, "events"),
    ],
)
def test_tools_call_returns_the_shared_envelope(
    name: str, arguments: dict[str, Any], data_key: str
) -> None:
    async def scenario(session: ClientSession, outcome: dict[str, Any]) -> None:
        await session.initialize()
        listed = [tool.name for tool in (await session.list_tools()).tools]
        assert listed == TOOL_NAMES
        outcome["result"] = await session.call_tool(name, arguments)

    outcome = _run(scenario)
    payload = _envelope(outcome["result"])
    data = payload["data"]

    assert data["symbol"] == "600519"
    assert data_key in data
    assert payload["meta"]["source"] in {"dws", "dwd", "vector"}
    assert outcome["service"].calls[0][0] in {"structured", "search_announcements", "risk"}


def test_structured_call_passes_the_projection_and_year_window() -> None:
    async def scenario(session: ClientSession, outcome: dict[str, Any]) -> None:
        await session.initialize()
        outcome["result"] = await session.call_tool(
            "query_structured_data",
            {"symbol": "600519", "metric": "roe", "year_range": "2020-2023"},
        )

    outcome = _run(scenario)
    _envelope(outcome["result"])
    assert outcome["service"].calls == [
        ("structured", ("600519",), {"metric": "roe", "year_range": "2020-2023"})
    ]


def test_search_call_uses_the_documented_default_top_k() -> None:
    async def scenario(session: ClientSession, outcome: dict[str, Any]) -> None:
        await session.initialize()
        outcome["result"] = await session.call_tool(
            "search_announcements_semantically",
            {"symbol": "600519", "query_text": "分红方案"},
        )

    outcome = _run(scenario)
    _envelope(outcome["result"])
    assert outcome["service"].calls[0][2] == {"year_range": None, "top_k": 10}


def test_a_rejected_symbol_is_an_error_frame() -> None:
    async def scenario(session: ClientSession, outcome: dict[str, Any]) -> None:
        await session.initialize()
        outcome["result"] = await session.call_tool(
            "query_structured_data", {"symbol": "600519; DROP TABLE"}
        )

    payload = _error_frame(_run(scenario)["result"])

    assert payload["code"] == "E_INVALID_ARGUMENT"
    assert payload["details"]["field"] == "symbol"


def test_a_missing_required_argument_is_an_error_frame() -> None:
    async def scenario(session: ClientSession, outcome: dict[str, Any]) -> None:
        await session.initialize()
        outcome["result"] = await session.call_tool("get_risk_history", {})

    result = _run(scenario)["result"]

    assert result.is_error is True
    assert "symbol" in result.content[0].text


def test_an_unknown_tool_is_an_error_frame() -> None:
    async def scenario(session: ClientSession, outcome: dict[str, Any]) -> None:
        await session.initialize()
        outcome["result"] = await session.call_tool("delete_everything", {})

    result = _run(scenario)["result"]

    assert result.is_error is True
    assert "Unknown tool" in result.content[0].text


def test_unexpected_errors_do_not_leak_internals() -> None:
    class ExplodingService(StubService):
        def risk(self, symbol: str, *, since: str | None = None, limit: int = 50) -> QueryResult:
            raise RuntimeError("password=hunter2 host=127.0.0.1:5432")

    server = create_server(ExplodingService())

    async def scenario() -> Any:
        async with InMemoryTransport(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("get_risk_history", {"symbol": "600519"})

    result = anyio.run(scenario)
    payload = _error_frame(result)
    rendered = json.dumps(payload)

    assert payload["code"] == "E_INTERNAL"
    assert payload["message"] == "unexpected server error"
    assert payload["details"] is None
    assert "hunter2" not in rendered
    assert "5432" not in rendered
