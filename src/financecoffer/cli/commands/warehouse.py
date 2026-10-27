"""Warehouse layer commands: DWD and DWS."""

from __future__ import annotations

from typing import Annotated

import typer

from financecoffer.cli.options import GlobalOptions, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group, add_placeholder
from financecoffer.config.loader import load_config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import (
    FinanceCofferError,
    InvalidArgumentError,
    PartialFailureError,
)
from financecoffer.dws import DEFAULT_FISCAL_YEARS, MAX_FISCAL_YEARS, DwsService


def _register_dwd(app: typer.Typer) -> None:
    dwd = add_group(app, name="dwd", help_text="Detail data layer.")
    add_placeholder(dwd, "show", path="dwd show", summary="Show a financial snapshot")
    add_placeholder(dwd, "versions", path="dwd versions", summary="List snapshot versions")
    add_placeholder(dwd, "backfill", path="dwd backfill", summary="Backfill DWD from ODS")
    add_placeholder(dwd, "verify", path="dwd verify", summary="Verify quality passthrough")


def _service(options: GlobalOptions) -> DwsService:
    try:
        config = load_config(overrides={"db_url": options.db_url})
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    return DwsService(config=config, dsn=options.db_url, timeout=options.timeout)


def _run_build(
    options: GlobalOptions,
    *,
    symbols: list[str],
    build_all: bool,
    as_of: str | None,
    years: int,
) -> None:
    service = _service(options)
    try:
        report = service.build(
            symbols,
            build_all=build_all,
            as_of=as_of,
            fiscal_years=years,
        )
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    finally:
        service.close()

    payload = report.payload()
    if report.failed:
        emit_failure(
            options,
            PartialFailureError(
                "some symbols failed to build; see details.failed",
                details=payload,
            ),
        )
    emit_success(options, payload, meta=Meta(source="dws"))


def _register_dws(app: typer.Typer) -> None:
    dws = add_group(app, name="dws", help_text="Wide-table layer.")

    @dws.command("build", help="Build the agent analysis wide table from DWD.")
    def build(
        ctx: typer.Context,
        symbol: Annotated[
            list[str] | None,
            typer.Option(
                "--symbol",
                help="Symbol to build; repeatable. Omit with --all to build every DWD symbol.",
            ),
        ] = None,
        all_symbols: bool = typer.Option(
            False, "--all", help="Build every symbol present in dwd.stock_financial_snapshot."
        ),
        as_of: str | None = typer.Option(
            None, "--as-of", help="Data cutoff day, YYYY-MM-DD (default: today, UTC)."
        ),
        years: int = typer.Option(
            DEFAULT_FISCAL_YEARS,
            "--years",
            min=1,
            max=MAX_FISCAL_YEARS,
            help="Fiscal-year window, newest year first.",
        ),
    ) -> None:
        options = from_context(ctx)
        if symbol and all_symbols:
            emit_failure(
                options,
                InvalidArgumentError(
                    "--all and --symbol are mutually exclusive",
                    details={"field": "symbol"},
                ),
            )
        if not symbol and not all_symbols:
            emit_failure(
                options,
                InvalidArgumentError(
                    "either --symbol or --all is required",
                    details={"field": "symbol"},
                ),
            )
        _run_build(
            options,
            symbols=symbol or [],
            build_all=all_symbols,
            as_of=as_of,
            years=years,
        )

    @dws.command("show", help="Show one built wide-table row.")
    def show(
        ctx: typer.Context,
        symbol: str = typer.Argument(..., help="A-share code or US ticker."),
    ) -> None:
        options = from_context(ctx)
        service = _service(options)
        try:
            data = service.show(symbol)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        finally:
            service.close()
        emit_success(options, data, meta=Meta(source="dws"))

    @dws.command("rebuild", help="Rebuild every wide-table row (equivalent to `build --all`).")
    def rebuild(
        ctx: typer.Context,
        as_of: str | None = typer.Option(
            None, "--as-of", help="Data cutoff day, YYYY-MM-DD (default: today, UTC)."
        ),
        years: int = typer.Option(
            DEFAULT_FISCAL_YEARS,
            "--years",
            min=1,
            max=MAX_FISCAL_YEARS,
            help="Fiscal-year window, newest year first.",
        ),
    ) -> None:
        options = from_context(ctx)
        _run_build(
            options,
            symbols=[],
            build_all=True,
            as_of=as_of,
            years=years,
        )


def register(app: typer.Typer) -> None:
    _register_dwd(app)
    _register_dws(app)
