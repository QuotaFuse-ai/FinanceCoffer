"""Monitoring commands: scrape the running API's metrics and probe dependencies.

``metrics`` reads the live ``/metrics`` endpoint of a running ``fcoffer serve
api`` process; the CLI does not keep a registry of its own, so a call with no
API listening fails with a hint that names the command to start. ``health``
reuses the doctor probes for PostgreSQL, MinIO and Redis and adds an ETL check
that inspects the Redis consumer group.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import typer

from financecoffer.cli.commands import bootstrap as bootstrap_commands
from financecoffer.cli.commands.bootstrap import (
    ProbeContext,
    _check,
    _parse_checks,
    _sanitize_check,
)
from financecoffer.cli.options import OUTPUT_FORMATS, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.config.loader import load_cli_config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import (
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
)
from financecoffer.core.redaction import redact_text

DEFAULT_API_URL = "http://127.0.0.1:8100"
METRIC_FORMATS: tuple[str, ...] = ("prometheus", "json")
HEALTH_CHECKS: tuple[str, ...] = ("db", "minio", "redis", "etl")

_SAMPLE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?\s+(?P<value>[^\s]+)(?:\s+\d+)?\s*$"
)
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}

_Probe = Callable[[ProbeContext], dict[str, Any]]


def _choice(value: str, allowed: tuple[str, ...], option: str) -> str:
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise InvalidArgumentError(
            f"{option} must be one of: {', '.join(allowed)}",
            details={"field": option.lstrip("-"), "value": value, "valid": list(allowed)},
        )
    return normalized


def _unquote(value: str) -> str:
    out: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            out.append(_ESCAPES.get(char, char))
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    return "".join(out)


def _labels(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    return {name: _unquote(quoted) for name, quoted in _LABEL.findall(value)}


def parse_prometheus(text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse the Prometheus text exposition format into named metric groups.

    Only ``HELP`` / ``TYPE`` comments and sample lines are understood; anything
    else is counted as a skipped line instead of aborting the scrape. Values are
    kept as strings so ``+Inf`` / ``NaN`` survive a JSON round trip.
    """

    order: list[str] = []
    groups: dict[str, dict[str, Any]] = {}
    skipped = 0

    def group(name: str) -> dict[str, Any]:
        if name not in groups:
            groups[name] = {"name": name, "type": None, "help": None, "samples": []}
            order.append(name)
        return groups[name]

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line[1:].strip().split(None, 2)
            if len(parts) >= 2 and parts[0] == "HELP":
                group(parts[1])["help"] = parts[2] if len(parts) > 2 else ""
            elif len(parts) >= 2 and parts[0] == "TYPE":
                group(parts[1])["type"] = parts[2] if len(parts) > 2 else None
            continue
        match = _SAMPLE.match(line)
        if match is None:
            skipped += 1
            continue
        entry = group(match.group("name"))
        entry["samples"].append(
            {"labels": _labels(match.group("labels")), "value": match.group("value")}
        )
    return [groups[name] for name in order], skipped


def _trust_env_for(url: str) -> bool:
    return (urlsplit(url).hostname or "") not in {"127.0.0.1", "localhost", "::1"}


def _scrape(api_url: str, timeout: int) -> str:
    try:
        import httpx
    except ImportError:  # pragma: no cover - depends on install extras
        raise DependencyError(
            "httpx is not installed; cannot scrape the API",
            details={"hint": "install financecoffer[vector]"},
        ) from None

    base = api_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    url = f"{base}/metrics"
    try:
        with httpx.Client(timeout=min(timeout, 10), trust_env=_trust_env_for(url)) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPStatusError as exc:
        raise DependencyError(
            f"the API returned HTTP {exc.response.status_code} for {url}",
            details={
                "api_url": base,
                "hint": "check the API log; /metrics is served by `fcoffer serve api`",
            },
        ) from None
    except httpx.HTTPError as exc:
        raise DependencyError(
            f"no REST API is reachable at {base}",
            details={
                "api_url": base,
                "error": redact_text(str(exc).strip()),
                "hint": "start it with `fcoffer serve api` or pass --api-url",
            },
        ) from None


