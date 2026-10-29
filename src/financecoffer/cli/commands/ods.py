"""ODS commands: inspect the raw layer and reset event-log state for replay.

``show --raw`` is the one command that bypasses the JSON envelope: it writes
the stored upstream document verbatim (no trailing newline) so callers can
compare it byte for byte with what the publisher sent.
"""

from __future__ import annotations

import sys

import typer

from financecoffer.cli.options import GlobalOptions, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_cli_config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import FinanceCofferError
from financecoffer.events.models import EventDataType
from financecoffer.ods import (
    DEFAULT_LIMIT,
    ETL_STATUSES,
    GROUP_BYS,
    MAX_LIMIT,
    OdsService,
)


def _service(options: GlobalOptions) -> OdsService:
    try:
        config = load_cli_config(db_url=options.db_url)
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    return OdsService(config=config, dsn=options.db_url)


def _meta() -> Meta:
    return Meta(source="ods")


def _write_verbatim(payload: str) -> None:
    """Write the stored document without an envelope or newline translation.

    Going through ``sys.stdout`` in text mode would turn ``\\n`` into ``\\r\\n``
    on Windows, breaking byte-for-byte comparison with the upstream file.
    """

    console = sys.stdout
    buffer = getattr(console, "buffer", None)
    if buffer is None:
        console.write(payload)
        return
    console.flush()
    buffer.write(payload.encode("utf-8"))
    buffer.flush()


def register(app: typer.Typer) -> None:
    ods = add_group(app, name="ods", help_text="Raw data layer.")

    @ods.command("list", help="List raw records, newest first.")
    def list_records(
        ctx: typer.Context,
        data_type: str | None = typer.Option(
            None,
            "--data-type",
            help="Filter by type: " + "|".join(item.value for item in EventDataType) + ".",
        ),
        symbol: str | None = typer.Option(None, "--symbol", help="Filter by symbol."),
        since: str | None = typer.Option(
            None, "--since", help="Earliest fetch_time (YYYY-MM-DD or RFC 3339)."
        ),
        until: str | None = typer.Option(
            None, "--until", help="Latest fetch_time; a date includes the whole day."
        ),
        etl_status: str | None = typer.Option(
            None,
            "--etl-status",
            help="Filter by etl_status: " + "|".join(ETL_STATUSES) + ".",
        ),
        limit: int = typer.Option(
            DEFAULT_LIMIT, "--limit", min=1, max=MAX_LIMIT, help="Maximum number of records."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            data = _service(options).listing(
                data_type=data_type,
                symbol=symbol,
                since=since,
                until=until,
                etl_status=etl_status,
                limit=limit,
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=_meta())

    @ods.command("show", help="Show one raw record; --raw prints the original document.")
    def show(
        ctx: typer.Context,
        record_id: int = typer.Argument(..., metavar="ID", help="ODS record id."),
        raw: bool = typer.Option(
            False, "--raw", help="Print the stored upstream payload verbatim."
        ),
    ) -> None:
        options = from_context(ctx)
        service = _service(options)
        if raw:
            try:
                payload = service.raw_text(record_id)
            except FinanceCofferError as exc:
                emit_failure(options, exc)
            _write_verbatim(payload)
            return
        try:
            detail = service.show(record_id)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, detail, meta=_meta())

    @ods.command("stats", help="Show raw-layer counts by column.")
    def stats(
        ctx: typer.Context,
        group_by: str = typer.Option(
            GROUP_BYS[0],
            "--group-by",
            help="Column to group by: " + "|".join(GROUP_BYS) + ".",
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            data = _service(options).stats(group_by=group_by)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=_meta())

    @ods.command(
        "replay",
        help=(
            "Reset the event log of stored ODS records (no re-publish to the stream;"
            " re-publish upstream, then consume with --force)."
        ),
    )
    def replay(
        ctx: typer.Context,
        record_id: int | None = typer.Argument(None, metavar="ID", help="ODS record id."),
        all_failed: bool = typer.Option(
            False,
            "--all-failed",
            help="Reset every record whose event failed or was dead-lettered.",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Only print the manifest; change nothing."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            data = _service(options).replay(
                record_ids=(record_id,) if record_id is not None else (),
                all_failed=all_failed,
                dry_run=dry_run,
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=_meta())
