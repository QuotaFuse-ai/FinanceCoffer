"""Manual ingestion, one-off backfill and the webhook receiver.

``ingest event`` and ``ingest webhook`` land a single payload through the shared
pipeline (validate -> ``ops.etl_event_log`` -> ODS -> DWD); a ``heartbeat``
refreshes ``ops.source_heartbeat`` instead, because it is liveness state rather
than data. ``ingest backfill`` walks a directory of event documents, keeps only
the ones inside the requested fetch-time window and replays them through the
same pipeline with ``source=\"backfill\"`` in ODS; heartbeats are excluded (a
replayed round would move the heartbeat backwards) and re-running the rest is
safe because the event log deduplicates by ``event_id``.
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.exc import SQLAlchemyError

from financecoffer.cli.options import GlobalOptions, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_config
from financecoffer.core.config import Config
from financecoffer.core.envelope import Envelope, Meta
from financecoffer.core.errors import (
    AuthError,
    ConfigError,
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
    NotFoundError,
    PartialFailureError,
    StorageError,
)
from financecoffer.core.redaction import redact_text
from financecoffer.db.session import Database
from financecoffer.dwd import write_event
from financecoffer.etl.processor import record_heartbeat
from financecoffer.events import ValidatedEvent, register_event, validate_event
from financecoffer.events.models import EventDataType
from financecoffer.ods import (
    fetch_time_for,
    land_event,
    normalize_data_type,
    parse_instant,
    resolve_source,
)
from financecoffer.query import normalize_symbol

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_BACKFILL_SOURCES: tuple[str, ...] = ("tickhub-cn", "tickhub-us")
_WEBHOOK_TOKEN_ENV = "FC_WEBHOOK_TOKEN"
BACKFILL_MAX_EVENTS = 10_000

_HTTP_STATUS: dict[str, int] = {
    "E_INVALID_ARGUMENT": 400,
    "E_CONFIG": 503,
    "E_STORAGE": 500,
    "E_DEPENDENCY": 503,
    "E_AUTH": 401,
    "E_NOT_FOUND": 404,
    "E_NOT_IMPLEMENTED": 501,
    "E_RATE_LIMITED": 429,
    "E_PARTIAL_FAILURE": 500,
}


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
            details={"errors": [{"field": "file", "reason": f"invalid UTF-8 at byte {exc.start}"}]},
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


def _persist(
    config: Config,
    validated: ValidatedEvent,
    raw_text: str,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """Register and append one validated event; duplicate events skip ODS/DWD.

    A heartbeat is registered the same way but refreshes ``ops.source_heartbeat``
    instead of landing anywhere: contract 3.8 keeps liveness state out of
    ODS/DWD/DWS, so its ``source`` is always the publishing hub.
    """

    data = validated.summary()
    database = Database(config.db_url)
    try:
        with database.session_scope() as session:
            registration = register_event(session, validated)
            landing = None
            fact = None
            heartbeat = None
            if not registration.duplicate:
                if validated.data_type is EventDataType.HEARTBEAT:
                    heartbeat = record_heartbeat(session, validated)
                else:
                    landing = land_event(session, validated, raw_text=raw_text, source=source)
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
    data["heartbeat"] = heartbeat.as_dict() if heartbeat is not None else None
    return data


def _ingest_event(
    config: Config,
    payload: Mapping[str, Any],
    raw_text: str,
    *,
    source: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    validated = validate_event(payload, compliance_enabled=config.compliance_enabled)
    if not write:
        return validated.summary()
    return _persist(config, validated, raw_text, source=source)


# -- backfill ---------------------------------------------------------------


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _window(from_value: str, to_value: str) -> tuple[datetime, datetime]:
    start = parse_instant(from_value, field_name="from")
    end = parse_instant(to_value, field_name="to", end_of_day=True)
    if start is None or end is None:
        raise InvalidArgumentError(
            "--from and --to are required",
            details={"field": "from"},
        )
    if start >= end:
        raise InvalidArgumentError(
            "--from must be earlier than --to",
            details={"field": "from", "from": _rfc3339(start), "to": _rfc3339(end)},
        )
    return start, end


def _backfill_data_type(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized == "all":
        return None
    return normalize_data_type(normalized)


def _backfill_symbols(value: str | None) -> set[str] | None:
    if value is None:
        return None
    items = [part.strip() for part in value.split(",") if part.strip()]
    if not items:
        raise InvalidArgumentError(
            "--symbols must name at least one symbol",
            details={"field": "symbols", "value": value},
        )
    return {normalize_symbol(item) for item in items}


def _event_files(value: str) -> list[Path]:
    root = Path(value).expanduser()
    if root.is_dir():
        files = sorted(
            path for path in root.iterdir() if path.is_file() and path.suffix.lower() == ".json"
        )
    elif root.is_file():
        files = [root]
    else:
        raise InvalidArgumentError(
            "input path does not exist",
            details={"field": "input", "value": value},
        )
    if not files:
        raise NotFoundError("no event JSON files found", details={"input": str(root)})
    return files


def _backfill(
    config: Config,
    *,
    source: str,
    start: datetime,
    end: datetime,
    symbols: set[str] | None,
    data_type: str | None,
    input_path: str,
    dry_run: bool,
) -> dict[str, Any]:
    files = _event_files(input_path)
    truncated = len(files) > BACKFILL_MAX_EVENTS
    files = files[:BACKFILL_MAX_EVENTS]

    skipped = {
        "heartbeat": 0,
        "out_of_window": 0,
        "source_mismatch": 0,
        "symbol_filtered": 0,
        "data_type_filtered": 0,
    }
    failures: list[dict[str, str]] = []
    selected = landed = duplicates = invalid = failed = 0

    for path in files:
        try:
            payload, raw_text = _read_payload(str(path))
            validated = validate_event(payload, compliance_enabled=config.compliance_enabled)
        except FinanceCofferError as exc:
            invalid += 1
            failures.append(
                {"file": path.name, "stage": "validate", "reason": redact_text(exc.message)}
            )
            continue

        if validated.data_type is EventDataType.HEARTBEAT:
            # Contract 3.8: ops.source_heartbeat keeps the *current* state, so
            # replaying an old round would move last_seen backwards.
            skipped["heartbeat"] += 1
            continue
        if not start <= fetch_time_for(validated) < end:
            skipped["out_of_window"] += 1
            continue
        if resolve_source(validated) != source:
            skipped["source_mismatch"] += 1
            continue
        if symbols is not None and (validated.symbol or "").upper() not in symbols:
            skipped["symbol_filtered"] += 1
            continue
        if data_type is not None and validated.data_type.value != data_type:
            skipped["data_type_filtered"] += 1
            continue

        selected += 1
        if dry_run:
            continue
        try:
            result = _persist(config, validated, raw_text, source="backfill")
        except FinanceCofferError as exc:
            failed += 1
            failures.append(
                {"file": path.name, "stage": "write", "reason": redact_text(exc.message)}
            )
            continue
        if result.get("duplicate"):
            duplicates += 1
        else:
            landed += 1

    return {
        "source": source,
        "window": {"from": _rfc3339(start), "to": _rfc3339(end)},
        "data_type": data_type or "all",
        "symbols": sorted(symbols) if symbols else None,
        "input": str(Path(input_path).expanduser()),
        "scanned": len(files),
        "selected": selected,
        "landed": landed,
        "duplicates": duplicates,
        "skipped": sum(skipped.values()),
        "skipped_reasons": skipped,
        "invalid": invalid,
        "failed": failed,
        "would_write": selected if dry_run else 0,
        "truncated": truncated,
        "dry_run": dry_run,
        "failures": failures,
    }


# -- webhook ----------------------------------------------------------------


def _split_hosts(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in (value or "").split(",") if part.strip())


def _webhook_token(explicit: str | None) -> str | None:
    return explicit or os.environ.get(_WEBHOOK_TOKEN_ENV)


def _http_status(code: str) -> int:
    return _HTTP_STATUS.get(code, 500)


def build_webhook_app(
    config: Config,
    *,
    token: str | None = None,
    allowed_hosts: Sequence[str] = (),
) -> Any:
    """Build the Starlette receiver; the token and Host checks run per request."""

    try:
        from starlette.applications import Starlette
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import JSONResponse
        from starlette.routing import Route
    except ImportError:  # pragma: no cover - depends on install extras
        raise DependencyError(
            "the webhook extras are not installed",
            details={"hint": "install financecoffer[admin]"},
        ) from None

    hosts = tuple(host.strip().lower() for host in allowed_hosts if host.strip())

    def _failure(error: FinanceCofferError) -> Any:
        envelope = Envelope.from_error(error, trace_id=None)
        body = json.loads(envelope.model_dump_json())
        return JSONResponse(body, status_code=_http_status(error.code.value))

    def _preflight(request: Any) -> Any | None:
        if token is not None:
            scheme, _, presented = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), token):
                return _failure(AuthError("a valid bearer token is required"))
        if hosts:
            host = (request.headers.get("host") or "").split(":")[0].strip().lower()
            if host not in hosts:
                return _failure(
                    InvalidArgumentError(
                        "Host is not in the allowlist",
                        details={"host": host, "allowed": list(hosts)},
                    )
                )
        return None

    async def receive(request: Any) -> Any:
        rejected = _preflight(request)
        if rejected is not None:
            return rejected
        body = await request.body()
        try:
            text = body.decode("utf-8")
            payload = json.loads(text)
        except UnicodeDecodeError:
            return _failure(
                InvalidArgumentError("request body is not valid UTF-8", details={"field": "body"})
            )
        except json.JSONDecodeError as exc:
            return _failure(
                InvalidArgumentError(
                    "request body is not valid JSON",
                    details={"field": "body", "reason": f"line {exc.lineno} column {exc.colno}"},
                )
            )
        if not isinstance(payload, Mapping):
            return _failure(
                InvalidArgumentError(
                    "request body must contain a JSON object", details={"field": "body"}
                )
            )
        try:
            data = await run_in_threadpool(_ingest_event, config, payload, text)
        except FinanceCofferError as exc:
            return _failure(exc)
        envelope = Envelope.success(data, meta=Meta(source="etl"))
        return JSONResponse(json.loads(envelope.model_dump_json()))

    async def health(request: Any) -> Any:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/", receive, methods=["POST"]),
            Route("/events", receive, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ],
        debug=False,
    )


def _config(options: GlobalOptions) -> Config:
    try:
        config = load_config(overrides={"db_url": options.db_url})
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    if config.db_url is None:
        emit_failure(
            options,
            ConfigError(
                "database URL is not configured",
                details={"hint": "set FC_DB_URL or pass --db-url"},
            ),
        )
    return config


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
            data = _ingest_event(config, payload, original_text, write=not validate_only)
            if validate_only:
                data["valid"] = True
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=Meta(source="etl"))

    @ingest.command("backfill", help="Replay event documents from a time window into ODS.")
    def backfill(
        ctx: typer.Context,
        source: str = typer.Option(
            ..., "--source", help="Upstream source: " + " | ".join(_BACKFILL_SOURCES) + "."
        ),
        from_value: str = typer.Option(
            ..., "--from", help="Earliest fetch_time, YYYY-MM-DD or RFC 3339."
        ),
        to_value: str = typer.Option(
            ..., "--to", help="Latest fetch_time (a date includes that whole day)."
        ),
        symbols: str | None = typer.Option(
            None, "--symbols", help="Comma-separated symbols; default: every symbol."
        ),
        data_type: str = typer.Option("all", "--data-type", help="Event data type, or all."),
        input_path: str = typer.Option(
            ".", "--input", help="Event JSON file or directory (default: current directory)."
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Validate and filter only; write nothing."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            if source not in _BACKFILL_SOURCES:
                raise InvalidArgumentError(
                    "source must be one of: " + ", ".join(_BACKFILL_SOURCES),
                    details={
                        "field": "source",
                        "value": source,
                        "valid": list(_BACKFILL_SOURCES),
                    },
                )
            start, end = _window(from_value, to_value)
            wanted_type = _backfill_data_type(data_type)
            wanted_symbols = _backfill_symbols(symbols)
            config = _config(options)
            payload = _backfill(
                config,
                source=source,
                start=start,
                end=end,
                symbols=wanted_symbols,
                data_type=wanted_type,
                input_path=input_path,
                dry_run=dry_run,
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        if payload["invalid"] or payload["failed"]:
            emit_failure(
                options,
                PartialFailureError(
                    "some events could not be backfilled; see details.failures",
                    details=payload,
                ),
            )
        emit_success(options, payload, meta=Meta(source="etl"))

    @ingest.command("webhook", help="Run the webhook receiver for upstream events.")
    def webhook(
        ctx: typer.Context,
        host: str = typer.Option(
            "127.0.0.1", "--host", help="Bind address; default is loopback only."
        ),
        port: int = typer.Option(8101, "--port", min=1, max=65535, help="Listen port."),
        token: str | None = typer.Option(
            None, "--token", help=f"Bearer token; defaults to {_WEBHOOK_TOKEN_ENV}."
        ),
        allowed_hosts: str | None = typer.Option(
            None,
            "--allowed-hosts",
            help="Comma-separated Host allowlist; required on a non-loopback bind.",
        ),
    ) -> None:
        options = from_context(ctx)
        resolved_token = _webhook_token(token)
        hosts = _split_hosts(allowed_hosts)
        if host not in _LOOPBACK_HOSTS and (not resolved_token or not hosts):
            emit_failure(
                options,
                InvalidArgumentError(
                    "the webhook needs a token and a Host allowlist on a non-loopback address",
                    details={
                        "host": host,
                        "hint": f"pass --token (or {_WEBHOOK_TOKEN_ENV}) and --allowed-hosts",
                    },
                ),
            )
        try:
            config = _config(options)
            webhook_app = build_webhook_app(
                config,
                token=resolved_token,
                allowed_hosts=hosts,
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        try:
            import uvicorn
        except ImportError:
            emit_failure(
                options,
                DependencyError(
                    "the webhook extras are not installed",
                    details={"hint": "install financecoffer[admin]"},
                ),
            )
        typer.echo(f"[ingest webhook] listening on http://{host}:{port}", err=True)
        uvicorn.run(webhook_app, host=host, port=port, log_level=config.log_level.lower())


__all__ = [
    "BACKFILL_MAX_EVENTS",
    "build_webhook_app",
    "register",
]
