"""Query commands (CLI equivalents of the MCP tools)."""

from __future__ import annotations

import typer

from financecoffer.cli.options import GlobalOptions, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_config
from financecoffer.core.errors import FinanceCofferError
from financecoffer.query import MAX_RISK_LIMIT, MAX_TOP_K, STRUCTURED_METRICS, QueryService


def _service(options: GlobalOptions) -> QueryService:
    try:
        config = load_config(overrides={"db_url": options.db_url})
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    return QueryService(config=config, dsn=options.db_url, timeout=options.timeout)


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
        try:
            result = _service(options).structured(symbol, metric=metric, year_range=year_range)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, result.data, meta=result.meta(options.trace_id))

    @query.command("risk", help="Query historical risk events.")
    def risk(
        ctx: typer.Context,
        symbol: str = typer.Option(..., "--symbol", help="A-share code or US ticker."),
        since: str | None = typer.Option(None, "--since", help="Earliest occur_date, YYYY-MM-DD."),
        limit: int = typer.Option(
            50, "--limit", min=1, max=MAX_RISK_LIMIT, help="Maximum number of events."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            result = _service(options).risk(symbol, since=since, limit=limit)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
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
        top_k: int = typer.Option(10, "--top-k", min=1, max=MAX_TOP_K, help="Maximum hits."),
    ) -> None:
        options = from_context(ctx)
        try:
            result = _service(options).search_announcements(
                symbol, query_text, year_range=year_range, top_k=top_k
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, result.data, meta=result.meta(options.trace_id))
