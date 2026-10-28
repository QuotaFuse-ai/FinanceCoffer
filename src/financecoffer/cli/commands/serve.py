"""Service commands: serve api/admin and start-all.

``serve admin`` renders a read-only status page: the four dependency probes
(shared with ``fcoffer health``), the pending DLQ count and the latest archive
job. It never writes to the warehouse.

``start-all`` runs the REST API in the main process and the ETL consumer as a
child process. A crashing child is restarted until it crashes three times within
five minutes; then the child is stopped and an alert is logged while the API
keeps serving. SIGINT/SIGTERM stop the child before the API.
"""

from __future__ import annotations

import html
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from subprocess import Popen
from typing import Any

import typer

from financecoffer.cli.commands.bootstrap import ProbeContext, _sanitize_check
from financecoffer.cli.commands.monitor import HEALTH_CHECKS, health_probes
from financecoffer.cli.options import from_context
from financecoffer.cli.output import emit_failure
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_config
from financecoffer.core.config import Config
from financecoffer.core.errors import (
    ConfigError,
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
)
from financecoffer.core.logging import get_logger
from financecoffer.core.redaction import redact_text
from financecoffer.db.connection import connect, resolve_dsn

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_CONSUMERS: tuple[str, ...] = ("redis", "kafka", "webhook")

_log = get_logger("serve")


def _reject_non_loopback(options: Any, host: str) -> None:
    if host not in _LOOPBACK_HOSTS:
        emit_failure(
            options,
            InvalidArgumentError(
                "serving on a non-loopback address requires token auth and a Host allowlist",
                details={
                    "host": host,
                    "hint": "bind 127.0.0.1 and put an authenticating proxy in front",
                },
            ),
        )


# -- admin status -----------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": redact_text(reason)}


def _warehouse_summary(dsn: str | None, timeout: int) -> dict[str, Any]:
    """Read the DLQ and archive bookkeeping; a DB outage degrades, never 500s."""

    if not dsn:
        reason = "FC_DB_URL is not configured"
        return {"dlq": _unavailable(reason), "archive": _unavailable(reason)}
    try:
        resolved = resolve_dsn(dsn)
        with connect(resolved, timeout=timeout) as conn, conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM ops.etl_dlq GROUP BY status")
            dlq = {str(status): int(count) for status, count in cur.fetchall()}
            cur.execute("SELECT status, count(*) FROM ops.archive_job GROUP BY status")
            archive = {str(status): int(count) for status, count in cur.fetchall()}
            cur.execute(
                "SELECT table_name, symbol, status, rows_archived, bytes_archived, "
                "started_at, finished_at FROM ops.archive_job "
                "ORDER BY created_at DESC LIMIT 1"
            )
            latest = cur.fetchone()
    except FinanceCofferError as exc:
        return {"dlq": _unavailable(str(exc)), "archive": _unavailable(str(exc))}
    except Exception as exc:  # noqa: BLE001 - the status page degrades instead of 500ing
        reason = f"cannot read warehouse bookkeeping: {exc}"
        return {"dlq": _unavailable(reason), "archive": _unavailable(reason)}

    if latest is None:
        recent = None
    else:
        recent = {
            "table_name": str(latest[0]),
            "symbol": latest[1],
            "status": str(latest[2]),
            "rows_archived": int(latest[3]),
            "bytes_archived": int(latest[4]),
            "started_at": _iso(latest[5]),
            "finished_at": _iso(latest[6]),
        }
    return {
        "dlq": {"available": True, "pending": dlq.get("pending", 0), "by_status": dlq},
        "archive": {"available": True, "by_status": archive, "latest": recent},
    }


def admin_snapshot(config: Config, *, dsn: str | None, timeout: int) -> dict[str, Any]:
    """Collect everything the admin page shows; read-only by construction."""

    context = ProbeContext(config=config, dsn=dsn, timeout=timeout)
    probes = health_probes()
    checks = [_sanitize_check(name, probes[name](context)) for name in HEALTH_CHECKS]
    status = {
        "generated_at": _iso(datetime.now(UTC)),
        "status": (
            "fail"
            if any(item["status"] == "fail" for item in checks)
            else ("degraded" if any(item["status"] == "warn" for item in checks) else "ok")
        ),
        "checks": checks,
    }
    status.update(_warehouse_summary(dsn, timeout))
    return status


def _cell(value: Any) -> str:
    if value is None:
        return "&mdash;"
    return html.escape(str(value))


