"""Warehouse layer commands: DWD and DWS."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import typer
from sqlalchemy.exc import SQLAlchemyError

from financecoffer.cli.options import GlobalOptions, from_context, parse_int_option
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_cli_config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import (
    FinanceCofferError,
    InvalidArgumentError,
    PartialFailureError,
    StorageError,
)
from financecoffer.dwd import DwdService
from financecoffer.dws import DEFAULT_FISCAL_YEARS, MAX_FISCAL_YEARS, DwsService


def _dwd_service(options: GlobalOptions) -> DwdService:
    try:
        config = load_cli_config(db_url=options.db_url)
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    return DwdService(config=config, dsn=options.db_url, timeout=options.timeout)


def _run_dwd(options: GlobalOptions, action: Callable[[DwdService], Any]) -> Any:
    service = _dwd_service(options)
    try:
        return action(service)
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    except SQLAlchemyError:
        emit_failure(
            options,
            StorageError(
                "DWD database operation failed",
                details={"hint": "check FC_DB_URL and that migrations are applied"},
            ),
        )
    finally:
        service.close()


def _register_dwd(app: typer.Typer) -> None:
    dwd = add_group(app, name="dwd", help_text="Detail data layer.")

    @dwd.command("show", help="Show one financial snapshot; defaults to the latest version.")
    def show(
        ctx: typer.Context,
        symbol: str = typer.Option(..., "--symbol", help="A-share code or US ticker."),
        report_date: str | None = typer.Option(
            None, "--report-date", help="Report date, YYYY-MM-DD (default: latest stored)."
        ),
        report_type: str | None = typer.Option(
            None, "--report-type", help="Report type label, e.g. income or Q3 (default: latest)."
        ),
        version: str | None = typer.Option(
            None, "--version", help="Snapshot version (>= 1; default: MAX(version))."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            version_value = (
                None if version is None else parse_int_option(version, field="version", minimum=1)
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        data = _run_dwd(
            options,
            lambda service: service.show(
                symbol,
                report_date=report_date,
                report_type=report_type,
                version=version_value,
            ),
        )
        emit_success(options, data, meta=Meta(source="dwd"))

    @dwd.command("versions", help="List stored versions of one financial snapshot.")
    def versions(
        ctx: typer.Context,
        symbol: str = typer.Option(..., "--symbol", help="A-share code or US ticker."),
        report_date: str = typer.Option(..., "--report-date", help="Report date, YYYY-MM-DD."),
        report_type: str = typer.Option(
            ..., "--report-type", help="Report type label used when the snapshot was stored."
        ),
    ) -> None:
        options = from_context(ctx)
        data = _run_dwd(
            options,
            lambda service: service.versions(
                symbol,
                report_date=report_date,
                report_type=report_type,
            ),
        )
        emit_success(options, data, meta=Meta(source="dwd"))

    @dwd.command("backfill", help="Append missing DWD rows by replaying ODS payloads.")
    def backfill(
        ctx: typer.Context,
        from_ods: bool = typer.Option(
            False, "--from-ods", help="Replay the ODS layer (the only backfill source)."
        ),
        symbol: str | None = typer.Option(None, "--symbol", help="Limit to one symbol."),
        since: str | None = typer.Option(
            None, "--since", help="Earliest fetch_time (YYYY-MM-DD or RFC 3339)."
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Validate and count only; write nothing."
        ),
    ) -> None:
        options = from_context(ctx)
        payload = _run_dwd(
            options,
            lambda service: service.backfill(
                from_ods=from_ods,
                symbol=symbol,
                since=since,
                dry_run=dry_run,
            ),
        )
        if payload["invalid"] or payload["failed"]:
            emit_failure(
                options,
                PartialFailureError(
                    "some ODS payloads could not be written to DWD; see details.failures",
                    details=payload,
                ),
            )
        emit_success(options, payload, meta=Meta(source="dwd"))

    @dwd.command("verify", help="Check quality passthrough and money precision.")
    def verify(
        ctx: typer.Context,
        symbol: str | None = typer.Option(None, "--symbol", help="Limit to one symbol."),
        report_date: str | None = typer.Option(
            None, "--report-date", help="Limit to one report date, YYYY-MM-DD."
        ),
    ) -> None:
        options = from_context(ctx)
        payload = _run_dwd(
            options,
            lambda service: service.verify(symbol=symbol, report_date=report_date),
        )
        if payload["failed"]:
            emit_failure(
                options,
                PartialFailureError(
                    "some DWD rows failed verification; see details.failures",
                    details=payload,
                ),
            )
        emit_success(options, payload, meta=Meta(source="dwd"))


def _service(options: GlobalOptions) -> DwsService:
    try:
        config = load_cli_config(db_url=options.db_url)
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
        years: str = typer.Option(
            str(DEFAULT_FISCAL_YEARS),
            "--years",
            help=f"Fiscal-year window, newest year first (1-{MAX_FISCAL_YEARS}).",
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            years_value = parse_int_option(
                years, field="years", minimum=1, maximum=MAX_FISCAL_YEARS
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
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
            years=years_value,
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
        years: str = typer.Option(
            str(DEFAULT_FISCAL_YEARS),
            "--years",
            help=f"Fiscal-year window, newest year first (1-{MAX_FISCAL_YEARS}).",
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            years_value = parse_int_option(
                years, field="years", minimum=1, maximum=MAX_FISCAL_YEARS
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        _run_build(
            options,
            symbols=[],
            build_all=True,
            as_of=as_of,
            years=years_value,
        )


def register(app: typer.Typer) -> None:
    _register_dwd(app)
    _register_dws(app)
