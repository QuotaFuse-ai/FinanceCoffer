"""Service commands: serve api/admin and start-all."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_group, add_placeholder


def register(app: typer.Typer) -> None:
    serve = add_group(app, name="serve", help_text="Run FinanceCoffer services.")
    add_placeholder(serve, "api", path="serve api", summary="Run the REST API server")
    add_placeholder(serve, "admin", path="serve admin", summary="Run the admin UI server")
    add_placeholder(
        app, "start-all", path="start-all", summary="Run API and the ETL consumer together"
    )
