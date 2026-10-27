"""Manual ingestion and backfill commands."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.exc import SQLAlchemyError

from financecoffer.cli.options import from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group, add_placeholder
from financecoffer.config.loader import load_config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import FinanceCofferError, InvalidArgumentError, StorageError
from financecoffer.db.session import Database
from financecoffer.events import register_event, validate_event


def _read_payload(path: str) -> Mapping[str, Any]:
    """Read one event file as UTF-8 JSON; failures name the offending ``file``."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidArgumentError(
            f"cannot read event file: {path}",
            details={"errors": [{"field": "file", "reason": exc.strerror or str(exc)}]},
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidArgumentError(
            "event file is not valid JSON",
            details={
                "errors": [
                    {"field": "file", "reason": f"line {exc.lineno} column {exc.colno}: {exc.msg}"}
                ]
            },
        ) from exc
    if not isinstance(payload, Mapping):
        raise InvalidArgumentError(
            "event file must contain a JSON object",
            details={"errors": [{"field": "file", "reason": f"got {type(payload).__name__}"}]},
        )
    return payload


def register(app: typer.Typer) -> None:
    ingest = add_group(app, name="ingest", help_text="Manual ingestion entry points.")

    @ingest.command(
        "event",
        help="Validate one upstream event and register it in ops.etl_event_log.",
    )
    def event(
        ctx: typer.Context,
        file: str = typer.Option(..., "--file", help="Path to the event JSON payload."),
        validate_only: bool = typer.Option(
            False,
            "--validate-only",
            help="Validate against the event contract without touching the database.",
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            payload = _read_payload(file)
            config = load_config(overrides={"db_url": options.db_url})
            validated = validate_event(payload, compliance_enabled=config.compliance_enabled)
            data = validated.summary()
            if validate_only:
                data["valid"] = True
            else:
                database = Database(config.db_url)
                try:
                    with database.session_scope() as session:
                        registration = register_event(session, validated)
                except SQLAlchemyError as exc:
                    raise StorageError(
                        "event log registration failed",
                        details={"hint": "check FC_DB_URL and that migrations are applied"},
                    ) from exc
                finally:
                    database.dispose()
                data.update(registration.as_dict())
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=Meta(source="etl"))

    add_placeholder(
        ingest, "backfill", path="ingest backfill", summary="One-off historical backfill"
    )
    add_placeholder(ingest, "webhook", path="ingest webhook", summary="Run the webhook receiver")
