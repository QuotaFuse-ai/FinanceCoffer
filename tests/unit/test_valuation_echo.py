"""Echo contract for the persisted valuation basis (QUOT-244).

The read side already picks a basis from the wide row; these tests pin what the
row's ``default_basis`` means on both surfaces. One fixture per shape, driven
through the real :class:`QueryService` so REST and MCP are proven to agree:

* a row built with ``default_basis: reference`` echoes ``reference`` on both
  surfaces, without a per-request flag;
* asking for a basis the row never built reports ``basis_not_built`` and does
  not fall back to the other basis' numbers;
* a legacy row with no ``bases``/``default_basis`` folds into ``snapshot`` and
  still answers.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import anyio
from fastapi.testclient import TestClient

from financecoffer.api import create_app
from financecoffer.core.config import Config
from financecoffer.mcp.server import create_server
from financecoffer.query.service import QueryService

DSN = "postgresql://fcoffer:secret@127.0.0.1:5432/financecoffer"

COLUMNS = [
    "symbol",
    "as_of",
    "metrics_json",
    "risk_summary",
    "latest_announcements",
    "fiscal_years",
    "source_version",
    "built_at",
]


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]], columns: list[str]) -> None:
        self._rows = rows
        self.description = [(name,) for name in columns]

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, _sql: str, _params: tuple[Any, ...] = ()) -> None:
        pass

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def cursor(self) -> _Cursor:
        return self._cursor

    def execute(self, _sql: str, _params: tuple[Any, ...] = ()) -> None:
        pass


def _row_from(metrics: dict[str, Any]) -> tuple[Any, ...]:
    return (
        "600000",
        date(2026, 9, 30),
        metrics,
        {"counts": {"high": 0}},
        [],
        [2023, 2024],
        3,
        datetime(2026, 10, 1, 12, 0),
    )


def _service(metrics: dict[str, Any]) -> QueryService:
    config = Config(db_url=DSN, rate_limit_enabled=False)
    rows = [_row_from(metrics)]
    return QueryService(config=config, connector=lambda *a, **k: _Conn(_Cursor(rows, COLUMNS)))


def _mcp_valuation(service: QueryService, **arguments: Any) -> dict[str, Any]:
    server = create_server(service)
    result = anyio.run(
        lambda: server.call_tool("query_structured_data", {"symbol": "600000", **arguments})
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    return payload["data"]["valuation"]


def _rest_valuation(service: QueryService, **params: Any) -> dict[str, Any]:
    client = TestClient(create_app(config=service.config, service=service))
    response = client.get("/v1/structured/600000", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]["valuation"]


def _two_basis_metrics(default_basis: str) -> dict[str, Any]:
    """The shape ``DwsService._compose`` writes, mirror included."""

    bases = {
        "snapshot": {
            "pe": 15.0,
            "pe_source": "quote.pe_ttm",
            "pe_reason": None,
            "pb": None,
            "pb_reason": "no_equity",
            "roe": None,
            "roe_reason": "equity_unavailable",
        },
        "reference": {
            "pe": 10.0,
            "pe_source": "deducted_annualized",
            "pe_reason": None,
            "pb": 1.5,
            "pb_reason": None,
            "roe": None,
            "roe_reason": "equity_unavailable",
        },
    }
    mirror = bases[default_basis]
    return {
        "years": [{"year": 2025, "revenue": "1.0000"}],
        "valuation": {
            "as_of": "2026-09-30",
            "price_date": "2026-09-30",
            "price_close": "10.0000",
            "default_basis": default_basis,
            "pe_source": mirror["pe_source"],
            "pe": mirror["pe"],
            "pe_reason": mirror["pe_reason"],
            "pb": mirror["pb"],
            "pb_reason": mirror["pb_reason"],
            "bases": bases,
        },
    }


def test_rest_and_mcp_echo_the_persisted_default_basis() -> None:
    service = _service(_two_basis_metrics("reference"))

    rest = _rest_valuation(service)
    mcp = _mcp_valuation(service)

    for valuation in (rest, mcp):
        assert valuation["basis"] == "reference"
        assert valuation["default_basis"] == "reference"
        assert valuation["available_bases"] == ["snapshot", "reference"]
        assert valuation["pe"] == 10.0
        assert valuation["pb"] == 1.5
        assert valuation["pe_reason"] is None
    # The two surfaces must not diverge about a row they both read.
    assert rest == mcp


def test_both_surfaces_switch_basis_per_request_without_moving_the_default() -> None:
    service = _service(_two_basis_metrics("reference"))

    rest = _rest_valuation(service, valuation_basis="snapshot")
    mcp = _mcp_valuation(service, valuation_basis="snapshot")

    for valuation in (rest, mcp):
        assert valuation["basis"] == "snapshot"
        assert valuation["default_basis"] == "reference"
        assert valuation["pe"] == 15.0
    assert rest == mcp


def test_unbuilt_basis_is_explained_not_substituted() -> None:
    metrics = _two_basis_metrics("snapshot")
    del metrics["valuation"]["bases"]["reference"]
    service = _service(metrics)

    for valuation in (
        _rest_valuation(service, valuation_basis="reference"),
        _mcp_valuation(service, valuation_basis="reference"),
    ):
        assert valuation["basis"] == "reference"
        assert valuation["default_basis"] == "snapshot"
        assert valuation["available_bases"] == ["snapshot"]
        assert valuation["pe"] is None
        assert valuation["pe_reason"] == "basis_not_built"
        # Never the snapshot number wearing the reference label.
        assert valuation["pe"] != 15.0


def test_legacy_row_without_a_basis_map_folds_into_snapshot() -> None:
    legacy = {
        "years": [{"year": 2025, "revenue": "1.0000"}],
        "valuation": {
            "as_of": "2026-09-30",
            "price_date": "2026-09-30",
            "price_close": "10.0000",
            "pe": 15.0,
            "pe_source": "quote.pe_ttm",
            "pe_reason": None,
        },
    }
    service = _service(legacy)

    for valuation in (_rest_valuation(service), _mcp_valuation(service)):
        assert valuation["basis"] == "snapshot"
        assert valuation["default_basis"] == "snapshot"
        assert valuation["available_bases"] == ["snapshot"]
        assert valuation["pe"] == 15.0

    for valuation in (
        _rest_valuation(service, valuation_basis="reference"),
        _mcp_valuation(service, valuation_basis="reference"),
    ):
        assert valuation["pe"] is None
        assert valuation["pe_reason"] == "basis_not_built"
