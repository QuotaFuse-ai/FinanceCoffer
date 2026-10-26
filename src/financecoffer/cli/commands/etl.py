"""ETL consumer commands."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_group, add_placeholder


def register(app: typer.Typer) -> None:
    etl = add_group(app, name="etl", help_text="Event ingestion pipeline.")
    add_placeholder(etl, "consume", path="etl consume", summary="Consume upstream events")
    add_placeholder(etl, "status", path="etl status", summary="Show consumer status")
    add_placeholder(etl, "pause", path="etl pause", summary="Pause the consumer")
    add_placeholder(etl, "resume", path="etl resume", summary="Resume the consumer")
    add_placeholder(etl, "replay", path="etl replay", summary="Replay events from ODS")

    dlq = add_group(etl, name="dlq", help_text="Dead-letter queue operations.")
    add_placeholder(dlq, "list", path="etl dlq list", summary="List dead-letter events")
    add_placeholder(dlq, "retry", path="etl dlq retry", summary="Retry dead-letter events")
