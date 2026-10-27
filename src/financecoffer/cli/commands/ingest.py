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
from financecoffer.dwd import write_event
from financecoffer.events import register_event, validate_event
from financecoffer.ods import land_event


def _read_payload(path: str) -> tuple[Mapping[str, Any], str]:
    """Read one event file as UTF-8 JSON; failures name the offending ``file``.

    The file is read as bytes and decoded without newline translation, so CRLF
    documents keep their exact bytes for ``ods show <id> --raw``. The decoded
    text is returned alongside the parsed mapping for ODS to store verbatim.
    """

    try:
        text = Path(path).read_bytes().decode("utf-8")
    except OSError as exc:
        raise InvalidArgumentError(
            f"cannot read event file: {path}",
            details={"errors": [{"field": "file", "reason": exc.strerror or str(exc)}]},
        ) from exc
    except UnicodeDecodeError as exc:
        raise InvalidArgumentError(
            "event file is not valid UTF-8",
            details={
                "errors": [
                    {"field": "file", "reason": f"invalid UTF-8 at byte {exc.start}"}
                ]
            },
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
    return payload, text


def register(app: typer.Typer) -> None:
    ingest = add_group(app, name="ingest", help_text="Manual ingestion entry points.")

    @ingest.command(
        "event",
        help="Validate one upstream event, register it once and append it to ODS.",
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
            payload, original_text = _read_payload(file)
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
                        landing = None
                        fact = None
                        if not registration.duplicate:
                            landing = land_event(
                                session, validated, raw_text=original_text
                            )
                            fact = write_event(
                                session,
                                validated,
                                compliance_enabled=config.compliance_enabled,
                            )
                except SQLAlchemyError as exc:
                    raise StorageError(
                        "event registration failed",
                        details={"hint": "check FC_DB_URL and that migrations are applied"},
                    ) from exc
                finally:
                    database.dispose()
                data.update(registration.as_dict())
                data["ods"] = landing.as_dict() if landing is not None else None
                data["dwd"] = fact.as_dict() if fact is not None else None
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=Meta(source="etl"))

    add_placeholder(
        ingest, "backfill", path="ingest backfill", summary="One-off historical backfill"
    )
    add_placeholder(ingest, "webhook", path="ingest webhook", summary="Run the webhook receiver")
