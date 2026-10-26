from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from financecoffer.api import create_app
from financecoffer.core.config import Config
from financecoffer.core.errors import (
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
    NotFoundError,
)
from financecoffer.query.service import MAX_TOP_K, QueryResult

DSN = "postgresql://fcoffer:secret@127.0.0.1:5432/financecoffer"


class FakeService:
    """In-memory ``QueryService`` stand-in for the HTTP layer."""

    def __init__(
        self,
        *,
        healthy: bool = True,
        result: QueryResult | None = None,
        error: FinanceCofferError | None = None,
        compliance_enabled: bool = False,
    ) -> None:
        self.config = Config(db_url=DSN, compliance_enabled=compliance_enabled)
        self.healthy = healthy
        self.result = result or QueryResult(data={"symbol": "600000"}, source="dws")
        self.error = error
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def ping(self) -> bool:
        return self.healthy

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


def test_health_ok_reports_envelope_and_switch_state() -> None:
    client = _client(FakeService(compliance_enabled=True), compliance_enabled=True)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["checks"] == {"database": "ok"}
    assert payload["data"]["compliance_enabled"] is True
    assert payload["data"]["version"]
    assert payload["meta"]["trace_id"]


def test_health_degraded_when_database_is_unreachable() -> None:
    client = _client(FakeService(healthy=False))

    payload = client.get("/health").json()

    assert payload["ok"] is True
    assert payload["data"]["status"] == "degraded"
    assert payload["data"]["checks"] == {"database": "unreachable"}
    assert payload["data"]["compliance_enabled"] is False


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


def test_unknown_route_is_a_404() -> None:
    response = _client().get("/v1/nope/600000")

    assert response.status_code == 404


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
