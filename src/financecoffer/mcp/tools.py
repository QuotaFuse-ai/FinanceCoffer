"""Fixed MCP tool catalogue (``financecoffer.md`` section 6.1).

Pure data: no SDK, database or embedding imports, so ``fcoffer mcp tools`` needs
neither the optional ``mcp`` extra nor a reachable warehouse. The live server in
:mod:`financecoffer.mcp.server` registers exactly these names and parameters;
``tests/unit/test_mcp.py`` keeps the two views in sync.
"""

from __future__ import annotations

from typing import Any

from financecoffer.query import STRUCTURED_METRICS

_SYMBOL = {
    "type": "string",
    "description": "6-digit A-share code or US ticker, for example 600519 or AAPL.",
}

_YEAR_RANGE = {
    "anyOf": [{"type": "string"}, {"type": "null"}],
    "default": None,
    "description": "Inclusive fiscal-year window as YYYY-YYYY (at most 10 years).",
}

TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "query_structured_data",
        "description": (
            "Read DWS structured metrics for one symbol, optionally projected to a "
            "single metric and a fiscal-year window. Returns the FinanceCoffer envelope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": dict(_SYMBOL),
                "metric": {
                    "anyOf": [
                        {"type": "string", "enum": list(STRUCTURED_METRICS)},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": (
                        "Optional projection: " + "|".join(STRUCTURED_METRICS) + "."
                    ),
                },
                "year_range": dict(_YEAR_RANGE),
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_announcements_semantically",
        "description": (
            "Search announcement chunks for one symbol semantically. The symbol and "
            "fiscal-year window filter before vector ranking. Returns the "
            "FinanceCoffer envelope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": dict(_SYMBOL),
                "query_text": {
                    "type": "string",
                    "description": "Natural-language query; at most 2000 characters.",
                },
                "year_range": dict(_YEAR_RANGE),
            },
            "required": ["symbol", "query_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_risk_history",
        "description": (
            "Read historical risk events for one symbol from DWD. Returns the "
            "FinanceCoffer envelope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": dict(_SYMBOL)},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
)
