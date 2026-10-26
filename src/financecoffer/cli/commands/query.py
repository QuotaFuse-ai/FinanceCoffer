"""Query commands (CLI equivalents of the MCP tools)."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_group, add_placeholder


def register(app: typer.Typer) -> None:
    query = add_group(app, name="query", help_text="Structured and risk queries.")
    add_placeholder(
        query, "structured", path="query structured", summary="Query DWS structured metrics"
    )
    add_placeholder(query, "risk", path="query risk", summary="Query historical risk events")

    search = add_group(app, name="search", help_text="Semantic search.")
    add_placeholder(
        search,
        "announcements",
        path="search announcements",
        summary="Search announcements semantically",
    )
