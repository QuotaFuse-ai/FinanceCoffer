"""Warehouse layer commands: DWD and DWS."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_group, add_placeholder


def _register_dwd(app: typer.Typer) -> None:
    dwd = add_group(app, name="dwd", help_text="Detail data layer.")
    add_placeholder(dwd, "show", path="dwd show", summary="Show a financial snapshot")
    add_placeholder(dwd, "versions", path="dwd versions", summary="List snapshot versions")
    add_placeholder(dwd, "backfill", path="dwd backfill", summary="Backfill DWD from ODS")
    add_placeholder(dwd, "verify", path="dwd verify", summary="Verify quality passthrough")


def _register_dws(app: typer.Typer) -> None:
    dws = add_group(app, name="dws", help_text="Wide-table layer.")
    add_placeholder(dws, "build", path="dws build", summary="Build agent analysis wide tables")
    add_placeholder(dws, "show", path="dws show", summary="Show a wide-table row")
    add_placeholder(dws, "rebuild", path="dws rebuild", summary="Rebuild wide tables")


def register(app: typer.Typer) -> None:
    _register_dwd(app)
    _register_dws(app)