# -- health probes ----------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _probe_etl(context: ProbeContext) -> dict[str, Any]:
    """Report whether the ETL consumer group is attached and keeping up."""

    try:
        import redis
    except ImportError:  # pragma: no cover - depends on install extras
        return _check(
            "warn", "the Redis client is not installed", hint="install financecoffer[etl]"
        )

    config = context.config
    stream = config.event_stream
    group = config.event_group
    client = redis.Redis.from_url(
        config.redis_url,
        socket_connect_timeout=context.timeout,
        socket_timeout=context.timeout,
    )
    try:
        client.ping()
        try:
            groups = client.xinfo_groups(stream)
        except redis.ResponseError as exc:
            return _check(
                "warn",
                f"stream {stream} has no consumer groups; "
                "the ETL consumer does not appear to be running",
                stream=stream,
                group=group,
                error=redact_text(str(exc).strip()),
            )
        attached = next(
            (item for item in groups if _text(item.get("name")) == group),
            None,
        )
        if attached is None:
            return _check(
                "warn",
                f"consumer group {group} is not attached to {stream}; "
                "the ETL consumer does not appear to be running",
                stream=stream,
                group=group,
            )
        stale = client.xpending_range(stream, group, "-", "+", 1, idle=60_000)
    except Exception as exc:  # noqa: BLE001 - redis-py raises several transport errors
        return _check("fail", redact_text(str(exc).strip()))
    finally:
        client.close()

    pending = int(attached.get("pending") or 0)
    consumers = int(attached.get("consumers") or 0)
    details = {
        "stream": stream,
        "group": group,
        "consumers": consumers,
        "pending": pending,
    }
    if stale:
        return _check(
            "warn",
            "the ETL consumer group has pending entries idle for more than 60 seconds",
            **details,
        )
    return _check(
        "ok",
        f"consumer group {group} is attached with {consumers} consumer(s); "
        f"{pending} pending entries",
        **details,
    )


def health_probes() -> dict[str, _Probe]:
    """Doctor probes plus the ETL check, resolved at call time for tests."""

    probes = bootstrap_commands._probes()
    return {
        "db": probes["db"],
        "minio": probes["minio"],
        "redis": probes["redis"],
        "etl": _probe_etl,
    }


def register(app: typer.Typer) -> None:
    @app.command("metrics", help="Scrape Prometheus metrics from the running REST API.")
    def metrics(
        ctx: typer.Context,
        format: str = typer.Option(
            "prometheus", "--format", help="Output format: prometheus | json."
        ),
        api_url: str = typer.Option(
            DEFAULT_API_URL, "--api-url", help="Base URL of the running API."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            chosen = _choice(format, METRIC_FORMATS, "--format")
            text = _scrape(api_url, options.timeout)
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        if chosen == "prometheus":
            typer.echo(text if text.endswith("\n") else text + "\n", nl=False)
            return
        groups, skipped = parse_prometheus(text)
        data = {
            "api_url": api_url.strip().rstrip("/"),
            "format": "json",
            "metric_count": len(groups),
            "sample_count": sum(len(item["samples"]) for item in groups),
            "skipped_lines": skipped,
            "metrics": groups,
        }
        emit_success(options, data, meta=Meta(source="api"))

    @app.command("health", help="Probe PostgreSQL, MinIO, Redis and the ETL consumer.")
    def health(
        ctx: typer.Context,
        check: str | None = typer.Option(
            None,
            "--check",
            help="Comma-separated probes: db | minio | redis | etl.",
        ),
        output: str | None = typer.Option(
            None,
            "--output",
            "-o",
            help="Output format: json | table | csv | yaml (accepted for spec compatibility).",
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            if output is not None:
                _choice(output, OUTPUT_FORMATS, "--output")
            names = _parse_checks(check, order=HEALTH_CHECKS)
            config = load_cli_config(db_url=options.db_url)
            context = ProbeContext(
                config=config,
                dsn=options.db_url or config.db_url,
                timeout=options.timeout,
            )
            table = health_probes()
            results = [_sanitize_check(name, table[name](context)) for name in names]
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        failed = [item["name"] for item in results if item["status"] == "fail"]
        if failed:
            emit_failure(
                options,
                DependencyError(
                    "one or more health checks failed",
                    details={"status": "fail", "failed": failed, "checks": results},
                ),
            )
        degraded = any(item["status"] == "warn" for item in results)
        data = {
            "status": "degraded" if degraded else "ok",
            "checks": results,
            "failed": [],
        }
        emit_success(options, data, meta=Meta(source="health"))


__all__ = [
    "DEFAULT_API_URL",
    "HEALTH_CHECKS",
    "METRIC_FORMATS",
    "health_probes",
    "parse_prometheus",
    "register",
]
