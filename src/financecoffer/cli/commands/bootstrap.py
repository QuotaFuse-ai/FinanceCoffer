"""Bootstrap commands: init, doctor and the config group."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_group, add_placeholder


def register(app: typer.Typer) -> None:
    add_placeholder(app, "init", path="init", summary="Create schemas, extensions and base tables")
    add_placeholder(
        app, "doctor", path="doctor", summary="Probe PostgreSQL, MinIO, Redis and embedding"
    )

    config = add_group(app, name="config", help_text="Inspect and validate configuration.")
    add_placeholder(config, "show", path="config show", summary="Print effective configuration")
    add_placeholder(config, "check", path="config check", summary="Validate configuration")
