"""Query commands (CLI equivalents of the MCP tools)."""

from __future__ import annotations

import time

import typer

from financecoffer.cli.options import GlobalOptions, from_context, parse_int_option
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_cli_config
from financecoffer.core.errors import FinanceCofferError
from financecoffer.core.logging import log_query_audit
from financecoffer.query import MAX_RISK_LIMIT, MAX_TOP_K, STRUCTURED_METRICS, QueryService


def _service(options: GlobalOptions) -> QueryService:
    try:
        config = load_cli_config(db_url=options.db_url)
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    return QueryService(config=config, dsn=options.db_url, timeout=options.timeout)


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def register(app: typer.Typer) -> None:
    query = add_group(app, name="query", help_text="Structured and risk queries.")

    @query.command("structured", help="Query DWS structured metrics.")
    def structured(
        ctx: typer.Context,
        symbol: str = typer.Option(..., "--symbol", help="A-share code or US ticker."),
        metric: str | None = typer.Option(
            None,
            "--metric",
            help="Metric to project: " + "|".join(STRUCTURED_METRICS) + ".",
        ),
        year_range: str | None = typer.Option(
            None, "--year-range", help="Inclusive window as YYYY-YYYY (max 10 years)."
        ),
    ) -> None:
        options = from_context(ctx)
        started = time.perf_counter()
        try:
            result = _service(options).structured(symbol, metric=metric, year_range=year_range)
        except FinanceCofferError as exc:
            log_query_audit(
                tool="query.structured",
                trace_id=options.trace_id,
                symbol=symbol,
                year_range=year_range,
                elapsed_ms=_ms(started),
                outcome=exc.code.value,
            )
            emit_failure(options, exc)
        log_query_audit(
            tool="query.structured",
            trace_id=options.trace_id,
            symbol=symbol,
            year_range=year_range,
            hits=len(result.data.get("years") or ()),
            elapsed_ms=_ms(started),
            outcome="ok",
        )
        emit_success(options, result.data, meta=result.meta(options.trace_id))

    @query.command("risk", help="Query historical risk events.")
    def risk(
        ctx: typer.Context,
        symbol: str = typer.Option(..., "--symbol", help="A-share code or US ticker."),
        since: str | None = typer.Option(None, "--since", help="Earliest occur_date, YYYY-MM-DD."),
        limit: str = typer.Option(
            "50", "--limit", help=f"Maximum number of events (1-{MAX_RISK_LIMIT})."
        ),
    ) -> None:
        options = from_context(ctx)
        started = time.perf_counter()
        limit_value: int | None = None
        try:
            limit_value = parse_int_option(limit, field="limit", minimum=1, maximum=MAX_RISK_LIMIT)
            result = _service(options).risk(symbol, since=since, limit=limit_value)
        except FinanceCofferError as exc:
            log_query_audit(
                tool="query.risk",
                trace_id=options.trace_id,
                symbol=symbol,
                since=since,
                elapsed_ms=_ms(started),
                outcome=exc.code.value,
            )
            emit_failure(options, exc)
        log_query_audit(
            tool="query.risk",
            trace_id=options.trace_id,
            symbol=symbol,
            since=since,
            hits=result.data.get("count"),
            elapsed_ms=_ms(started),
            outcome="ok",
        )
        emit_success(options, result.data, meta=result.meta(options.trace_id))

    search = add_group(app, name="search", help_text="Semantic search.")

    @search.command("announcements", help="Search announcements semantically.")
    def search_announcements(
        ctx: typer.Context,
        symbol: str = typer.Option(..., "--symbol", help="A-share code or US ticker."),
        query_text: str = typer.Option(..., "--query", help="Natural-language query."),
        year_range: str | None = typer.Option(
            None, "--year-range", help="Inclusive window as YYYY-YYYY (default: last 5 years)."
        ),
        top_k: str = typer.Option("10", "--top-k", help=f"Maximum hits (1-{MAX_TOP_K})."),
    ) -> None:
        options = from_context(ctx)
        started = time.perf_counter()
        top_k_value: int | None = None
        try:
            top_k_value = parse_int_option(top_k, field="top_k", minimum=1, maximum=MAX_TOP_K)
            result = _service(options).search_announcements(
                symbol, query_text, year_range=year_range, top_k=top_k_value
            )
        except FinanceCofferError as exc:
            log_query_audit(
                tool="search.announcements",
                trace_id=options.trace_id,
                symbol=symbol,
                year_range=year_range,
                top_k=top_k_value,
                elapsed_ms=_ms(started),
                outcome=exc.code.value,
            )
            emit_failure(options, exc)
        log_query_audit(
            tool="search.announcements",
            trace_id=options.trace_id,
            symbol=symbol,
            year_range=year_range,
            top_k=top_k_value,
            hits=result.data.get("count"),
            elapsed_ms=_ms(started),
            outcome="ok",
        )
        emit_success(options, result.data, meta=result.meta(options.trace_id))
