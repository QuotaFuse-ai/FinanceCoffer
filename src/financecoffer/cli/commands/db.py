"""Database migration and partition commands."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_group, add_placeholder


def register(app: typer.Typer) -> None:
    db = add_group(app, name="db", help_text="Database migrations and partitions.")
    add_placeholder(db, "migrate", path="db migrate", summary="Apply Alembic migrations")
    add_placeholder(db, "status", path="db status", summary="Show migration status")

    partitions = add_group(db, name="partitions", help_text="Manage ODS time partitions.")
    add_placeholder(
        partitions, "create", path="db partitions create", summary="Create monthly partitions"
    )
    add_placeholder(
        partitions, "list", path="db partitions list", summary="List existing partitions"
    )
