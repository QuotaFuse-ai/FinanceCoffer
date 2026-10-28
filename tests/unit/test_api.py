from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import redis
import yaml
from fastapi.testclient import TestClient

from financecoffer.api import create_app
from financecoffer.api.health import ETL_LAG_DEGRADED_SECONDS, etl_check
from financecoffer.api.ratelimit import TokenBucket
from financecoffer.core.config import Config
from financecoffer.core.errors import (
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
    NotFoundError,
    NotImplementedYetError,
    StorageError,
)
from financecoffer.etl.processor import classify_failure
from financecoffer.query.service import (
    MAX_QUERY_CHARS,
    MAX_RISK_LIMIT,
    MAX_TOP_K,
    BusinessMetrics,
    QueryResult,
    QueryService,
)

DSN = "postgresql://fcoffer:secret@127.0.0.1:5432/financecoffer"
ROOT = Path(__file__).resolve().parents[2]


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
        queue: tuple[int, float | None] | None = (0, 0.0),
    ) -> None:
        self.config = Config(db_url=DSN, compliance_enabled=compliance_enabled)
        self.healthy = healthy
        self.embedding_ok = embedding_ok
        self.result = result or QueryResult(data={"symbol": "600000"}, source="dws")
        self.error = error
        self.business = business
        self.queue = queue
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.probes: list[str] = []

    def ping(self) -> bool:
        self.probes.append("database")
        return self.healthy

    def embedding_ready(self) -> bool:
        self.probes.append("embedding")
        return self.embedding_ok

    def business_metrics(self) -> BusinessMetrics | None:
        return self.business

    def etl_queue(self) -> tuple[int, float | None] | None:
        return self.queue

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


class _FakeRedis:
    """Redis client stand-in so the health tests never open a socket."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.closed = False

    def ping(self) -> bool:
        if self.error is not None:
            raise self.error
        return True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def redis_server(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _FakeRedis]:
    """Replace ``Redis.from_url``; call the fixture to choose the fake's answer."""

    state: dict[str, _FakeRedis] = {"client": _FakeRedis()}

    def build(*args: Any, **kwargs: Any) -> _FakeRedis:
        return state["client"]

    def use(error: BaseException | None = None) -> _FakeRedis:
        state["client"] = _FakeRedis(error=error)
        return state["client"]

    monkeypatch.setattr(redis.Redis, "from_url", build)
    return use


