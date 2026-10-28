from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from financecoffer.api import create_app
from financecoffer.api.ratelimit import TokenBucket
from financecoffer.core.config import Config
from financecoffer.core.errors import (
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
    NotFoundError,
)
from financecoffer.query.service import (
    MAX_QUERY_CHARS,
    MAX_RISK_LIMIT,
    MAX_TOP_K,
    BusinessMetrics,
    QueryResult,
    QueryService,
)

DSN = "postgresql://fcoffer:secret@127.0.0.1:5432/financecoffer"


def _no_io(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("validation must finish before any database or HTTP call")


class FakeService:
    """In-memory ``QueryService`` stand-in for the HTTP layer."""

    def __init__(
        self,
        *,
        healthy: bool = True,
        embedding_ok: bool = True,
        result: QueryResult | None = None,
        error: FinanceCofferError | None = None,
        compliance_enabled: bool = False,
        business: BusinessMetrics | None = None,
    ) -> None:
        self.config = Config(db_url=DSN, compliance_enabled=compliance_enabled)
        self.healthy = healthy
        self.embedding_ok = embedding_ok
        self.result = result or QueryResult(data={"symbol": "600000"}, source="dws")
        self.error = error
        self.business = business
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def ping(self) -> bool:
        return self.healthy

    def embedding_ready(self) -> bool:
        return self.embedding_ok

    def business_metrics(self) -> BusinessMetrics | None:
        return self.business

    def _answer(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> QueryResult:
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def structured(self, symbol: str, **kwargs: Any) -> QueryResult:
        return self._answer("structured", (symbol,), kwargs)

    def risk(self, symbol: str, **kwargs: Any) -> QueryResult:
        return self._answer("risk", (symbol,), kwargs)

    def search_announcements(self, symbol: str, text: str, **kwargs: Any) -> QueryResult:
        return self._answer("search_announcements", (symbol, text), kwargs)


def _app(
    service: FakeService | None = None, *, compliance_enabled: bool = False
) -> Any:
    config = Config(db_url=DSN, compliance_enabled=compliance_enabled)
    return create_app(config=config, service=service or FakeService())


def _client(service: FakeService | None = None, *, compliance_enabled: bool = False) -> TestClient:
    return TestClient(_app(service, compliance_enabled=compliance_enabled))


def _strict_client() -> TestClient:
    """Client over the real service with I/O poisoned.

    Every negative case below must be rejected by argument validation, so a
    database connection or embedding request means the contract is wrong.
    """

    config = Config(db_url=DSN, rate_limit_enabled=False)
    service = QueryService(config=config, connector=_no_io)
    return TestClient(create_app(config=config, service=service))


def test_health_ok_reports_envelope_and_switch_state() -> None:
    client = _client(FakeService(compliance_enabled=True), compliance_enabled=True)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["checks"] == {"database": "ok", "embedding": "ok"}
    assert payload["data"]["compliance_enabled"] is True
    assert payload["data"]["version"]
    assert payload["meta"]["trace_id"]


def test_health_degraded_when_database_is_unreachable() -> None:
    client = _client(FakeService(healthy=False))

    payload = client.get("/health").json()

    assert payload["ok"] is True
    assert payload["data"]["status"] == "degraded"
    assert payload["data"]["checks"] == {"database": "unreachable", "embedding": "ok"}
    assert payload["data"]["compliance_enabled"] is False


def test_health_degraded_when_embedding_is_unreachable() -> None:
    client = _client(FakeService(embedding_ok=False))

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["status"] == "degraded"
    assert payload["data"]["checks"] == {"database": "ok", "embedding": "unreachable"}


def test_structured_returns_data_with_service_meta() -> None:
    service = FakeService(
        result=QueryResult(
            data={"symbol": "600000", "years": [{"year": "2025", "revenue": 1.0}]},
            source="dws",
            quality_status="normal",
        )
    )
    client = _client(service)

    payload = client.get("/v1/structured/600000").json()

    assert payload["ok"] is True
    assert payload["data"]["symbol"] == "600000"
    assert payload["meta"]["source"] == "dws"
    assert payload["meta"]["quality_status"] == "normal"
    assert payload["meta"]["compliance_tier"] is None
    assert service.calls == [("structured", ("600000",), {"metric": None, "year_range": None})]


def test_structured_forwards_query_parameters() -> None:
    service = FakeService()
    client = _client(service)

    client.get("/v1/structured/600000", params={"metric": "revenue", "year_range": "2023-2025"})

    assert service.calls[0][2] == {"metric": "revenue", "year_range": "2023-2025"}


def test_compliance_tier_reaches_the_envelope_meta() -> None:
    from financecoffer.core.compliance import ComplianceMark

    service = FakeService(
        result=QueryResult(
            data={"symbol": "600000"},
            source="dws",
            compliance=ComplianceMark(tier="C", commercial_use=False),
        )
    )

    payload = _client(service, compliance_enabled=True).get("/v1/structured/600000").json()

    assert payload["meta"]["compliance_tier"] == "C"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InvalidArgumentError("bad symbol"), 400, "E_INVALID_ARGUMENT"),
        (NotFoundError("unknown symbol"), 404, "E_NOT_FOUND"),
        (DependencyError("database down"), 503, "E_DEPENDENCY"),
    ],
)
def test_service_errors_use_the_stable_status_table(
    error: FinanceCofferError, status: int, code: str
) -> None:
    client = _client(FakeService(error=error))

    response = client.get("/v1/structured/600000")

    assert response.status_code == status
    payload = response.json()
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]


