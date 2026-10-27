"""Bootstrap commands: init, doctor and the config group."""

from __future__ import annotations

import json
from typing import Any

import typer

from financecoffer.cli.options import from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group, add_placeholder
from financecoffer.config.loader import load_config
from financecoffer.core.config import Config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import FinanceCofferError


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _render_text(config: Config) -> str:
    """Render the redacted config as ``key: value`` lines (never plaintext secrets)."""

    shown = config.redacted_dict()
    return "\n".join(f"{key}: {_scalar(value)}" for key, value in shown.items())


def register(app: typer.Typer) -> None:
    add_placeholder(app, "init", path="init", summary="Create schemas, extensions and base tables")
    add_placeholder(
        app, "doctor", path="doctor", summary="Probe PostgreSQL, MinIO, Redis and embedding"
    )

    config = add_group(app, name="config", help_text="Inspect and validate configuration.")

    @config.command("show", help="Print effective configuration; secrets are always redacted.")
    def show(
        ctx: typer.Context,
        as_json: bool = typer.Option(
            False, "--json", help="Emit the redacted configuration inside the standard envelope."
        ),
        redact: bool = typer.Option(
            False,
            "--redact",
            help="Accepted for spec compatibility; output is always redacted.",
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            resolved = load_config(overrides={"db_url": options.db_url})
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        _ = redact  # redaction is unconditional; the flag only documents intent
        if as_json:
            emit_success(options, resolved.redacted_dict(), meta=Meta(source="config"))
            return
        typer.echo(_render_text(resolved))

    @config.command("check", help="Validate configuration and report missing credentials.")
    def check(ctx: typer.Context) -> None:
        options = from_context(ctx)
        try:
            resolved = load_config(overrides={"db_url": options.db_url})
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        warnings: list[str] = []
        if resolved.db_url is None:
            warnings.append("FC_DB_URL is not set")
        if resolved.minio_access_key is None or resolved.minio_secret_key is None:
            warnings.append("MinIO credentials are not fully configured")
        if resolved.embedding_provider == "openai" and resolved.embedding_api_key is None:
            warnings.append("FC_EMBEDDING_API_KEY is required for the openai provider")

        data = {
            "valid": True,
            "db_url_configured": resolved.db_url is not None,
            "minio_configured": (
                resolved.minio_access_key is not None and resolved.minio_secret_key is not None
            ),
            "embedding_provider": resolved.embedding_provider,
            "warnings": warnings,
        }
        emit_success(options, data, meta=Meta(source="config"))
