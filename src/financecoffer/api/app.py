"""FastAPI application for the FinanceCoffer REST surface.

Routes follow ``financecoffer.md`` section 6.2 and every response uses the
shared envelope from DEC-0007. Errors are translated to stable ``E_*`` codes
with an HTTP status derived from the error class, never from the message text.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from financecoffer import __version__
from financecoffer.api.ratelimit import TokenBucket
from financecoffer.config.loader import load_config
from financecoffer.core.config import Config
from financecoffer.core.envelope import Envelope
from financecoffer.core.errors import (
    ErrorCode,
    FinanceCofferError,
    InternalError,
    InvalidArgumentError,
    RateLimitedError,
)
from financecoffer.core.logging import get_logger, log_query_audit
from financecoffer.core.trace import new_trace_id, normalize_trace_id
from financecoffer.query.service import (
    MAX_QUERY_CHARS,
    MAX_RISK_LIMIT,
    MAX_TOP_K,
    QueryService,
)

_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.E_INVALID_ARGUMENT: 400,
    ErrorCode.E_NOT_FOUND: 404,
    ErrorCode.E_AUTH: 401,
    ErrorCode.E_CONFIG: 503,
    ErrorCode.E_STORAGE: 500,
    ErrorCode.E_DEPENDENCY: 503,
    ErrorCode.E_PARTIAL_FAILURE: 500,
    ErrorCode.E_NOT_IMPLEMENTED: 501,
    ErrorCode.E_INTERRUPTED: 500,
    ErrorCode.E_INTERNAL: 500,
    ErrorCode.E_RATE_LIMITED: 429,
}

_RATE_LIMITED_PREFIX = "/v1/"

_log = get_logger("api")


class _Metrics:
    """Minimal in-process Prometheus registry for the API itself."""

    def __init__(self) -> None:
        self._requests: Counter[tuple[str, str, int]] = Counter()
        self._seconds = 0.0

    def record(self, method: str, path: str, status: int, seconds: float) -> None:
        self._requests[(method, path, status)] += 1
        self._seconds += seconds

    def render(self) -> str:
        lines = [
            "# HELP fc_api_requests_total Total HTTP requests handled by the REST API.",
            "# TYPE fc_api_requests_total counter",
        ]
        for (method, path, status), count in sorted(self._requests.items()):
            labels = f'method="{method}",path="{path}",status="{status}"'
            lines.append(f"fc_api_requests_total{{{labels}}} {count}")
        lines.extend(
            [
                "# HELP fc_api_request_seconds_total Cumulative request handling time.",
                "# TYPE fc_api_request_seconds_total counter",
                f"fc_api_request_seconds_total {self._seconds:.6f}",
            ]
        )
        return "\n".join(lines) + "\n"


class AnnouncementSearchRequest(BaseModel):
    """Body of ``POST /v1/search/announcements``."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)
    query_text: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    year_range: str | None = None
    top_k: int = Field(default=10, ge=1, le=MAX_TOP_K)


def _trace_id(request: Request) -> str:
    value = getattr(request.state, "trace_id", None)
    return value if isinstance(value, str) and value else new_trace_id()


def _service(request: Request) -> QueryService:
    return request.app.state.query_service  # type: ignore[no-any-return]


def _respond(envelope: Envelope) -> JSONResponse:
    return JSONResponse(content=envelope.model_dump(mode="json"))


_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})


def _field_name(location: tuple[Any, ...]) -> str:
    """Report ``symbol`` rather than FastAPI's internal ``body.symbol`` path."""

    parts = [str(part) for part in location]
    if len(parts) > 1 and parts[0] in _LOCATIONS:
        parts = parts[1:]
    return ".".join(parts)


def _audit_query(
    request: Request,
    tool: str,
    *,
    started: float,
    symbol: str | None,
    outcome: str,
    year_range: str | None = None,
    since: str | None = None,
    top_k: int | None = None,
    hits: int | None = None,
) -> None:
    """Emit the S-10-1 audit record for one REST query."""

    log_query_audit(
        tool=tool,
        trace_id=_trace_id(request),
        symbol=symbol,
        year_range=year_range,
        since=since,
        top_k=top_k,
        hits=hits,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        outcome=outcome,
    )


