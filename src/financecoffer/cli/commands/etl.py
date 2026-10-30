"""ETL consumer commands: consume, inspect, control, replay and DLQ."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

import typer
from sqlalchemy.exc import SQLAlchemyError

from financecoffer.cli.options import GlobalOptions, from_context, parse_int_option
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_cli_config
from financecoffer.core.config import Config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import (
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
    NotImplementedYetError,
    StorageError,
)
from financecoffer.db.session import Database
from financecoffer.etl.client import close_client, create_client, is_redis_error
from financecoffer.etl.consumer import Consumer, graceful_signals
from financecoffer.etl.control import pause, resume
from financecoffer.etl.dlq import list_entries, retry_dlq
from financecoffer.etl.metrics import METRICS
from financecoffer.etl.replay import replay_from_ods
from financecoffer.etl.status import collect_status

_META = Meta(source="etl")

_CONSUMER_BACKENDS = ("redis", "kafka", "webhook")
_MAX_CONSUME_BATCH = 1000
_MAX_BLOCK_MS = 60_000


def _config(options: GlobalOptions) -> Config:
    try:
        return load_cli_config(db_url=options.db_url)
    except FinanceCofferError as exc:
        emit_failure(options, exc)


def _redis_error(options: GlobalOptions, exc: BaseException, action: str) -> NoReturn:
    if isinstance(exc, FinanceCofferError):
        emit_failure(options, exc)
    if is_redis_error(exc):
        emit_failure(
            options,
            DependencyError(
                f"Redis operation failed during {action}",
                details={"hint": "check FC_REDIS_URL and that Redis is reachable"},
            ),
        )
    raise exc


def _database_error(options: GlobalOptions, exc: BaseException, action: str) -> NoReturn:
    if isinstance(exc, FinanceCofferError):
        emit_failure(options, exc)
    if isinstance(exc, SQLAlchemyError):
        emit_failure(
            options,
            StorageError(
                f"database operation failed during {action}",
                details={"hint": "check FC_DB_URL and that migrations are applied"},
            ),
        )
    raise exc


def _with_redis(
    options: GlobalOptions,
    config: Config,
    action: str,
    run: Callable[[Any], Any],
) -> Any:
    client = create_client(config.redis_url)
    try:
        return run(client)
    except Exception as exc:  # noqa: BLE001 - translated below
        _redis_error(options, exc, action)
    finally:
        close_client(client)


def _with_database(
    options: GlobalOptions,
    config: Config,
    action: str,
    run: Callable[[Database], Any],
) -> Any:
    database = Database(config.db_url)
    try:
        return run(database)
    except (FinanceCofferError, SQLAlchemyError) as exc:
        _database_error(options, exc, action)
    finally:
        database.dispose()


def _with_redis_and_database(
    options: GlobalOptions,
    config: Config,
    action: str,
    run: Callable[[Any, Database], Any],
) -> Any:
    client = create_client(config.redis_url)
    database = Database(config.db_url)
    try:
        return run(client, database)
    except Exception as exc:  # noqa: BLE001 - translated below
        if isinstance(exc, SQLAlchemyError):
            _database_error(options, exc, action)
        _redis_error(options, exc, action)
    finally:
        close_client(client)
        database.dispose()


def _read_payload_file(path: str) -> tuple[Mapping[str, Any], str]:
    """Read a corrected event document for ``etl dlq retry --file``."""

    try:
        text = Path(path).read_bytes().decode("utf-8")
    except OSError as exc:
        raise InvalidArgumentError(
            f"cannot read payload file: {path}",
            details={"errors": [{"field": "file", "reason": exc.strerror or str(exc)}]},
        ) from exc
    except UnicodeDecodeError as exc:
        raise InvalidArgumentError(
            "payload file is not valid UTF-8",
            details={"errors": [{"field": "file", "reason": f"invalid UTF-8 at byte {exc.start}"}]},
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidArgumentError(
            "payload file is not valid JSON",
            details={
                "errors": [
                    {"field": "file", "reason": f"line {exc.lineno} column {exc.colno}: {exc.msg}"}
                ]
            },
        ) from exc
    if not isinstance(payload, Mapping):
        raise InvalidArgumentError(
            "payload file must contain a JSON object",
            details={"errors": [{"field": "file", "reason": f"got {type(payload).__name__}"}]},
        )
    return payload, text


def register(app: typer.Typer) -> None:
    etl = add_group(app, name="etl", help_text="Event ingestion pipeline.")

    @etl.command(
        "consume",
        help="Consume upstream events until stopped; --once handles one batch.",
    )
    def consume(
        ctx: typer.Context,
        consumer: str = typer.Option(
            "redis",
            "--consumer",
            help="Backend to consume from; only redis is implemented.",
        ),
        group: str | None = typer.Option(
            None, "--group", help="Consumer group (default: FC_EVENT_GROUP)."
        ),
        consumer_name: str | None = typer.Option(
            None,
            "--consumer-name",
            help="Stable consumer name (default: hostname).",
        ),
        once: bool = typer.Option(
            False, "--once", help="Handle at most one batch, then exit."
        ),
        batch_size: str | None = typer.Option(
            None, "--batch-size", help=f"Entries per read (1-{_MAX_CONSUME_BATCH})."
        ),
        block_ms: str | None = typer.Option(
            None,
            "--block-ms",
            help=f"XREADGROUP block time in ms (0-{_MAX_BLOCK_MS}); 0 disables blocking.",
        ),
        claim_idle_ms: str | None = typer.Option(
            None,
            "--claim-idle-ms",
            help="Reclaim entries idle longer than this (>= 0); 0 disables XAUTOCLAIM.",
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            batch_value = (
                None
                if batch_size is None
                else parse_int_option(
                    batch_size, field="batch_size", minimum=1, maximum=_MAX_CONSUME_BATCH
                )
            )
            block_value = (
                None
                if block_ms is None
                else parse_int_option(
                    block_ms, field="block_ms", minimum=0, maximum=_MAX_BLOCK_MS
                )
            )
            claim_value = (
                None
                if claim_idle_ms is None
                else parse_int_option(claim_idle_ms, field="claim_idle_ms", minimum=0)
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        config = _config(options)
        if consumer not in _CONSUMER_BACKENDS:
            emit_failure(
                options,
                InvalidArgumentError(
                    "unsupported consumer backend",
                    details={"field": "consumer", "value": consumer, "supported": ["redis"]},
                ),
            )
        if consumer != "redis":
            emit_failure(
                options,
                NotImplementedYetError(
                    f"consumer backend {consumer!r} is not implemented yet",
                    details={"field": "consumer", "supported": ["redis"]},
                ),
            )

        client = create_client(config.redis_url)
        database = Database(config.db_url)
        data: dict[str, Any] = {}
        try:
            instance = Consumer(
                client=client,
                database=database,
                config=config,
                consumer_name=consumer_name,
                group=group,
            )
            with graceful_signals(instance):
                report = instance.run(
                    once=once,
                    batch_size=batch_value,
                    block_ms=0 if once and block_value is None else block_value,
                    claim_idle_ms=claim_value,
                )
            data = report.as_dict()
            data["metrics"] = METRICS.snapshot()
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        except SQLAlchemyError as exc:
            _database_error(options, exc, "etl consume")
        finally:
            close_client(client)
            database.dispose()
        emit_success(options, data, meta=_META)

    @etl.command("status", help="Show lag, pending entries, DLQ depth and the last error.")
    def status(
        ctx: typer.Context,
        consumer_name: str | None = typer.Option(
            None, "--consumer-name", help="Report this consumer's pending share."
        ),
    ) -> None:
        options = from_context(ctx)
        config = _config(options)
        data = _with_redis_and_database(
            options,
            config,
            "etl status",
            lambda client, database: collect_status(
                client=client,
                database=database,
                config=config,
                consumer_name=consumer_name,
            ),
        )
        emit_success(options, data, meta=_META)

    @etl.command("pause", help="Stop handing out new messages to consumers.")
    def pause_command(
        ctx: typer.Context,
        reason: str | None = typer.Option(None, "--reason", help="Why the pipeline is paused."),
    ) -> None:
        options = from_context(ctx)
        config = _config(options)
        data = _with_redis(
            options,
            config,
            "etl pause",
            lambda client: pause(client, config.etl_control_key, reason=reason),
        )
        emit_success(options, data, meta=_META)

    @etl.command("resume", help="Allow consumers to pick up new messages again.")
    def resume_command(ctx: typer.Context) -> None:
        options = from_context(ctx)
        config = _config(options)
        data = _with_redis(
            options,
            config,
            "etl resume",
            lambda client: resume(client, config.etl_control_key),
        )
        emit_success(options, data, meta=_META)

    @etl.command(
        "replay",
        help=(
            "Reset the etl event log of stored ODS records. It does not re-publish"
            " them to the stream: a later redelivery of the same event is accepted"
            " instead of skipped, so re-publish upstream and consume with --force."
        ),
    )
    def replay(
        ctx: typer.Context,
        from_ods: bool = typer.Option(
            False, "--from-ods", help="Replay from ODS (the only supported source)."
        ),
        data_type: str | None = typer.Option(
            None, "--data-type", help="Limit to one event data type."
        ),
        symbol: str | None = typer.Option(None, "--symbol", help="Limit to one symbol."),
        since: str | None = typer.Option(
            None, "--since", help="Earliest fetch_time (YYYY-MM-DD or RFC 3339)."
        ),
        until: str | None = typer.Option(
            None, "--until", help="Latest fetch_time, inclusive for a date (YYYY-MM-DD)."
        ),
        limit: str | None = typer.Option(
            None,
            "--limit",
            help="Maximum records (>= 1); values above the replay cap are reduced to it.",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Only print the manifest; change nothing."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            limit_value = (
                None
                if limit is None
                else parse_int_option(limit, field="limit", minimum=1)
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        config = _config(options)
        if not from_ods:
            emit_failure(
                options,
                InvalidArgumentError(
                    "only --from-ods replay is supported",
                    details={"field": "from-ods"},
                ),
            )
        data = _with_database(
            options,
            config,
            "etl replay",
            lambda database: replay_from_ods(
                database=database,
                data_type=data_type,
                symbol=symbol,
                since=since,
                until=until,
                limit=limit_value,
                dry_run=dry_run,
            ),
        )
        emit_success(options, data, meta=_META)

    dlq = add_group(etl, name="dlq", help_text="Dead-letter queue operations.")

    @dlq.command("list", help="List pending dead-letter entries.")
    def list_command(
        ctx: typer.Context,
        limit: int = typer.Option(
            50, "--limit", min=1, max=1000, help="Maximum entries to show."
        ),
    ) -> None:
        options = from_context(ctx)
        config = _config(options)
        entries = _with_database(
            options,
            config,
            "etl dlq list",
            lambda database: list_entries(database, limit=limit),
        )
        emit_success(options, {"count": len(entries), "entries": entries}, meta=_META)

    @dlq.command("retry", help="Replay one dead letter (or every pending one).")
    def retry_command(
        ctx: typer.Context,
        event_id: str | None = typer.Argument(
            None, metavar="EVENT_ID", help="Event id to retry."
        ),
        dlq_id: int | None = typer.Option(None, "--id", min=1, help="Dead-letter row id."),
        all_entries: bool = typer.Option(False, "--all", help="Retry every pending entry."),
        file: str | None = typer.Option(
            None, "--file", help="Corrected event JSON to apply instead of the stored payload."
        ),
        limit: int = typer.Option(
            100, "--limit", min=1, max=1000, help="Maximum entries for --all."
        ),
    ) -> None:
        options = from_context(ctx)
        config = _config(options)
        targets = sum(value is not None for value in (event_id, dlq_id)) + int(all_entries)
        if targets != 1:
            emit_failure(
                options,
                InvalidArgumentError(
                    "pass exactly one of an event id, --id or --all",
                    details={"field": "event_id"},
                ),
            )
        payload: Mapping[str, Any] | None = None
        payload_text: str | None = None
        if file is not None:
            if all_entries:
                emit_failure(
                    options,
                    InvalidArgumentError(
                        "a corrected payload cannot be applied to every pending entry",
                        details={"field": "file"},
                    ),
                )
            payload, payload_text = _read_payload_file(file)

        data = _with_redis_and_database(
            options,
            config,
            "etl dlq retry",
            lambda client, database: retry_dlq(
                client=client,
                database=database,
                config=config,
                dlq_id=dlq_id,
                event_id=event_id,
                all_pending=all_entries,
                payload=payload,
                payload_text=payload_text,
                limit=limit,
            ),
        )
        emit_success(options, data, meta=_META)