def test_unexpected_errors_become_internal_500() -> None:
    class BoomService(FakeService):
        def structured(self, symbol: str, **kwargs: Any) -> QueryResult:
            raise RuntimeError("kaboom")

    client = TestClient(_app(BoomService()), raise_server_exceptions=False)

    response = client.get("/v1/structured/600000")

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "E_INTERNAL"
    assert "kaboom" not in payload["error"]["message"]


def test_request_validation_errors_are_invalid_argument() -> None:
    client = _client()

    response = client.post(
        "/v1/search/announcements",
        json={"symbol": "600000", "query_text": "", "top_k": MAX_TOP_K + 1},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    fields = {problem["field"] for problem in payload["error"]["details"]["errors"]}
    assert {"query_text", "top_k"} <= fields


def test_search_rejects_unknown_body_fields() -> None:
    service = FakeService()
    client = _client(service)

    response = client.post(
        "/v1/search/announcements",
        json={"symbol": "600000", "query_text": "revenue", "surprise": 1},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_INVALID_ARGUMENT"
    assert service.calls == []


def test_search_forwards_the_body_to_the_service() -> None:
    service = FakeService(result=QueryResult(data={"count": 0, "chunks": []}, source="vector"))
    client = _client(service)

    payload = client.post(
        "/v1/search/announcements",
        json={
            "symbol": "600000",
            "query_text": "revenue growth",
            "year_range": "2023-2025",
            "top_k": 5,
        },
    ).json()

    assert payload["ok"] is True
    assert payload["meta"]["source"] == "vector"
    assert service.calls == [
        (
            "search_announcements",
            ("600000", "revenue growth"),
            {"year_range": "2023-2025", "top_k": 5},
        )
    ]


def test_risk_forwards_since_and_limit() -> None:
    service = FakeService(result=QueryResult(data={"count": 0, "events": []}, source="dwd"))
    client = _client(service)

    client.get("/v1/risk/600000", params={"since": "2025-01-01", "limit": 20})

    assert service.calls == [("risk", ("600000",), {"since": "2025-01-01", "limit": 20})]


def test_risk_limit_is_bounded_by_the_contract() -> None:
    service = FakeService()
    client = _client(service)

    response = client.get("/v1/risk/600000", params={"limit": 0})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_INVALID_ARGUMENT"
    assert service.calls == []


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/v1/structured/12ab", {}),
        ("/v1/structured/ABCDEFGHIJK", {}),
        ("/v1/structured/600000", {"metric": "ebitda"}),
        ("/v1/structured/600000", {"year_range": "2010-2025"}),
        ("/v1/structured/600000", {"year_range": "2025-2021"}),
        ("/v1/structured/600000", {"year_range": "2023/2025"}),
        ("/v1/risk/600000", {"since": "2024/06/01"}),
        ("/v1/risk/600000", {"since": "yesterday"}),
        ("/v1/risk/600000", {"limit": "0"}),
        ("/v1/risk/600000", {"limit": str(MAX_RISK_LIMIT + 1)}),
        ("/v1/risk/600000", {"limit": "abc"}),
    ],
)
def test_invalid_query_parameters_are_rejected_before_io(path: str, params: dict[str, str]) -> None:
    response = _strict_client().get(path, params=params)

    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["message"]


def test_invalid_symbol_reports_the_offending_field() -> None:
    payload = _strict_client().get("/v1/structured/12ab").json()

    assert payload["error"]["details"]["field"] == "symbol"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query_text": "revenue"},
        {"symbol": "600000"},
        {"symbol": "600000", "query_text": "   "},
        {"symbol": "600000", "query_text": "revenue", "top_k": 0},
        {"symbol": "600000", "query_text": "revenue", "top_k": MAX_TOP_K + 1},
        {"symbol": "600000", "query_text": "revenue", "year_range": "2020-2035"},
        {"symbol": "600000", "query_text": "revenue", "year_range": "2025-2024"},
        {"symbol": "12ab", "query_text": "revenue"},
        {"symbol": "600000", "query_text": "x" * (MAX_QUERY_CHARS + 1)},
    ],
)
def test_search_body_contract_negatives(body: dict[str, Any]) -> None:
    response = _strict_client().post("/v1/search/announcements", json=body)

    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"


