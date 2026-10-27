"""Cold archive commands: run, status, restore and verify (T-012)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.exc import SQLAlchemyError

from financecoffer.archive import ArchiveService
from financecoffer.cli.options import GlobalOptions, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import FinanceCofferError, PartialFailureError, StorageError

#: Read the repository ``.env`` when the command runs from the repo root; CI and
#: unit tests run without one and fall back to the process environment.
_ENV_FILE = Path(".env")


def _archive_service(options: GlobalOptions) -> ArchiveService:
    try:
        config = load_config(
            env_file=_ENV_FILE if _ENV_FILE.is_file() else None,
            overrides={"db_url": options.db_url},
        )
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


def register(app: typer.Typer) -> None:
    archive = add_group(app, name="archive", help_text="Cold data archive in MinIO.")

    @archive.command("run", help="Archive cold and oversized rows; uploads before marking.")
    def run(
        ctx: typer.Context,
        older_than_years: int | None = typer.Option(
            None,
            "--older-than-years",
            min=0,
            help="Cold age in years (default: FC_COLD_AFTER_YEARS).",
        ),
        min_bytes: int = typer.Option(
            8192,
            "--min-bytes",
            min=1,
            help="Oversized body threshold in bytes, regardless of age.",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Report candidate counts and bytes; write nothing."
        ),
        limit: int | None = typer.Option(
            None, "--limit", min=1, help="Process at most N candidate rows per run."
        ),
    ) -> None:
        options = from_context(ctx)
        payload = _run_archive(
            options,
            lambda service: service.run(
                older_than_years=older_than_years,
                min_bytes=min_bytes,
                dry_run=dry_run,
                limit=limit,
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
    ) -> None:
        options = from_context(ctx)
        payload = _run_archive(
            options,
            lambda service: service.restore(symbol, since=since, until=until),
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
        sample: int = typer.Option(
            100, "--sample", min=1, help="Maximum number of archived objects to check."
        ),
    ) -> None:
        options = from_context(ctx)
        payload = _run_archive(options, lambda service: service.verify(sample=sample))
        if payload["failed"]:
            emit_failure(
                options,
                PartialFailureError(
                    "some archived objects failed verification; see details.failures",
                    details=payload,
                ),
            )
        emit_success(options, payload, meta=Meta(source="archive"))