def render_admin_page(status: dict[str, Any]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{_cell(item.get('name'))}</td>"
        f'<td class="{_cell(item.get("status"))}">{_cell(item.get("status"))}</td>'
        f"<td>{_cell(item.get('reason'))}</td>"
        "</tr>"
        for item in status.get("checks", [])
    )
    dlq = status.get("dlq") or {}
    archive = status.get("archive") or {}
    latest = archive.get("latest") or {}
    if dlq.get("available"):
        dlq_text = f"{_cell(dlq.get('pending'))} pending"
    else:
        dlq_text = f"unavailable: {_cell(dlq.get('reason'))}"
    if archive.get("available"):
        archive_text = _cell(latest.get("status")) if latest else "no jobs yet"
        if latest:
            archive_text += f" &middot; {_cell(latest.get('table_name'))}"
    else:
        archive_text = f"unavailable: {_cell(archive.get('reason'))}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FinanceCoffer status</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 52rem; padding: 0 1rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
th, td {{ text-align: left; border-bottom: 1px solid #ddd; padding: 0.5rem; vertical-align: top; }}
.ok {{ color: #1a7f37; }} .warn {{ color: #9a6700; }} .fail {{ color: #cf222e; }}
</style>
</head>
<body>
<h1>FinanceCoffer status</h1>
<p>Overall: <strong class="{_cell(status.get("status"))}">{_cell(status.get("status"))}</strong>
&middot; generated {_cell(status.get("generated_at"))}</p>
<table>
<thead><tr><th>Dependency</th><th>Status</th><th>Details</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<p>DLQ: {dlq_text}<br>Archive: {archive_text}</p>
</body>
</html>
"""


def build_admin_app(snapshot: Callable[[], dict[str, Any]]) -> Any:
    """Build the read-only Starlette app; ``snapshot`` is resolved per request."""

    try:
        from starlette.applications import Starlette
        from starlette.responses import HTMLResponse, JSONResponse
        from starlette.routing import Route
    except ImportError:  # pragma: no cover - depends on install extras
        raise DependencyError(
            "the admin extras are not installed",
            details={"hint": "install financecoffer[admin]"},
        ) from None

    def index(request: Any) -> HTMLResponse:
        return HTMLResponse(render_admin_page(snapshot()))

    def status(request: Any) -> JSONResponse:
        return JSONResponse(snapshot())

    return Starlette(routes=[Route("/", index), Route("/status", status)], debug=False)


# -- start-all supervision --------------------------------------------------


def _spawn_consumer(consumer: str) -> Popen[bytes]:
    return Popen(
        [sys.executable, "-m", "financecoffer", "etl", "consume", "--consumer", consumer],
        close_fds=True,
    )


@dataclass
class ConsumerSupervisor:
    """Restart a crashed ETL consumer, giving up after repeated fast failures."""

    spawn: Callable[[], Any] = _spawn_consumer  # type: ignore[assignment]
    consumer: str = "redis"
    max_restarts: int = 3
    window_seconds: float = 300.0
    stop_timeout: float = 10.0
    clock: Callable[[], float] = time.monotonic
    alert: Callable[[str], None] = lambda message: _alert(message)
    process: Any = None
    crashes: list[float] = field(default_factory=list[float])
    stopped: bool = False

    def start(self) -> None:
        self.process = self.spawn()

    def poll(self) -> str | None:
        """Reap one exit; returns ``restarted``, ``stopped`` or ``None``."""

        if self.stopped or self.process is None:
            return None
        if self.process.poll() is None:
            return None
        now = self.clock()
        self.crashes = [at for at in self.crashes if now - at <= self.window_seconds]
        self.crashes.append(now)
        if len(self.crashes) >= self.max_restarts:
            self.stopped = True
            self.process = None
            self.alert(
                f"the {self.consumer} ETL consumer crashed {len(self.crashes)} times "
                f"within {int(self.window_seconds)} seconds; it is stopped and the API "
                "keeps serving"
            )
            return "stopped"
        self.process = self.spawn()
        return "restarted"

    def stop(self) -> None:
        self.stopped = True
        process = self.process
        self.process = None
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=self.stop_timeout)
        except Exception:  # noqa: BLE001 - any wait failure falls back to kill
            process.kill()
            process.wait(timeout=self.stop_timeout)


def _alert(message: str) -> None:
    _log.error(message)
    typer.echo(f"[start-all] {message}", err=True)


def _install_signal_handlers(stop: threading.Event) -> dict[int, Any]:
    def handler(signum: int, frame: Any) -> None:
        stop.set()

    previous: dict[int, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, handler)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def _serve_all(
    *,
    config: Config,
    host: str,
    port: int,
    consumer: str,
    with_etl: bool,
    stop_timeout: float = 10.0,
) -> None:
    """Run the API in-process and supervise the ETL child until a signal arrives."""

    import uvicorn

    from financecoffer.api import create_app

    app = create_app(config=config)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level=config.log_level.lower(), workers=1)
    )
    stop = threading.Event()
    previous = _install_signal_handlers(stop)
    supervisor = (
        ConsumerSupervisor(consumer=consumer, stop_timeout=stop_timeout) if with_etl else None
    )
    thread = threading.Thread(target=server.run, name="fcoffer-api", daemon=True)
    try:
        thread.start()
        if supervisor is not None:
            supervisor.start()
            typer.echo(f"[start-all] ETL consumer ({consumer}) started", err=True)
        typer.echo(f"[start-all] API listening on http://{host}:{port}", err=True)
        while not stop.is_set() and thread.is_alive():
            if supervisor is not None:
                supervisor.poll()
            stop.wait(0.5)
    finally:
        if supervisor is not None:
            supervisor.stop()
        server.should_exit = True
        thread.join(timeout=stop_timeout + 5)
        _restore_signal_handlers(previous)

    if not server.started:
        raise DependencyError(
            "the API server stopped before it started listening",
            details={"host": host, "port": port, "hint": "check that the port is free"},
        )


def register(app: typer.Typer) -> None:
    serve = add_group(app, name="serve", help_text="Run FinanceCoffer services.")

    @serve.command("api", help="Run the REST API server (FastAPI + uvicorn).")
    def api(
        ctx: typer.Context,
        host: str = typer.Option(
            "127.0.0.1", "--host", help="Bind address; only loopback is allowed for now."
        ),
        port: int = typer.Option(8100, "--port", min=1, max=65535, help="Listen port."),
        workers: int = typer.Option(1, "--workers", min=1, max=64, help="Worker processes."),
    ) -> None:
        options = from_context(ctx)
        _reject_non_loopback(options, host)
        try:
            config = load_config(overrides={"db_url": options.db_url})
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        try:
            import uvicorn

            from financecoffer.api import create_app
        except ImportError:
            emit_failure(
                options,
                DependencyError(
                    "the REST API extras are not installed",
                    details={"hint": "install financecoffer[api]"},
                ),
            )

        log_level = config.log_level.lower()
        if workers > 1:
            # uvicorn needs an import string to fork workers; each worker reads
            # the same FC_* environment, so the CLI override is exported first.
            if options.db_url is not None:
                os.environ["FC_DB_URL"] = options.db_url
            uvicorn.run(
                "financecoffer.api.app:create_app",
                factory=True,
                host=host,
                port=port,
                workers=workers,
                log_level=log_level,
            )
        else:
            uvicorn.run(create_app(config=config), host=host, port=port, log_level=log_level)

    @serve.command("admin", help="Run the read-only status page.")
    def admin(
        ctx: typer.Context,
        host: str = typer.Option(
            "127.0.0.1", "--host", help="Bind address; only loopback is allowed for now."
        ),
        port: int = typer.Option(8788, "--port", min=1, max=65535, help="Listen port."),
    ) -> None:
        options = from_context(ctx)
        _reject_non_loopback(options, host)
        try:
            config = load_config(overrides={"db_url": options.db_url})
            dsn = options.db_url or config.db_url
            admin_app = build_admin_app(
                lambda: admin_snapshot(config, dsn=dsn, timeout=options.timeout)
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        try:
            import uvicorn
        except ImportError:
            emit_failure(
                options,
                DependencyError(
                    "the admin extras are not installed",
                    details={"hint": "install financecoffer[admin]"},
                ),
            )
        uvicorn.run(admin_app, host=host, port=port, log_level=config.log_level.lower())

    @app.command("start-all", help="Run the API and the ETL consumer together.")
    def start_all(
        ctx: typer.Context,
        host: str = typer.Option(
            "127.0.0.1", "--host", help="Bind address; only loopback is allowed for now."
        ),
        port: int = typer.Option(8100, "--port", min=1, max=65535, help="Listen port."),
        consumer: str = typer.Option(
            "redis", "--consumer", help="ETL consumer: " + " | ".join(_CONSUMERS) + "."
        ),
        no_etl: bool = typer.Option(
            False, "--no-etl", help="Run the API only, without the ETL child process."
        ),
    ) -> None:
        options = from_context(ctx)
        _reject_non_loopback(options, host)
        if consumer not in _CONSUMERS:
            emit_failure(
                options,
                InvalidArgumentError(
                    "consumer must be one of: " + ", ".join(_CONSUMERS),
                    details={"field": "consumer", "value": consumer, "valid": list(_CONSUMERS)},
                ),
            )
        try:
            config = load_config(overrides={"db_url": options.db_url})
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        if config.db_url is None and options.db_url is None:
            emit_failure(
                options,
                ConfigError(
                    "database URL is not configured",
                    details={"hint": "set FC_DB_URL or pass --db-url"},
                ),
            )

        try:
            _serve_all(
                config=config,
                host=host,
                port=port,
                consumer=consumer,
                with_etl=not no_etl,
            )
        except FinanceCofferError as exc:
            emit_failure(options, exc)


__all__ = [
    "ConsumerSupervisor",
    "admin_snapshot",
    "build_admin_app",
    "register",
    "render_admin_page",
]
