"""Bootstrap commands: init, doctor and the config group."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import typer

from financecoffer.cli.commands import config_secret
from financecoffer.cli.options import from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import env_var_name, load_cli_config
from financecoffer.core.config import Config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import (
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
)
from financecoffer.core.redaction import redact_text, redact_value
from financecoffer.db.connection import connect, describe_dsn, resolve_cli_dsn
from financecoffer.db.migrations import (
    MIGRATIONS_DIR,
    MigrationRunner,
    discover_migrations,
)

_INIT_PARTITION_AHEAD = 3

_PROBE_ORDER: tuple[str, ...] = ("db", "minio", "redis", "embedding", "mcp")
_PROBE_STATUSES: frozenset[str] = frozenset({"ok", "warn", "fail"})


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


# -- init -------------------------------------------------------------------


def _months(start: date, ahead: int) -> list[date]:
    months: list[date] = []
    year, month = start.year, start.month
    for _ in range(ahead + 1):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _init_plan(dsn: str, *, with_extensions: bool) -> dict[str, Any]:
    """Describe the steps ``init`` would run without opening a connection."""

    migrations = discover_migrations()
    steps: list[dict[str, Any]] = []
    if with_extensions:
        steps.append({"step": "extension", "name": "vector"})
    for migration in migrations:
        steps.append({"step": "migration", "version": migration.version, "name": migration.name})
    for month in _months(date.today().replace(day=1), _INIT_PARTITION_AHEAD):
        steps.append(
            {
                "step": "partition",
                "table": "ods.stock_raw_data",
                "month": f"{month:%Y-%m}",
            }
        )
    steps.append(
        {
            "step": "index",
            "table": "vector.announcement_chunk",
            "name": "idx_ann_chunk_embedding",
            "migration": 6,
        }
    )
    return {
        "dry_run": True,
        "database": describe_dsn(dsn),
        "with_extensions": with_extensions,
        "migrations_dir": MIGRATIONS_DIR.name,
        "head_version": migrations[-1].version,
        "steps": steps,
    }


def _ensure_extensions(dsn: str, timeout: int) -> list[dict[str, str]]:
    """Re-assert the required extensions; safe to run on every ``init``."""

    with connect(dsn, timeout=timeout) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
        rows = cur.fetchall()
    return [{"name": str(row[0]), "version": str(row[1])} for row in rows]


# -- doctor -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeContext:
    """Everything a doctor probe may read, with secrets kept as config fields."""

    config: Config
    dsn: str | None
    timeout: int


def _check(status: str, reason: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "reason": " ".join(str(reason).split())}
    if details:
        payload["details"] = details
    return payload


def _trust_env_for(url: str) -> bool:
    """A local service must never be routed through the host's HTTP proxy."""

    return (urlsplit(url).hostname or "") not in {"127.0.0.1", "localhost", "::1"}


def _probe_db(context: ProbeContext) -> dict[str, Any]:
    if not context.dsn:
        return _check("fail", "FC_DB_URL is not configured")
    try:
        dsn = resolve_cli_dsn(context.dsn)
        with connect(dsn, timeout=context.timeout) as conn, conn.cursor() as cur:
            cur.execute("SHOW server_version")
            version = str(cur.fetchone()[0])
            cur.execute("SELECT extname FROM pg_extension ORDER BY extname")
            extensions = [str(row[0]) for row in cur.fetchall()]
    except FinanceCofferError as exc:
        return _check("fail", str(exc), error=(exc.details or {}).get("error"))

    details: dict[str, Any] = {
        "version": version,
        "extensions": extensions,
        "database": describe_dsn(dsn),
    }
    if "vector" not in extensions:
        return _check(
            "warn",
            f"PostgreSQL {version} is reachable but the vector extension is missing",
            **details,
        )
    return _check("ok", f"PostgreSQL {version} with vector extension", **details)


def _probe_minio(context: ProbeContext) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:  # pragma: no cover - depends on install extras
        return _check(
            "warn",
            "httpx is not installed; cannot probe MinIO",
            hint="install financecoffer[vector]",
        )

    bucket = context.config.minio_bucket
    endpoint = context.config.minio_endpoint.strip().rstrip("/")
    base = endpoint if endpoint.startswith(("http://", "https://")) else f"http://{endpoint}"
    try:
        with httpx.Client(
            timeout=min(context.timeout, 10), trust_env=_trust_env_for(base)
        ) as client:
            health = client.get(f"{base}/minio/health/live")
            if health.status_code != 200:
                return _check(
                    "fail",
                    f"MinIO health endpoint returned HTTP {health.status_code}",
                    endpoint=base,
                )
            # An anonymous HEAD is enough: 200 means a public bucket, 403 means the
            # bucket exists behind authentication, 404 means it is missing.
            probe = client.head(f"{base}/{bucket}/")
            status = probe.status_code
    except httpx.HTTPError as exc:
        return _check("fail", redact_text(str(exc).strip()), endpoint=base)

    details = {"endpoint": base, "bucket": bucket}
    if status in (200, 403):
        return _check("ok", f"MinIO is healthy and bucket {bucket} exists", **details)
    if status == 404:
        return _check("fail", f"MinIO is healthy but bucket {bucket} does not exist", **details)
    return _check(
        "warn",
        f"MinIO is healthy but the bucket probe returned HTTP {status}",
        **details,
    )


