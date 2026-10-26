"""Database migration and partition commands."""

from __future__ import annotations

import typer

from financecoffer.cli.options import from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.core.errors import FinanceCofferError
from financecoffer.db import (
    PARTITIONED_TABLES,
    MigrationRunner,
    create_partitions,
    describe_dsn,
    list_partitions,
    parse_month,
    resolve_dsn,
)
from financecoffer.db.connection import connect
from financecoffer.db.migrations import render_sql


def register(app: typer.Typer) -> None:
    db = add_group(app, name="db", help_text="Database migrations and partitions.")

    @db.command("migrate", help="Apply pending migrations (forward-only, idempotent).")
    def migrate(
        ctx: typer.Context,
        revision: str = typer.Option(
            "head", "--revision", help="Target revision: head or a numeric version."
        ),
        sql: bool = typer.Option(
            False, "--sql", help="Print the ordered migration SQL without connecting."
        ),
        ahead: int = typer.Option(
            3, "--ahead", min=0, help="Pre-create ODS partitions for N months ahead."
        ),
    ) -> None:
        options = from_context(ctx)
        if sql:
            typer.echo(render_sql(), nl=False)
            return
        try:
            report = MigrationRunner(options.db_url, timeout=options.timeout).apply(
                revision=revision,
                ahead_months=ahead,
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, report.as_dict())

    @db.command("status", help="Show applied and pending migrations for the target database.")
    def status(ctx: typer.Context) -> None:
        options = from_context(ctx)
        try:
            report = MigrationRunner(options.db_url, timeout=options.timeout).status()
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, report.as_dict())

    partitions = add_group(db, name="partitions", help_text="Manage ODS time partitions.")

    @partitions.command("create", help="Create monthly ODS partitions.")
    def partitions_create(
        ctx: typer.Context,
        month: str | None = typer.Option(
            None, "--month", help="First month as YYYY-MM (default: current month)."
        ),
        ahead: int = typer.Option(3, "--ahead", min=0, help="How many extra months to add."),
    ) -> None:
        options = from_context(ctx)
        try:
            start = parse_month(month) if month else None
            rows = create_partitions(
                resolve_dsn(options.db_url),
                start_month=start,
                ahead=ahead,
                timeout=options.timeout,
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(
            options,
            {
                "table": PARTITIONED_TABLES[0],
                "partitions": [row["partition"] for row in rows],
                "created": [row["partition"] for row in rows if row["created"]],
            },
        )

    @partitions.command("list", help="List existing ODS partitions and their bounds.")
    def partitions_list(
        ctx: typer.Context,
        table: str = typer.Option(
            PARTITIONED_TABLES[0], "--table", help="Partitioned parent table."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            dsn = resolve_dsn(options.db_url)
            with connect(dsn, timeout=options.timeout) as conn:
                rows = list_partitions(conn, table)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(
            options,
            {
                "database": describe_dsn(dsn),
                "table": table,
                "count": len(rows),
                "partitions": rows,
            },
        )