def create_app(
    *,
    config: Config | None = None,
    dsn: str | None = None,
    service: QueryService | None = None,
    rate_limiter: TokenBucket | None = None,
) -> FastAPI:
    """Build the ASGI app.

    ``service`` is injectable for tests; in production the DSN comes from
    ``FC_DB_URL`` (optionally overridden by ``dsn``). Missing configuration is
    reported by ``/health`` and by the first query, not at import time.

    ``rate_limiter`` overrides the limiter derived from ``config``; pass
    ``None`` with ``rate_limit_enabled=False`` to disable limiting entirely.
    """

    resolved_config = config or load_config()
    app = FastAPI(
        title="FinanceCoffer API",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.config = resolved_config
    app.state.query_service = service or QueryService(config=resolved_config, dsn=dsn)
    app.state.metrics = _Metrics()
    app.state.rate_limiter = (
        rate_limiter
        if rate_limiter is not None
        else (
            TokenBucket(rate_per_minute=resolved_config.rate_limit_per_minute)
            if resolved_config.rate_limit_enabled
            else None
        )
    )

    @app.middleware("http")
    async def _instrument(request: Request, call_next: Any) -> Any:
        raw = request.headers.get("X-Trace-Id")
        trace = new_trace_id()
        if raw:
            try:
                trace = normalize_trace_id(raw)
            except InvalidArgumentError:
                trace = new_trace_id()
        request.state.trace_id = trace
        started = time.perf_counter()

        limiter: TokenBucket | None = app.state.rate_limiter
        if limiter is not None and request.url.path.startswith(_RATE_LIMITED_PREFIX):
            client_ip = request.client.host if request.client else "unknown"
            if not limiter.allow(client_ip):
                retry_after = limiter.retry_after(client_ip)
                error = RateLimitedError(
                    "rate limit exceeded",
                    details={
                        "limit_per_minute": limiter.rate_per_minute,
                        "retry_after": retry_after,
                    },
                )
                response = JSONResponse(
                    status_code=_HTTP_STATUS[ErrorCode.E_RATE_LIMITED],
                    content=Envelope.from_error(error, trace_id=trace).model_dump(mode="json"),
                )
                response.headers["Retry-After"] = str(retry_after)
                app.state.metrics.record(
                    request.method,
                    "rate_limited",
                    response.status_code,
                    time.perf_counter() - started,
                )
                response.headers["X-Trace-Id"] = trace
                return response

        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        app.state.metrics.record(
            request.method, path, response.status_code, time.perf_counter() - started
        )
        response.headers["X-Trace-Id"] = trace
        return response

    @app.exception_handler(FinanceCofferError)
    async def _finance_error(request: Request, exc: FinanceCofferError) -> JSONResponse:
        status = _HTTP_STATUS.get(exc.code, 500)
        return JSONResponse(
            status_code=status,
            content=Envelope.from_error(exc, trace_id=_trace_id(request)).model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        problems = [
            {"field": _field_name(error["loc"]), "reason": error["msg"]}
            for error in exc.errors()
        ]
        error = InvalidArgumentError("invalid request", details={"errors": problems})
        return JSONResponse(
            status_code=400,
            content=Envelope.from_error(error, trace_id=_trace_id(request)).model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        _log.error("unhandled API error", exc_info=exc)
        error = InternalError("unexpected server error")
        return JSONResponse(
            status_code=500,
            content=Envelope.from_error(error, trace_id=_trace_id(request)).model_dump(mode="json"),
        )

    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        query_service: QueryService = request.app.state.query_service
        database = query_service.ping()
        data = {
            "status": "ok" if database else "degraded",
            "version": __version__,
            "checks": {"database": "ok" if database else "unreachable"},
            "compliance_enabled": query_service.config.compliance_enabled,
        }
        return _respond(Envelope.success(data, trace_id=_trace_id(request)))

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            request.app.state.metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/v1/structured/{symbol}")
    def structured(
        request: Request,
        symbol: str,
        metric: str | None = None,
        year_range: str | None = None,
    ) -> JSONResponse:
        tool = "GET /v1/structured/{symbol}"
        started = time.perf_counter()
        try:
            result = _service(request).structured(symbol, metric=metric, year_range=year_range)
        except FinanceCofferError as exc:
            _audit_query(
                request,
                tool,
                started=started,
                symbol=symbol,
                year_range=year_range,
                outcome=exc.code.value,
            )
            raise
        _audit_query(
            request,
            tool,
            started=started,
            symbol=symbol,
            year_range=year_range,
            hits=len(result.data.get("years") or ()),
            outcome="ok",
        )
        return _respond(
            Envelope.success(result.data, meta=result.meta(_trace_id(request)))
        )

    @app.get("/v1/risk/{symbol}")
    def risk(
        request: Request,
        symbol: str,
        since: str | None = None,
        limit: int = Query(default=50, ge=1, le=MAX_RISK_LIMIT),
    ) -> JSONResponse:
        tool = "GET /v1/risk/{symbol}"
        started = time.perf_counter()
        try:
            result = _service(request).risk(symbol, since=since, limit=limit)
        except FinanceCofferError as exc:
            _audit_query(
                request,
                tool,
                started=started,
                symbol=symbol,
                since=since,
                outcome=exc.code.value,
            )
            raise
        _audit_query(
            request,
            tool,
            started=started,
            symbol=symbol,
            since=since,
            hits=result.data.get("count"),
            outcome="ok",
        )
        return _respond(
            Envelope.success(result.data, meta=result.meta(_trace_id(request)))
        )

    @app.post("/v1/search/announcements")
    def search_announcements(
        request: Request,
        body: AnnouncementSearchRequest,
    ) -> JSONResponse:
        tool = "POST /v1/search/announcements"
        started = time.perf_counter()
        try:
            result = _service(request).search_announcements(
                body.symbol,
                body.query_text,
                year_range=body.year_range,
                top_k=body.top_k,
            )
        except FinanceCofferError as exc:
            _audit_query(
                request,
                tool,
                started=started,
                symbol=body.symbol,
                year_range=body.year_range,
                top_k=body.top_k,
                outcome=exc.code.value,
            )
            raise
        _audit_query(
            request,
            tool,
            started=started,
            symbol=body.symbol,
            year_range=body.year_range,
            top_k=body.top_k,
            hits=result.data.get("count"),
            outcome="ok",
        )
        return _respond(
            Envelope.success(result.data, meta=result.meta(_trace_id(request)))
        )

    return app


__all__ = ["AnnouncementSearchRequest", "create_app"]