def _probe_redis(context: ProbeContext) -> dict[str, Any]:
    try:
        import redis
    except ImportError:  # pragma: no cover - depends on install extras
        return _check(
            "warn",
            "the Redis client is not installed",
            hint="install financecoffer[etl]",
        )

    stream = context.config.event_stream
    client = redis.Redis.from_url(
        context.config.redis_url,
        socket_connect_timeout=context.timeout,
        socket_timeout=context.timeout,
    )
    try:
        client.ping()
        length = int(client.xlen(stream))
    except Exception as exc:  # noqa: BLE001 - redis-py raises several transport errors
        return _check("fail", redact_text(str(exc).strip()))
    finally:
        client.close()
    if length == 0:
        return _check(
            "warn",
            f"Redis is reachable but stream {stream} has no entries",
            stream=stream,
        )
    return _check(
        "ok",
        f"Redis is reachable; stream {stream} has {length} entries",
        stream=stream,
    )


def _probe_embedding(context: ProbeContext) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:  # pragma: no cover - depends on install extras
        return _check(
            "warn",
            "httpx is not installed; cannot probe the embedding service",
            hint="install financecoffer[vector]",
        )

    config = context.config
    payload = {"model": config.embedding_model, "input": ["fcoffer doctor"]}
    headers: dict[str, str] = {}
    if config.embedding_api_key is not None:
        headers["Authorization"] = f"Bearer {config.embedding_api_key.get_secret_value()}"
    try:
        with httpx.Client(
            timeout=min(context.timeout, 10), trust_env=_trust_env_for(config.embedding_url)
        ) as client:
            response = client.post(config.embedding_url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        return _check("fail", redact_text(str(exc).strip()), url=config.embedding_url)
    except ValueError:
        return _check("fail", "embedding service returned invalid JSON", url=config.embedding_url)

    data = body.get("data") if isinstance(body, Mapping) else None
    vector = data[0].get("embedding") if isinstance(data, list) and data else None
    if not isinstance(vector, list):
        return _check("fail", "embedding service returned no vector", url=config.embedding_url)
    if len(vector) != config.embedding_dim:
        return _check(
            "fail",
            "embedding dimension "
            f"{len(vector)} does not match FC_EMBEDDING_DIM {config.embedding_dim}",
            url=config.embedding_url,
        )
    return _check(
        "ok",
        f"embedding service returned a {config.embedding_dim}-dimension vector "
        f"from {config.embedding_model}",
        url=config.embedding_url,
    )


def _probe_mcp(context: ProbeContext) -> dict[str, Any]:
    try:
        import mcp.types as types
        from pydantic import TypeAdapter

        from financecoffer.mcp.server import create_server
        from financecoffer.query.service import QueryService
    except ImportError:  # pragma: no cover - depends on install extras
        return _check("warn", "the MCP extras are not installed", hint="install financecoffer[mcp]")

    try:
        frame = types.JSONRPCRequest(
            jsonrpc="2.0",
            id=1,
            method="initialize",
            params={
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "fcoffer", "version": "doctor"},
            },
        )
        TypeAdapter(types.JSONRPCMessage).validate_python(frame.model_dump(mode="json"))
        server = create_server(QueryService(config=context.config, dsn=context.dsn))
        tools = sorted(tool.name for tool in server._tool_manager.list_tools())
    except Exception as exc:  # noqa: BLE001 - any SDK wiring failure is a failed probe
        return _check("fail", f"MCP server failed to build: {redact_text(str(exc).strip())}")

    details = {"protocol": types.LATEST_PROTOCOL_VERSION, "tools": tools}
    if len(tools) != 3:
        return _check("warn", f"MCP server exposes {len(tools)} tools, expected 3", **details)
    return _check(
        "ok",
        f"MCP protocol frame {types.LATEST_PROTOCOL_VERSION} accepted; "
        f"{len(tools)} tools registered",
        **details,
    )


def _probes() -> dict[str, Callable[[ProbeContext], dict[str, Any]]]:
    """Resolve probes at call time so tests can replace individual functions."""

    return {
        "db": _probe_db,
        "minio": _probe_minio,
        "redis": _probe_redis,
        "embedding": _probe_embedding,
        "mcp": _probe_mcp,
    }


