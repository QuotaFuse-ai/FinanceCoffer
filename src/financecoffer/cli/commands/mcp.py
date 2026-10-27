"""MCP server commands."""

from __future__ import annotations

from typing import Any

import typer

from financecoffer.cli.options import GlobalOptions, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_config
from financecoffer.core.config import Config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import DependencyError, FinanceCofferError
from financecoffer.mcp.tools import TOOL_SPECS
from financecoffer.query import QueryService


def _config(options: GlobalOptions) -> Config:
    try:
        return load_config(overrides={"db_url": options.db_url})
    except FinanceCofferError as exc:
        emit_failure(options, exc)


def _service(config: Config, options: GlobalOptions) -> QueryService:
    return QueryService(config=config, dsn=options.db_url, timeout=options.timeout)


def _server_module(options: GlobalOptions) -> Any:
    try:
        from financecoffer.mcp import server
    except ImportError:
        emit_failure(
            options,
            DependencyError(
                "the MCP extras are not installed",
                details={"hint": "install financecoffer[mcp]"},
            ),
        )
    return server


def _signature(spec: dict[str, Any]) -> str:
    params = ", ".join(spec["inputSchema"]["properties"])
    return f"{spec['name']}({params})"


def register(app: typer.Typer) -> None:
    mcp = add_group(app, name="mcp", help_text="Model Context Protocol server.")

    @mcp.command("tools", help="List the fixed MCP tool catalogue.")
    def tools(
        ctx: typer.Context,
        as_json: bool = typer.Option(
            False, "--json", help="Emit the catalogue inside the standard envelope."
        ),
    ) -> None:
        options = from_context(ctx)
        if as_json:
            emit_success(options, {"tools": list(TOOL_SPECS)}, meta=Meta(source="mcp"))
            return
        for spec in TOOL_SPECS:
            typer.echo(_signature(spec))
            typer.echo(f"  {spec['description']}")

    @mcp.command("stdio", help="Run the MCP server over stdio.")
    def stdio(ctx: typer.Context) -> None:
        options = from_context(ctx)
        config = _config(options)
        service = _service(config, options)
        _server_module(options).run_stdio(service)

    @mcp.command("http", help="Run the MCP server over Streamable HTTP.")
    def http(
        ctx: typer.Context,
        host: str = typer.Option(
            "127.0.0.1",
            "--host",
            help="Bind address; non-loopback requires --token and --allowed-hosts.",
        ),
        port: int = typer.Option(8100, "--port", min=1, max=65535, help="Listen port."),
        token: str | None = typer.Option(
            None, "--token", help="Bearer token; defaults to FC_MCP_TOKEN."
        ),
        allowed_hosts: str | None = typer.Option(
            None,
            "--allowed-hosts",
            help="Comma-separated Host allowlist; bare names accept any port.",
        ),
    ) -> None:
        options = from_context(ctx)
        config = _config(options)
        resolved_token = token or (
            config.mcp_token.get_secret_value() if config.mcp_token is not None else None
        )
        hosts = tuple(part.strip() for part in (allowed_hosts or "").split(",") if part.strip())
        service = _service(config, options)
        try:
            _server_module(options).run_http(
                service,
                host=host,
                port=port,
                token=resolved_token,
                allowed_hosts=hosts,
                log_level=config.log_level.lower(),
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
