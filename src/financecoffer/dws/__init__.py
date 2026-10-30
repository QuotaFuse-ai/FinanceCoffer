"""DWS wide-table build: one deterministic row per symbol from DWD facts."""

from __future__ import annotations

from financecoffer.dws.builder import (
    DEFAULT_FISCAL_YEARS,
    MAX_FISCAL_YEARS,
    BuildReport,
    DwsService,
    SymbolResult,
    money,
    parse_as_of,
    rate,
    resolve_default_basis,
    validate_fiscal_years,
    years_back,
)

__all__ = [
    "DEFAULT_FISCAL_YEARS",
    "MAX_FISCAL_YEARS",
    "BuildReport",
    "DwsService",
    "SymbolResult",
    "money",
    "parse_as_of",
    "rate",
    "resolve_default_basis",
    "validate_fiscal_years",
    "years_back",
]