def _parse_checks(value: str | None, *, order: Sequence[str] = _PROBE_ORDER) -> list[str]:
    if value is None or not value.strip():
        return list(order)
    requested = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not requested:
        raise InvalidArgumentError(
            "--check must name at least one probe",
            details={"valid": list(order)},
        )
    unknown = [name for name in requested if name not in order]
    if unknown:
        raise InvalidArgumentError(
            "unknown --check value",
            details={"unknown": unknown, "valid": list(order)},
        )
    wanted = set(requested)
    return [name for name in order if name in wanted]


def _sanitize(value: Any, key: object = None) -> Any:
    """Recursively mask credentials in probe output, whatever a probe returned."""

    if isinstance(value, Mapping):
        return {str(name): _sanitize(item, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return redact_value(key, value)


def _sanitize_check(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    status = str(raw.get("status", "fail")).strip().lower()
    if status not in _PROBE_STATUSES:
        status = "fail"
    reason = " ".join(str(raw.get("reason", "probe returned no reason")).split())
    result: dict[str, Any] = {
        "name": name,
        "status": status,
        "reason": redact_text(reason),
    }
    details = raw.get("details")
    if isinstance(details, Mapping) and details:
        result["details"] = _sanitize(details)
    return result


# -- config -----------------------------------------------------------------


def _missing_item(field_name: str, reason: str) -> dict[str, str]:
    return {"field": field_name, "env": env_var_name(field_name), "reason": reason}


def _missing_config(config: Config) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    if config.db_url is None:
        missing.append(_missing_item("db_url", "not set"))
    if config.minio_access_key is None:
        missing.append(_missing_item("minio_access_key", "not set"))
    if config.minio_secret_key is None:
        missing.append(_missing_item("minio_secret_key", "not set"))
    if config.embedding_provider == "openai" and config.embedding_api_key is None:
        missing.append(
            _missing_item("embedding_api_key", "required when FC_EMBEDDING_PROVIDER=openai")
        )
    return missing


def register(app: typer.Typer) -> None:
    @app.command("init", help="Create schemas, extensions, tables, partitions and the HNSW index.")
    def init(
        ctx: typer.Context,
        db_url: str | None = typer.Option(
            None, "--db-url", help="Override FC_DB_URL for this command."
        ),
        with_extensions: bool = typer.Option(
            False,
            "--with-extensions",
            help="Re-assert required extensions before reporting (idempotent).",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Print the planned steps without touching the database."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            dsn = resolve_cli_dsn(db_url or options.db_url)
            if dry_run:
                data = _init_plan(dsn, with_extensions=with_extensions)
            else:
                report = MigrationRunner(dsn, timeout=options.timeout).apply(
                    ahead_months=_INIT_PARTITION_AHEAD
                )
                data = report.as_dict()
                data["with_extensions"] = with_extensions
                if with_extensions:
                    data["extensions"] = _ensure_extensions(dsn, options.timeout)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=Meta(source="db"))

    @app.command("doctor", help="Probe PostgreSQL, MinIO, Redis, embedding and MCP.")
    def doctor(
        ctx: typer.Context,
        check: str | None = typer.Option(
            None,
            "--check",
            help="Comma-separated probes: db | minio | redis | embedding | mcp.",
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            names = _parse_checks(check)
            config = load_cli_config(db_url=options.db_url)
            probe_context = ProbeContext(
                config=config,
                dsn=options.db_url or config.db_url,
                timeout=options.timeout,
            )
            table = _probes()
            results = [_sanitize_check(name, table[name](probe_context)) for name in names]
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        failed = [item["name"] for item in results if item["status"] == "fail"]
        if failed:
            emit_failure(
                options,
                DependencyError(
                    "one or more dependency checks failed",
                    details={
                        "status": "fail",
                        "failed": failed,
                        "checks": results,
                    },
                ),
            )
        degraded = any(item["status"] == "warn" for item in results)
        data = {
            "status": "degraded" if degraded else "ok",
            "checks": results,
            "failed": [],
        }
        emit_success(options, data, meta=Meta(source="doctor"))

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
            resolved = load_cli_config(db_url=options.db_url)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        _ = redact  # redaction is unconditional; the flag only documents intent
        if as_json:
            emit_success(options, resolved.redacted_dict(), meta=Meta(source="config"))
            return
        typer.echo(_render_text(resolved))

    @config.command("check", help="Validate configuration and name missing environment variables.")
    def check(ctx: typer.Context) -> None:
        options = from_context(ctx)
        try:
            resolved = load_cli_config(db_url=options.db_url)
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        missing = _missing_config(resolved)
        data = {
            "valid": True,
            "db_url_configured": resolved.db_url is not None,
            "minio_configured": (
                resolved.minio_access_key is not None and resolved.minio_secret_key is not None
            ),
            "embedding_provider": resolved.embedding_provider,
            "missing": missing,
            "missing_env": [item["env"] for item in missing],
            "warnings": [f"{item['env']} is {item['reason']}" for item in missing],
        }
        emit_success(options, data, meta=Meta(source="config"))

    config_secret.register(config)


__all__ = ["ProbeContext", "register"]