def test_health_ok_reports_envelope_and_switch_state(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    redis_server()
    client = _client(FakeService(compliance_enabled=True), compliance_enabled=True)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["status"] == "ok"
    checks = payload["data"]["checks"]
    assert checks["database"]["status"] == "ok"
    assert checks["embedding"]["status"] == "ok"
    assert checks["redis"]["status"] == "ok"
    assert payload["data"]["compliance_enabled"] is True
    assert payload["data"]["version"]
    assert payload["meta"]["trace_id"]


def test_health_checks_all_share_one_shape_and_vocabulary(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    redis_server()

    checks = _client(FakeService()).get("/health").json()["data"]["checks"]

    assert set(checks) == {"database", "embedding", "redis", "etl"}
    for name, check in checks.items():
        assert isinstance(check, dict), name
        assert check["status"] in {"ok", "degraded", "unreachable", "not_configured"}, name
    # elapsed_ms is present wherever a probe actually measured something.
    for name in ("database", "embedding", "redis"):
        assert isinstance(checks[name]["elapsed_ms"], float), name
        assert checks[name]["elapsed_ms"] >= 0, name


def test_health_degraded_when_database_is_unreachable(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    redis_server()
    client = _client(FakeService(healthy=False))

    payload = client.get("/health").json()

    assert payload["ok"] is True
    assert payload["data"]["status"] == "degraded"
    assert payload["data"]["checks"]["database"]["status"] == "unreachable"
    assert isinstance(payload["data"]["checks"]["database"]["elapsed_ms"], float)
    assert payload["data"]["checks"]["embedding"]["status"] == "ok"
    assert payload["data"]["compliance_enabled"] is False


def test_health_degrades_when_database_is_not_configured(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    redis_server()
    service = FakeService()
    service.config = Config(db_url=None)

    payload = _client(service).get("/health").json()

    database = payload["data"]["checks"]["database"]
    assert database == {"status": "not_configured"}
    # No probe was issued, so there is no measurement to report.
    assert "elapsed_ms" not in database
    # An unconfigured core dependency cannot serve reads: the process is not ready.
    assert payload["data"]["status"] == "degraded"


def test_health_degrades_when_embedding_url_is_not_configured(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    redis_server()
    service = FakeService()
    service.config = Config.model_construct(embedding_url="")

    payload = _client(service).get("/health").json()

    assert payload["data"]["checks"]["embedding"] == {"status": "not_configured"}
    # Nothing was dialled: an unconfigured endpoint is not probed.
    assert "embedding" not in service.probes
    assert payload["data"]["status"] == "degraded"


def test_health_degraded_when_embedding_is_unreachable(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    redis_server()
    client = _client(FakeService(embedding_ok=False))

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["status"] == "degraded"
    assert payload["data"]["checks"]["database"]["status"] == "ok"
    assert payload["data"]["checks"]["embedding"]["status"] == "unreachable"


def test_health_reports_redis_as_ok_with_its_own_timing(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    fake = redis_server()

    response = _client(FakeService()).get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["checks"]["redis"]["status"] == "ok"
    assert isinstance(payload["data"]["checks"]["redis"]["elapsed_ms"], float)
    assert payload["data"]["checks"]["redis"]["elapsed_ms"] >= 0
    assert payload["data"]["status"] == "ok"
    assert fake.closed is True


def test_health_stays_200_and_keeps_its_shape_when_redis_is_unreachable(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    redis_server(redis.exceptions.ConnectionError("connection refused"))

    response = _client(FakeService()).get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    checks = payload["data"]["checks"]
    assert set(checks) == {"database", "embedding", "redis", "etl"}
    assert checks["redis"]["status"] == "unreachable"
    assert isinstance(checks["redis"]["elapsed_ms"], float)
    # The aggregate status still turns on database and embedding only.
    assert payload["data"]["status"] == "ok"


def test_health_reports_redis_as_not_configured_without_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A base install (no ``financecoffer[etl]`` extra) has no redis client, so
    # the probe can never dial: that is ``not_configured``, not ``unreachable``.
    monkeypatch.setitem(sys.modules, "redis", None)

    payload = _client(FakeService()).get("/health").json()

    assert payload["data"]["checks"]["redis"]["status"] == "not_configured"
    assert isinstance(payload["data"]["checks"]["redis"]["elapsed_ms"], float)
    # Redis never moves the aggregate, whatever it reports.
    assert payload["data"]["status"] == "ok"


class _FakeCursor:
    """Psycopg cursor stand-in: answers the ETL queue query, records the SQL."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row
        self.statements: list[str] = []

    def execute(self, sql: str, *args: Any) -> _FakeCursor:
        self.statements.append(sql)
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeSession:
    """Psycopg connection stand-in so the ETL probe never opens a socket."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.cursor_obj = _FakeCursor(row)

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _etl_service(row: tuple[Any, ...] | None) -> QueryService:
    """Real ``QueryService`` over a fake session (``timeout=0`` skips the
    ``statement_timeout`` preamble, which the fake does not implement)."""

    return QueryService(
        config=Config(db_url=DSN),
        timeout=0,
        connector=lambda *args, **kwargs: _FakeSession(row),
    )


def test_etl_probe_reports_zero_when_nothing_is_queued() -> None:
    check = etl_check(_etl_service((0, None)))

    assert check == {"status": "ok", "lag_seconds": 0.0, "pending": 0}


def test_etl_probe_calls_a_short_backlog_ok() -> None:
    """A backlog below the alert threshold is normal consumption in progress."""

    check = etl_check(_etl_service((3, 42.5)))

    assert check == {"status": "ok", "lag_seconds": 42.5, "pending": 3}


def test_etl_probe_degrades_past_the_alert_threshold() -> None:
    check = etl_check(_etl_service((3, ETL_LAG_DEGRADED_SECONDS + 1)))

    assert check["status"] == "degraded"
    assert check["pending"] == 3
    assert check["lag_seconds"] == ETL_LAG_DEGRADED_SECONDS + 1


def test_etl_probe_degrades_a_backlog_of_unknown_age() -> None:
    """Pending rows with no ``received_at`` are a backlog we cannot date: still
    a backlog, so it must not be reported as ``ok``."""

    check = etl_check(_etl_service((4, None)))

    assert check == {"status": "degraded", "lag_seconds": None, "pending": 4}


def test_etl_probe_reports_unreachable_when_it_cannot_sample() -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("warehouse down")

    service = QueryService(config=Config(db_url=DSN), timeout=0, connector=boom)

    assert etl_check(service) == {
        "status": "unreachable",
        "lag_seconds": None,
        "pending": None,
    }


def test_etl_degraded_threshold_matches_the_alert_rule() -> None:
    """The probe and ``FinanceCofferEtlConsumeLagHigh`` must judge the same lag."""

    rules = yaml.safe_load((ROOT / "deploy" / "prometheus" / "rules.yml").read_text("utf-8"))
    expressions = {
        rule["alert"]: rule["expr"]
        for group in rules["groups"]
        for rule in group["rules"]
    }

    assert expressions["FinanceCofferEtlConsumeLagHigh"] == (
        f"fc_etl_consume_lag_seconds > {ETL_LAG_DEGRADED_SECONDS:g}"
    )


def test_health_reports_the_etl_queue_without_touching_the_aggregate(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    redis_server()

    payload = _client(FakeService(queue=(2, 7.25))).get("/health").json()

    checks = payload["data"]["checks"]
    assert checks["etl"] == {"status": "ok", "lag_seconds": 7.25, "pending": 2}
    # Like redis, the etl probe is reported for visibility only.
    assert payload["data"]["status"] == "ok", checks


def test_health_stays_ok_when_the_etl_queue_is_degraded(
    redis_server: Callable[..., _FakeRedis],
) -> None:
    redis_server()

    payload = _client(FakeService(queue=(9, 3600.0))).get("/health").json()

    assert payload["data"]["checks"]["etl"]["status"] == "degraded"
    assert payload["data"]["status"] == "ok"


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


def test_metrics_render_every_business_family_at_zero_when_idle() -> None:
    service = FakeService(
        business=BusinessMetrics(
            etl_events={}, dirty_data={}, archive_jobs={}, consume_lag_seconds=0.0
        )
    )

    text = _client(service).get("/metrics").text

    # A registered family never disappears: an idle warehouse reports zeros.
    assert "# TYPE fc_etl_events_total counter" in text
    assert 'fc_etl_events_total{status="received"} 0' in text
    assert 'fc_etl_events_total{status="duplicate"} 0' in text
    assert 'fc_dirty_data_total{reason="validation"} 0' in text
    assert 'fc_archive_job_total{status="pending"} 0' in text
    assert 'fc_archive_job_total{status="failed"} 0' in text
    assert "fc_etl_consume_lag_seconds 0.000" in text


def test_metrics_baseline_covers_every_status_the_ddl_allows() -> None:
    from financecoffer.api.metrics import (
        ARCHIVE_JOB_STATUSES,
        DIRTY_REASONS,
        ETL_EVENT_STATUSES,
    )

    migration = (ROOT / "src/financecoffer/db/migrations/0002_ops.sql").read_text(
        encoding="utf-8"
    )

    for status in ETL_EVENT_STATUSES:
        assert f"'{status}'" in migration
    for status in ARCHIVE_JOB_STATUSES:
        assert f"'{status}'" in migration

    # The dirty-reason baseline must stay producible by the classifier.
    reasons = {
        classify_failure(exc).reason
        for exc in (
            InvalidArgumentError("bad", details={"reason": "validation"}),
            NotImplementedYetError("unsupported"),
            StorageError("storage"),
            DependencyError("dependency"),
            OSError("internal"),
        )
    }
    assert reasons <= set(DIRTY_REASONS)


def test_metrics_still_render_every_family_when_the_warehouse_cannot_be_sampled() -> None:
    response = _client(FakeService(business=None)).get("/metrics")

    assert response.status_code == 200
    assert "fc_api_requests_total" in response.text
    assert "# TYPE fc_etl_events_total counter" in response.text
    assert 'fc_etl_events_total{status="received"} 0' in response.text
    assert 'fc_archive_job_total{status="pending"} 0' in response.text


def test_metrics_export_the_scrape_series_on_a_cold_process() -> None:
    """The very first scrape must already list ``fc_api_requests_total``."""

    text = _client().get("/metrics").text

    assert 'fc_api_requests_total{method="GET",path="/metrics",status="200"} 0' in text


def test_metrics_are_prometheus_text_whatever_accept_says() -> None:
    """``/metrics`` is text-only by contract (DEC-0022); JSON comes from the CLI."""

    response = _client().get("/metrics", headers={"Accept": "application/json"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "# TYPE fc_etl_events_total counter" in response.text


def test_metrics_are_not_rate_limited() -> None:
    limiter = TokenBucket(rate_per_minute=1, burst=1)
    config = Config(db_url=DSN, rate_limit_enabled=False)
    client = TestClient(
        create_app(config=config, service=FakeService(), rate_limiter=limiter)
    )
    assert client.get("/v1/structured/600000").status_code == 200

    for _ in range(3):
        response = client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")


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
