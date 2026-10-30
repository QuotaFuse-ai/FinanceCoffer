"""Cold archive commands: run, status, restore and verify (T-012)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import typer
from sqlalchemy.exc import SQLAlchemyError

from financecoffer.archive import ArchiveService
from financecoffer.archive.layout import TABLES
from financecoffer.archive.service import MAX_SAMPLE
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

TABLE_NAMES = ", ".join(spec.name for spec in TABLES)
SYMBOL_HELP = "Only archive these symbols (repeatable)."
TABLE_HELP = f"Only archive these tables (repeatable): {TABLE_NAMES}."


def _archive_service(options: GlobalOptions) -> ArchiveService:
    try:
        config = load_cli_config(db_url=options.db_url)
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    return ArchiveService(config=config, dsn=options.db_url, timeout=options.timeout)


def _run_archive(options: GlobalOptions, action: Callable[[ArchiveService], Any]) -> Any:
    service = _archive_service(options)
    try:
        return action(service)
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    except SQLAlchemyError:
        emit_failure(
            options,
            StorageError(
                "archive database operation failed",
                details={"hint": "check FC_DB_URL and that migrations are applied"},
            ),
        )
    finally:
        service.close()


def _require_yes(
    options: GlobalOptions,
    count: Callable[[ArchiveService], dict[str, Any]],
    *,
    action: str,
    field: str,
    noun: str,
) -> None:
    """Refuse a write command that carries no ``--yes`` (exit 2, no changes).

    The blast radius is counted read-only so the message can say what a
    confirmed run would touch. A warehouse that cannot be counted still gets
    the same refusal rather than a storage error: the missing flag alone is
    enough to stop.
    """

    service = _archive_service(options)
    try:
        scope: dict[str, Any] | None = count(service)
    except InvalidArgumentError as exc:
        emit_failure(options, exc)
    except FinanceCofferError:
        scope = None
    except Exception:
        scope = None
    finally:
        service.close()

    affected = None if scope is None else scope.get(field)
    message = f"{action} changes PostgreSQL and MinIO; re-run with --yes to confirm"
    if affected is not None:
        message += f" ({affected} {noun} in scope)"
    emit_failure(
        options,
        InvalidArgumentError(
            message,
            details={
                "field": "yes",
                "hint": "--yes",
                "candidates": affected,
                "scope": scope,
                "dry_run_hint": "--dry-run previews the same rows without writing",
            },
        ),
    )


def register(app: typer.Typer) -> None:
    archive = add_group(app, name="archive", help_text="Cold data archive in MinIO.")

    @archive.command("run", help="Archive cold and oversized rows; uploads before marking.")
    def run(
        ctx: typer.Context,
        older_than_years: str | None = typer.Option(
            None,
            "--older-than-years",
            help="Cold age in years, zero or greater (default: FC_COLD_AFTER_YEARS).",
        ),
        min_bytes: str = typer.Option(
            "8192",
            "--min-bytes",
            help="Oversized body threshold in bytes, at least 1 (default: 8192).",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Report candidate counts and bytes; write nothing."
        ),
        limit: str | None = typer.Option(
            None, "--limit", help="Process at most N candidate rows per run (>= 1)."
        ),
        symbol: Annotated[list[str] | None, typer.Option("--symbol", help=SYMBOL_HELP)] = None,
        table: Annotated[list[str] | None, typer.Option("--table", help=TABLE_HELP)] = None,
        yes: bool = typer.Option(
            False, "--yes", help="Confirm the run; a non-dry-run without it is refused."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            years_value = (
                None
                if older_than_years is None
                else parse_int_option(older_than_years, field="older_than_years", minimum=0)
            )
            min_bytes_value = parse_int_option(min_bytes, field="min_bytes", minimum=1)
            limit_value = (
                None
                if limit is None
                else parse_int_option(limit, field="limit", minimum=1)
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        if not dry_run and not yes:
            _require_yes(
                options,
                lambda service: service.count_candidates(
                    older_than_years=years_value,
                    min_bytes=min_bytes_value,
                    limit=limit_value,
                    symbols=symbol,
                    tables=table,
                ),
                action="archive run",
                field="candidates",
                noun="candidate rows",
            )
        payload = _run_archive(
            options,
            lambda service: service.run(
                older_than_years=years_value,
                min_bytes=min_bytes_value,
                dry_run=dry_run,
                limit=limit_value,
                symbols=symbol,
                tables=table,
            ),
        )
        if payload["failed"]:
            emit_failure(
                options,
                PartialFailureError(
                    "some rows could not be archived; see details.failures",
                    details=payload,
                ),
            )
        emit_success(options, payload, meta=Meta(source="archive"))

    @archive.command("status", help="Show hot/cold counts and archived bytes.")
    def status(
        ctx: typer.Context,
        symbol: str | None = typer.Option(None, "--symbol", help="Limit to one symbol."),
    ) -> None:
        options = from_context(ctx)
        payload = _run_archive(options, lambda service: service.status(symbol=symbol))
        emit_success(options, payload, meta=Meta(source="archive"))

    @archive.command("restore", help="Read archived rows back and verify their hashes.")
    def restore(
        ctx: typer.Context,
        symbol: str = typer.Option(..., "--symbol", help="A-share code or US ticker."),
        since: str | None = typer.Option(
            None, "--since", help="Earliest data date (YYYY-MM-DD or RFC 3339)."
        ),
        until: str | None = typer.Option(
            None, "--until", help="Latest data date (YYYY-MM-DD or RFC 3339)."
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Verify objects and count restores; write no bodies back.",
        ),
        yes: bool = typer.Option(
            False, "--yes", help="Confirm the write-back; a non-dry-run without it is refused."
        ),
    ) -> None:
        options = from_context(ctx)
        if not dry_run and not yes:
            _require_yes(
                options,
                lambda service: service.count_restore(symbol, since=since, until=until),
                action="archive restore",
                field="writes",
                noun="announcement rows to write back",
            )
        payload = _run_archive(
            options,
            lambda service: service.restore(
                symbol, since=since, until=until, dry_run=dry_run
            ),
        )
        if payload["failed"]:
            emit_failure(
                options,
                PartialFailureError(
                    "some archived rows failed verification; see details.failures",
                    details=payload,
                ),
            )
        emit_success(options, payload, meta=Meta(source="archive"))

    @archive.command(
        "verify", help="Download a random sample of archived objects and recheck hashes."
    )
    def verify(
        ctx: typer.Context,
        sample: str = typer.Option(
            "100",
            "--sample",
            help=f"Maximum number of archived objects to check (1-{MAX_SAMPLE}).",
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            sample_value = parse_int_option(
                sample, field="sample", minimum=1, maximum=MAX_SAMPLE
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        payload = _run_archive(options, lambda service: service.verify(sample=sample_value))
        if payload["failed"]:
            emit_failure(
                options,
                PartialFailureError(
                    "some archived objects failed verification; see details.failures",
                    details=payload,
                ),
            )
        emit_success(options, payload, meta=Meta(source="archive"))
