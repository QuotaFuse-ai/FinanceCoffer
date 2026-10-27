"""Tests for S-07-2 request-level rate limiting."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from financecoffer.api import create_app
from financecoffer.api.ratelimit import TokenBucket
from financecoffer.config.loader import load_config
from financecoffer.core.config import Config
from financecoffer.core.errors import ConfigError
from financecoffer.query.service import QueryResult

DSN = "postgresql://fcoffer:sup3r-s3cret@127.0.0.1:5432/financecoffer"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class FakeService:
    def __init__(self) -> None:
        self.config = Config(db_url=DSN)

    def ping(self) -> bool:
        return True

    def structured(self, symbol: str, **_: Any) -> QueryResult:
        return QueryResult(data={"symbol": symbol, "years": []}, source="dws")

    def risk(self, symbol: str, **_: Any) -> QueryResult:
        return QueryResult(data={"symbol": symbol, "count": 0, "events": []}, source="dwd")

    def search_announcements(self, symbol: str, text: str, **_: Any) -> QueryResult:
        return QueryResult(data={"symbol": symbol, "count": 0, "results": []}, source="vector")


def _client(*, limiter: TokenBucket | None = None, enabled: bool = True) -> TestClient:
    config = Config(db_url=DSN, rate_limit_enabled=enabled)
    return TestClient(create_app(config=config, service=FakeService(), rate_limiter=limiter))


def test_bucket_allows_a_burst_then_refills_over_time() -> None:
    clock = _Clock()
    bucket = TokenBucket(rate_per_minute=60, burst=3, clock=clock)

    assert [bucket.allow("ip") for _ in range(3)] == [True, True, True]
    assert bucket.allow("ip") is False
    assert bucket.retry_after("ip") == 1

    clock.now += 1.0  # 60/min == one token per second
    assert bucket.allow("ip") is True


def test_buckets_are_isolated_per_client_key() -> None:
    clock = _Clock()
    bucket = TokenBucket(rate_per_minute=60, burst=1, clock=clock)

    assert bucket.allow("10.0.0.1") is True
    assert bucket.allow("10.0.0.1") is False
    assert bucket.allow("10.0.0.2") is True


def test_bucket_map_stays_bounded() -> None:
    clock = _Clock()
    bucket = TokenBucket(rate_per_minute=600, burst=1, clock=clock, max_keys=5)

    for index in range(50):
        bucket.allow(f"10.0.0.{index}")
        clock.now += 1.0

    assert len(bucket._buckets) <= 5  # noqa: SLF001 - bounded growth is the contract


def test_bucket_rejects_non_positive_rates() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate_per_minute=0)


def test_request_above_the_threshold_gets_429_with_a_stable_envelope() -> None:
    limiter = TokenBucket(rate_per_minute=60, burst=2)
    client = _client(limiter=limiter)

    assert client.get("/v1/structured/600000").status_code == 200
    assert client.get("/v1/risk/600000").status_code == 200

    response = client.get("/v1/structured/600000")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.headers["X-Trace-Id"]
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_RATE_LIMITED"
    assert payload["error"]["details"]["limit_per_minute"] == 60
    assert payload["meta"]["trace_id"] == response.headers["X-Trace-Id"]


def test_search_route_is_also_rate_limited() -> None:
    client = _client(limiter=TokenBucket(rate_per_minute=60, burst=1))

    body = {"symbol": "600000", "query_text": "revenue"}
    assert client.post("/v1/search/announcements", json=body).status_code == 200
    assert client.post("/v1/search/announcements", json=body).status_code == 429


def test_disabled_switch_removes_limiting_entirely() -> None:
    client = _client(enabled=False)

    for _ in range(20):
        assert client.get("/v1/structured/600000").status_code == 200


def test_health_and_metrics_are_exempt_from_limiting() -> None:
    clock = _Clock()
    client = _client(limiter=TokenBucket(rate_per_minute=60, burst=1, clock=clock))

    for _ in range(5):
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 200

    assert client.get("/v1/structured/600000").status_code == 200
    assert client.get("/v1/structured/600000").status_code == 429


def test_rate_limit_settings_come_from_the_environment() -> None:
    config = load_config(
        env={"FC_RATE_LIMIT_ENABLED": "false", "FC_RATE_LIMIT_PER_MINUTE": "30"}
    )

    assert config.rate_limit_enabled is False
    assert config.rate_limit_per_minute == 30


def test_rate_limit_defaults_are_loose_and_on() -> None:
    config = load_config(env={})

    assert config.rate_limit_enabled is True
    assert config.rate_limit_per_minute == 120


def test_invalid_rate_limit_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        load_config(env={"FC_RATE_LIMIT_PER_MINUTE": "0"})
