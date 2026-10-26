"""MCP server commands."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_group, add_placeholder


def register(app: typer.Typer) -> None:
    mcp = add_group(app, name="mcp", help_text="Model Context Protocol server.")
    add_placeholder(mcp, "stdio", path="mcp stdio", summary="Run the MCP server over stdio")
    add_placeholder(mcp, "http", path="mcp http", summary="Run the MCP server over HTTP")
    add_placeholder(mcp, "tools", path="mcp tools", summary="List the MCP tools")
