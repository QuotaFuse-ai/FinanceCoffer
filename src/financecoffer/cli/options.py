"""Process-wide CLI options parsed once by the root callback."""

from __future__ import annotations

from dataclasses import dataclass, field

import typer

from financecoffer.core.errors import InvalidArgumentError
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


def from_context(ctx: typer.Context) -> GlobalOptions:
    """Return the options parsed by the root callback (defaults outside tests)."""

    return ctx.obj if isinstance(ctx.obj, GlobalOptions) else GlobalOptions()


def _range_hint(minimum: int | None, maximum: int | None) -> str:
    if minimum is not None and maximum is not None:
        return f" between {minimum} and {maximum}"
    if minimum is not None:
        return f" >= {minimum}"
    if maximum is not None:
        return f" <= {maximum}"
    return ""


def parse_int_option(
    value: str,
    *,
    field: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Parse a bounded numeric option into its inclusive range.

    Numeric options are declared as ``str`` so values such as ``abc`` reach the
    command instead of stopping at Typer's type conversion, which exits without
    a JSON envelope. Every rejected value therefore leaves through the same
    ``InvalidArgumentError`` path as the rest of the CLI.
    """

    try:
        parsed = int(value.strip())
    except (AttributeError, ValueError) as exc:
        raise InvalidArgumentError(
            f"{field} must be an integer{_range_hint(minimum, maximum)}",
            details={"field": field, "value": value},
        ) from exc
    below = minimum is not None and parsed < minimum
    above = maximum is not None and parsed > maximum
    if below or above:
        raise InvalidArgumentError(
            f"{field} must be{_range_hint(minimum, maximum)}",
            details={"field": field, "value": parsed},
        )
    return parsed
