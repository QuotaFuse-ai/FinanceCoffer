"""Manual ingestion and backfill commands."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_group, add_placeholder


def register(app: typer.Typer) -> None:
    ingest = add_group(app, name="ingest", help_text="Manual ingestion entry points.")
    add_placeholder(ingest, "event", path="ingest event", summary="Validate and ingest one event")
    add_placeholder(
        ingest, "backfill", path="ingest backfill", summary="One-off historical backfill"
    )
    add_placeholder(ingest, "webhook", path="ingest webhook", summary="Run the webhook receiver")