def test_trace_id_is_echoed_and_invalid_values_are_replaced() -> None:
    client = _client()

    echoed = client.get("/health", headers={"X-Trace-Id": "trace-abc-123"})
    assert echoed.headers["X-Trace-Id"] == "trace-abc-123"
    assert echoed.json()["meta"]["trace_id"] == "trace-abc-123"

    replaced = client.get("/health", headers={"X-Trace-Id": "bad trace!"})
    assert replaced.headers["X-Trace-Id"] != "bad trace!"
    assert replaced.json()["meta"]["trace_id"] == replaced.headers["X-Trace-Id"]


def test_metrics_render_prometheus_text_including_self() -> None:
    client = _client()
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert 'fc_api_requests_total{method="GET",path="/health",status="200"} 1' in text
    assert "# TYPE fc_api_request_seconds_total counter" in text


def test_metrics_render_business_families_from_the_warehouse() -> None:
    service = FakeService(
        business=BusinessMetrics(
            etl_events={"done": 3, "failed": 1},
            dirty_data={"schema_mismatch": 2},
            archive_jobs={"done": 4},
            consume_lag_seconds=12.5,
        )
    )

    text = _client(service).get("/metrics").text

    assert 'fc_etl_events_total{status="done"} 3' in text
    assert 'fc_etl_events_total{status="failed"} 1' in text
    assert 'fc_dirty_data_total{reason="schema_mismatch"} 2' in text
    assert 'fc_archive_job_total{status="done"} 4' in text
    assert "fc_etl_consume_lag_seconds 12.500" in text
    assert "# TYPE fc_etl_consume_lag_seconds gauge" in text


def test_metrics_omit_families_that_have_no_samples() -> None:
    service = FakeService(
        business=BusinessMetrics(
            etl_events={}, dirty_data={}, archive_jobs={}, consume_lag_seconds=None
        )
    )

    text = _client(service).get("/metrics").text

    for family in (
        "fc_etl_events_total",
        "fc_dirty_data_total",
        "fc_archive_job_total",
        "fc_etl_consume_lag_seconds",
    ):
        assert family not in text


def test_metrics_still_render_when_the_warehouse_cannot_be_sampled() -> None:
    response = _client(FakeService(business=None)).get("/metrics")

    assert response.status_code == 200
    assert "fc_api_requests_total" in response.text
    assert "fc_etl_events_total" not in response.text


def test_metrics_include_the_vector_layer_series() -> None:
    pytest.importorskip("financecoffer.vector.metrics")

    text = _client().get("/metrics").text

    assert "# TYPE fc_vector_search_seconds histogram" in text
    assert "fc_missing_chunk_total" in text


def test_rate_limit_returns_the_envelope_and_retry_after() -> None:
    config = Config(db_url=DSN, rate_limit_enabled=False)
    limiter = TokenBucket(rate_per_minute=1, burst=1)
    client = TestClient(create_app(config=config, service=FakeService(), rate_limiter=limiter))

    assert client.get("/v1/structured/600000").status_code == 200

    limited = client.get("/v1/structured/600000")

    assert limited.status_code == 429
    payload = limited.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_RATE_LIMITED"
    assert int(limited.headers["Retry-After"]) >= 1
    assert client.get("/health").status_code == 200


def test_unmatched_route_returns_the_404_envelope() -> None:
    response = _client().get("/v1/nope/600000")

    assert response.status_code == 404
    payload = response.json()
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "E_NOT_FOUND"
    assert payload["error"]["message"]
    # The trace id must be the one the request was instrumented with, so log and
    # body line up; the middleware echoes it on the response header as well.
    assert payload["meta"]["trace_id"]
    assert response.headers["X-Trace-Id"] == payload["meta"]["trace_id"]


def test_wrong_method_returns_the_405_envelope() -> None:
    response = _client().post("/health")

    assert response.status_code == 405
    payload = response.json()
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "E_METHOD_NOT_ALLOWED"
    assert payload["error"]["message"]
    assert payload["meta"]["trace_id"]
    assert response.headers["X-Trace-Id"] == payload["meta"]["trace_id"]


def test_documented_routes_are_served() -> None:
    app = _app()

    paths = {route.path for route in app.routes}

    assert {
        "/health",
        "/metrics",
        "/v1/structured/{symbol}",
        "/v1/risk/{symbol}",
        "/v1/search/announcements",
    } <= paths
