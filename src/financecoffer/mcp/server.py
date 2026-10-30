"""MCP server builders and transports.

Every tool is a thin adapter over :class:`financecoffer.query.QueryService`; no
SQL or warehouse logic lives in this package. Responses use the shared
:class:`~financecoffer.core.envelope.Envelope` serialized as JSON text, and a
failure carries ``{code, message, details, trace_id}`` with ``isError=true``.
Unexpected exceptions are logged and reported as ``E_INTERNAL`` with a generic
message so driver or connection text never reaches the client.

HTTP mode is Streamable HTTP. A non-loopback bind is refused (``E_CONFIG``)
unless both a Bearer token and a non-empty Host allowlist are configured, and
this check runs before any server object is built or a socket is opened.
"""

from __future__ import annotations

import hmac
import json
import time
from collections.abc import Callable, Sequence
from typing import Any

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from financecoffer.core.envelope import Envelope
from financecoffer.core.errors import (
    AuthError,
    ConfigError,
    FinanceCofferError,
    InternalError,
)
from financecoffer.core.logging import get_logger, log_query_audit
from financecoffer.core.trace import new_trace_id
from financecoffer.mcp.tools import TOOL_SPECS
from financecoffer.query import QueryResult, QueryService

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_SERVER_NAME = "financecoffer"
_Instructions = (
    "Read-only market data for A-share and US symbols. Every result uses the "
    "shared {ok, data, meta, error} envelope; meta.trace_id identifies the call."
)

_log = get_logger("mcp")
_SPECS = {spec["name"]: spec for spec in TOOL_SPECS}

_Hits = Callable[[dict[str, Any]], int | None]


