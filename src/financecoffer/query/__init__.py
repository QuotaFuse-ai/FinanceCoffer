"""Read-side query services shared by the CLI, REST API and MCP.

Every function here is pure read: it never writes to ODS/DWD/DWS and never
re-validates upstream quality data. The SQL is always parameterized and always
filtered by ``symbol`` first (S-01/S-02).
"""

from financecoffer.query.service import (
    MAX_QUERY_CHARS,
    MAX_RISK_LIMIT,
    MAX_TOP_K,
    MAX_YEAR_SPAN,
    STRUCTURED_METRICS,
    QueryResult,
    QueryService,
    normalize_symbol,
    parse_since,
    parse_year_range,
)

__all__ = [
    "MAX_QUERY_CHARS",
    "MAX_RISK_LIMIT",
    "MAX_TOP_K",
    "MAX_YEAR_SPAN",
    "STRUCTURED_METRICS",
    "QueryResult",
    "QueryService",
    "normalize_symbol",
    "parse_since",
    "parse_year_range",
]
