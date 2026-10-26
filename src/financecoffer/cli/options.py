"""Process-wide CLI options parsed once by the root callback."""

from __future__ import annotations

from dataclasses import dataclass, field

from financecoffer.core.trace import new_trace_id

OUTPUT_FORMATS: tuple[str, ...] = ("json", "table", "csv", "yaml")
DETAIL_TIERS: tuple[str, ...] = ("brief", "medium", "full")


@dataclass(slots=True)
class GlobalOptions:
    """Global CLI options from ``docs/specs/cli.md`` section 1."""

    output: str = "json"
    fields: str | None = None
    tier: str = "full"
    no_cache: bool = False
    refresh_cache: bool = False
    timeout: int = 30
    retries: int = 2
    db_url: str | None = None
    trace_id: str = field(default_factory=new_trace_id)
    quiet: bool = False
    verbose: bool = False