class BearerAuthMiddleware:
    """Require a static ``Authorization: Bearer`` header on HTTP requests."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).get("authorization", "").encode("utf-8", "replace")
        if not hmac.compare_digest(supplied, self._expected):
            envelope = Envelope.from_error(AuthError("missing or invalid bearer token"))
            response = JSONResponse(
                envelope.model_dump(mode="json"),
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _error_result(error: FinanceCofferError, trace_id: str) -> CallToolResult:
    payload = {
        "code": error.code.value,
        "message": error.message,
        "details": error.details,
        "trace_id": trace_id,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        is_error=True,
    )


def _success_result(result: QueryResult, trace_id: str) -> CallToolResult:
    envelope = Envelope.success(result.data, meta=result.meta(trace_id))
    text = json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False)
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=False)


def _audit(
    *,
    tool: str,
    trace_id: str,
    symbol: str | None,
    year_range: str | None,
    top_k: int | None,
    hits: int | None,
    started: float,
    outcome: str,
) -> None:
    # Metadata only: log_query_audit has no query_text parameter by design.
    log_query_audit(
        tool=tool,
        trace_id=trace_id,
        symbol=symbol,
        year_range=year_range,
        top_k=top_k,
        hits=hits,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        outcome=outcome,
    )


async def _execute(
    *,
    tool: str,
    call: Callable[[], QueryResult],
    hits: _Hits,
    symbol: str | None,
    year_range: str | None = None,
    top_k: int | None = None,
) -> CallToolResult:
    trace_id = new_trace_id()
    started = time.perf_counter()
    try:
        result = await anyio.to_thread.run_sync(call)
    except FinanceCofferError as exc:
        _audit(
            tool=tool,
            trace_id=trace_id,
            symbol=symbol,
            year_range=year_range,
            top_k=top_k,
            hits=None,
            started=started,
            outcome=exc.code.value,
        )
        return _error_result(exc, trace_id)
    except Exception:
        _log.exception("MCP tool %s failed", tool, extra={"trace_id": trace_id})
        error = InternalError("unexpected server error")
        _audit(
            tool=tool,
            trace_id=trace_id,
            symbol=symbol,
            year_range=year_range,
            top_k=top_k,
            hits=None,
            started=started,
            outcome=error.code.value,
        )
        return _error_result(error, trace_id)
    _audit(
        tool=tool,
        trace_id=trace_id,
        symbol=symbol,
        year_range=year_range,
        top_k=top_k,
        hits=hits(result.data),
        started=started,
        outcome="ok",
    )
    return _success_result(result, trace_id)


def create_server(service: QueryService) -> MCPServer:
    """Build the MCP server with the three spec-fixed tools."""

    server = MCPServer(name=_SERVER_NAME, instructions=_Instructions)

    async def query_structured_data(
        symbol: str,
        metric: str | None = None,
        year_range: str | None = None,
        valuation_basis: str | None = None,
    ) -> CallToolResult:
        return await _execute(
            tool="query_structured_data",
            call=lambda: service.structured(
                symbol,
                metric=metric,
                year_range=year_range,
                valuation_basis=valuation_basis,
            ),
            hits=lambda data: len(data.get("years") or ()),
            symbol=symbol,
            year_range=year_range,
        )

    async def search_announcements_semantically(
        symbol: str,
        query_text: str,
        year_range: str | None = None,
    ) -> CallToolResult:
        return await _execute(
            tool="search_announcements_semantically",
            call=lambda: service.search_announcements(
                symbol, query_text, year_range=year_range
            ),
            hits=lambda data: data.get("count"),
            symbol=symbol,
            year_range=year_range,
            top_k=10,
        )

    async def get_risk_history(symbol: str) -> CallToolResult:
        return await _execute(
            tool="get_risk_history",
            call=lambda: service.risk(symbol),
            hits=lambda data: data.get("count"),
            symbol=symbol,
        )

    server.add_tool(
        query_structured_data,
        name="query_structured_data",
        description=_SPECS["query_structured_data"]["description"],
        structured_output=False,
    )
    server.add_tool(
        search_announcements_semantically,
        name="search_announcements_semantically",
        description=_SPECS["search_announcements_semantically"]["description"],
        structured_output=False,
    )
    server.add_tool(
        get_risk_history,
        name="get_risk_history",
        description=_SPECS["get_risk_history"]["description"],
        structured_output=False,
    )
    return server


def _normalize_host(value: str) -> str:
    host = value.strip().lower()
    if not host:
        return ""
    if host.startswith("[") and host.endswith("]"):
        return f"{host}:*"
    return host if ":" in host else f"{host}:*"


def normalize_allowed_hosts(values: Sequence[str]) -> tuple[str, ...]:
    """Normalize Host allowlist entries; bare names accept any port."""

    hosts: list[str] = []
    for value in values:
        host = _normalize_host(value)
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def validate_http_options(
    *,
    host: str,
    token: str | None,
    allowed_hosts: Sequence[str],
) -> tuple[str, ...]:
    """Return normalized hosts or raise ``E_CONFIG`` for an unsafe bind.

    Runs before any server object is constructed so an invalid configuration
    never opens a listening socket.
    """

    hosts = normalize_allowed_hosts(allowed_hosts)
    if host not in LOOPBACK_HOSTS and (not token or not hosts):
        raise ConfigError(
            "serving MCP on a non-loopback address requires a Bearer token and a Host allowlist",
            details={
                "host": host,
                "hint": "pass --token (or FC_MCP_TOKEN) and --allowed-hosts host1,host2",
            },
        )
    return hosts


def _transport_security(
    host: str, allowed_hosts: tuple[str, ...]
) -> TransportSecuritySettings | None:
    if not allowed_hosts:
        # The SDK installs loopback defaults when the host is a loopback address.
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts),
        allowed_origins=[],
    )


def build_http_app(
    server: MCPServer,
    *,
    host: str,
    token: str | None,
    allowed_hosts: Sequence[str],
) -> ASGIApp:
    hosts = validate_http_options(host=host, token=token, allowed_hosts=allowed_hosts)
    app: ASGIApp = server.streamable_http_app(
        transport_security=_transport_security(host, hosts),
        host=host,
    )
    if token:
        app = BearerAuthMiddleware(app, token)
    return app


def run_stdio(service: QueryService) -> None:
    """Serve MCP over stdio (protocol frames handled by the SDK)."""

    create_server(service).run("stdio")


def run_http(
    service: QueryService,
    *,
    host: str = "127.0.0.1",
    port: int = 8100,
    token: str | None = None,
    allowed_hosts: Sequence[str] = (),
    log_level: str = "info",
) -> None:
    """Serve MCP over Streamable HTTP after validating the bind configuration."""

    validate_http_options(host=host, token=token, allowed_hosts=allowed_hosts)
    server = create_server(service)
    app = build_http_app(server, host=host, token=token, allowed_hosts=allowed_hosts)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
