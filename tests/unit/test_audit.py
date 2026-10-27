"""Tests for S-10-1 structured query audit logs (REST and CLI)."""

from __future__ import annotations

import io
import json
from typing import Any

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from financecoffer.api import create_app
from financecoffer.cli import app as cli_app
from financecoffer.cli.commands import query as query_module
from financecoffer.core.config import Config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import NotFoundError
from financecoffer.core.logging import configure_logging, log_query_audit
from financecoffer.query.service import QueryResult

DSN = "postgresql://fcoffer:sup3r-s3cret@127.0.0.1:5432/financecoffer"
AUDIT_FIELDS = {
    "trace_id",
    "tool",
    "symbol",
    "year_range",
    "since",
    "top_k",
    "hits",
    "elapsed_ms",
    "outcome",
}


class FakeService:
    def __init__(
        self,
        *,
        result: QueryResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.config = Config(db_url=DSN)
        self.result = result
        self.error = error

    def ping(self) -> bool:
        return True

    def structured(self, symbol: str, **_: Any) -> QueryResult:
        if self.error is not None:
            raise self.error
        return self.result or QueryResult(data={"symbol": symbol, "years": [{}]}, source="dws")

    def risk(self, symbol: str, **_: Any) -> QueryResult:
        return self.result or QueryResult(
            data={"symbol": symbol, "count": 3, "events": []}, source="dwd"
        )

    def search_announcements(self, symbol: str, text: str, **_: Any) -> QueryResult:
        return self.result or QueryResult(
            data={"symbol": symbol, "count": 0, "results": []}, source="vector"
        )


def _client(service: FakeService | None = None) -> TestClient:
    return TestClient(create_app(config=Config(db_url=DSN), service=service or FakeService()))


def _audit_records(stream: io.StringIO) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in stream.getvalue().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("logger") == "financecoffer.audit":
            records.append(payload)
    return records


def _single_audit(stream: io.StringIO) -> dict[str, Any]:
    records = _audit_records(stream)
    assert len(records) == 1, records
    return records[0]


def test_audit_helper_emits_every_documented_field() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    log_query_audit(
        tool="query.structured",
        trace_id="trace-1",
        symbol="600000",
        year_range="2023-2025",
        since=None,
        top_k=None,
        hits=2,
        elapsed_ms=1.23456,
        outcome="ok",
    )

    record = _single_audit(stream)
    assert AUDIT_FIELDS <= set(record)
    assert record["trace_id"] == "trace-1"
    assert record["elapsed_ms"] == 1.235
    assert "query_text" not in record


def test_rest_structured_writes_a_metadata_only_audit_record() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    service = FakeService(
        result=QueryResult(
            data={"symbol": "600000", "years": [{"year": "2024"}, {"year": "2025"}]},
            source="dws",
        )
    )

    response = _client(service).get(
        "/v1/structured/600000",
        params={"year_range": "2023-2025"},
        headers={"X-Trace-Id": "audit-trace-1"},
    )

    assert response.status_code == 200
    record = _single_audit(stream)
    assert record["trace_id"] == "audit-trace-1"
    assert record["tool"] == "GET /v1/structured/{symbol}"
    assert record["symbol"] == "600000"
    assert record["year_range"] == "2023-2025"
    assert record["hits"] == 2
    assert record["outcome"] == "ok"
    assert isinstance(record["elapsed_ms"], float)


def test_rest_risk_audits_since_and_hits() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    response = _client().get("/v1/risk/600000", params={"since": "2025-01-01"})

    assert response.status_code == 200
    record = _single_audit(stream)
    assert record["tool"] == "GET /v1/risk/{symbol}"
    assert record["since"] == "2025-01-01"
    assert record["hits"] == 3


def test_rest_search_audits_metadata_but_never_query_text() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    query_text = "acquisition plan for the secret target"

    response = _client().post(
        "/v1/search/announcements",
        json={
            "symbol": "600000",
            "query_text": query_text,
            "year_range": "2024-2025",
            "top_k": 5,
        },
    )

    assert response.status_code == 200
    record = _single_audit(stream)
    assert record["tool"] == "POST /v1/search/announcements"
    assert record["year_range"] == "2024-2025"
    assert record["top_k"] == 5
    assert record["hits"] == 0
    assert query_text not in stream.getvalue()


def test_failed_queries_audit_the_error_code() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    service = FakeService(error=NotFoundError("unknown symbol"))

    response = _client(service).get("/v1/structured/600000")

    assert response.status_code == 404
    record = _single_audit(stream)
    assert record["outcome"] == "E_NOT_FOUND"
    assert record["hits"] is None


def _cli_result(data: dict[str, Any]) -> Any:
    class _Result:
        def __init__(self) -> None:
            self.data = data

        @staticmethod
        def meta(trace_id: str | None = None) -> Meta:
            return Meta(source="dws")

    return _Result()


def test_cli_query_commands_emit_audit_records_without_query_text(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(query_module, "log_query_audit", lambda **kwargs: calls.append(kwargs))

    class _Service:
        def __init__(self, **_: object) -> None:
            pass

        def structured(self, symbol: str, **_: object) -> Any:
            return _cli_result({"symbol": symbol, "years": [{}, {}]})

        def risk(self, symbol: str, **_: object) -> Any:
            return _cli_result({"symbol": symbol, "count": 1, "events": []})

        def search_announcements(self, symbol: str, text: str, **_: object) -> Any:
            return _cli_result({"symbol": symbol, "count": 4, "results": []})

    monkeypatch.setattr(query_module, "QueryService", _Service)
    runner = CliRunner()

    assert runner.invoke(cli_app, ["query", "structured", "--symbol", "600000"]).exit_code == 0
    assert runner.invoke(cli_app, ["query", "risk", "--symbol", "600000"]).exit_code == 0
    assert (
        runner.invoke(
            cli_app,
            ["search", "announcements", "--symbol", "600000", "--query", "secret plan"],
        ).exit_code
        == 0
    )

    assert [call["tool"] for call in calls] == [
        "query.structured",
        "query.risk",
        "search.announcements",
    ]
    assert [call["outcome"] for call in calls] == ["ok", "ok", "ok"]
    assert [call["hits"] for call in calls] == [2, 1, 4]
    assert calls[2]["top_k"] == 10
    assert all("query_text" not in call for call in calls)
