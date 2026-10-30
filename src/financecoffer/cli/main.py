"""Typer application: command tree, global options and error exit mapping."""

from __future__ import annotations

import typer

from financecoffer import __version__
from financecoffer.cli.commands import MODULES
from financecoffer.cli.encoding import ensure_utf8_stdio
from financecoffer.cli.options import (
    DETAIL_TIERS,
    OUTPUT_FORMATS,
    GlobalOptions,
    parse_int_option,
)
from financecoffer.cli.output import emit_envelope, emit_failure
from financecoffer.core.envelope import Envelope
from financecoffer.core.errors import FinanceCofferError, InvalidArgumentError
from financecoffer.core.logging import configure_logging
from financecoffer.core.trace import normalize_trace_id

app = typer.Typer(
    name="fcoffer",
    help="FinanceCoffer CLI - warehouse, retrieval and MCP tooling.",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode=None,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"fcoffer {__version__}")
        raise typer.Exit()


def _choice(value: str, allowed: tuple[str, ...], option: str) -> str:
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise typer.BadParameter(
            f"must be one of: {', '.join(allowed)}",
            param_hint=option,
        )
    return normalized


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
    output: str = typer.Option(
        "json", "--output", "-o", help="Output format: json | table | csv | yaml."
    ),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated field projection."),
    tier: str = typer.Option("full", "--tier", help="Detail tier: brief | medium | full."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the query cache."),
    refresh_cache: bool = typer.Option(
        False, "--refresh-cache", help="Refresh the query cache before running."
    ),
    timeout: str = typer.Option("30", "--timeout", help="Operation timeout in seconds (>= 1)."),
    retries: str = typer.Option("2", "--retries", help="Retries for retryable operations (>= 0)."),
    db_url: str | None = typer.Option(None, "--db-url", help="Override FC_DB_URL."),
    trace_id: str | None = typer.Option(None, "--trace-id", help="Propagate a trace id."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only warnings and errors."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """FinanceCoffer command line interface."""

    if quiet and verbose:
        raise typer.BadParameter("--quiet and --verbose are mutually exclusive")

    try:
        resolved_trace_id = normalize_trace_id(trace_id)
    except InvalidArgumentError as exc:
        raise typer.BadParameter(exc.message, param_hint="--trace-id") from exc

    resolved_output = _choice(output, OUTPUT_FORMATS, "--output")
    resolved_tier = _choice(tier, DETAIL_TIERS, "--tier")
    try:
        resolved_timeout = parse_int_option(timeout, field="timeout", minimum=1)
        resolved_retries = parse_int_option(retries, field="retries", minimum=0)
    except InvalidArgumentError as exc:
        emit_failure(
            GlobalOptions(
                output=resolved_output,
                tier=resolved_tier,
                trace_id=resolved_trace_id,
                quiet=quiet,
            ),
            exc,
        )

    ctx.obj = GlobalOptions(
        output=resolved_output,
        fields=fields,
        tier=resolved_tier,
        no_cache=no_cache,
        refresh_cache=refresh_cache,
        timeout=resolved_timeout,
        retries=resolved_retries,
        db_url=db_url,
        trace_id=resolved_trace_id,
        quiet=quiet,
        verbose=verbose,
    )

    configure_logging("DEBUG" if verbose else ("WARNING" if quiet else None))

    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


for _module in MODULES:
    _module.register(app)


def main() -> None:
    """Console-script entry point (``fcoffer``)."""

    ensure_utf8_stdio()
    try:
        app(prog_name="fcoffer")
    except FinanceCofferError as exc:
        options = GlobalOptions()
        emit_envelope(
            Envelope.from_error(exc, trace_id=options.trace_id),
            quiet=options.quiet,
        )
        raise SystemExit(exc.exit_code) from exc
