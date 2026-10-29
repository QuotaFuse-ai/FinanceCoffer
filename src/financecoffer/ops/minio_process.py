"""Local MinIO process lifecycle: probe, start, stop, ensure (DEC-0028).

DEC-0014 put the object store on the host as a plain binary because no
container image could be pulled, and left starting it to a hand-run script.
That works until the machine reboots: every archive write then fails until
someone remembers. This module closes the gap with one switch and four verbs:

* ``FC_MINIO_AUTOSTART=true`` makes a launch ensure the store is up;
* ``FC_MINIO_BINARY`` and ``FC_MINIO_DATA_DIR`` name the executable and its
  data directory, both outside the repository (the binary is ~112 MB);
* ``FC_MINIO_LOG_FILE`` optionally appends the server's own output;
* ``FC_MINIO_CONSOLE_ENDPOINT`` defaults to the API port plus one, which is the
  pairing the original host setup used (``9100`` -> ``9101``).

The rules the decisions set stay in force: the listen address must be loopback,
credentials come from the already-loaded :class:`Config` (never from a second
file), and no code path here prints a secret.

Two deliberate asymmetries with the rest of the CLI:

* ``start`` is strict -- it raises when the binary is missing or the server
  never turns healthy -- while the hook inside ``serve api`` is best-effort and
  only warns, because an absent object store degrades the archive and never the
  read path (``fcoffer health`` reports the same verdict without failing).
* ``stop`` identifies the process before killing it, using the image name
  recorded in the pid file, so a recycled pid cannot take down an unrelated
  process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from financecoffer.core.config import Config
from financecoffer.core.errors import ConfigError, DependencyError

PROBE_PATH = "/minio/health/live"
PROBE_TIMEOUT = 1.0
START_WAIT_SECONDS = 20.0
STOP_WAIT_SECONDS = 5.0
POLL_INTERVAL = 0.2
PID_FILENAME = "minio.pid"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


# -- endpoint helpers -------------------------------------------------------


def base_url(endpoint: str) -> str:
    """Normalise ``host:port`` into an ``http://`` URL."""

    cleaned = (endpoint or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"http://{cleaned}"
    return cleaned


def split_endpoint(endpoint: str) -> tuple[str, int | None]:
    """Return ``(host, port)`` for an endpoint, tolerating a missing port."""

    parts = urlsplit(base_url(endpoint))
    try:
        port = parts.port
    except ValueError:
        port = None
    return (parts.hostname or ""), port


def is_loopback(endpoint: str) -> bool:
    """True when the endpoint names a loopback host."""

    return split_endpoint(endpoint)[0] in _LOOPBACK_HOSTS


def address_arg(endpoint: str) -> str:
    """``host:port`` as MinIO's ``--address`` expects it (no scheme)."""

    host, port = split_endpoint(endpoint)
    return f"{host}:{port}" if port is not None else host


def console_endpoint(config: Config) -> str:
    """The console listen address: configured value, else API port plus one."""

    configured = (config.minio_console_endpoint or "").strip()
    if configured:
        return address_arg(configured)
    host, port = split_endpoint(config.minio_endpoint)
    if port is None:
        return host
    return f"{host}:{port + 1}"


# -- probes -----------------------------------------------------------------


def probe(endpoint: str, *, timeout: float = PROBE_TIMEOUT) -> bool:
    """Return True when ``GET /minio/health/live`` answers ``200``.

    Any transport error, timeout or non-200 status is ``False``: the caller
    only needs "is a healthy store reachable at this endpoint".
    """

    url = base_url(endpoint)
    if not url:
        return False
    try:
        with urllib.request.urlopen(f"{url}{PROBE_PATH}", timeout=timeout) as response:
            return int(response.status) == 200
    except Exception:  # noqa: BLE001 - any failure means "not healthy"
        return False


# -- paths and pid file -----------------------------------------------------


def pid_file(config: Config) -> Path:
    """Location of the pid record: inside the data directory, outside the repo."""

    return _data_dir(config) / PID_FILENAME


def read_pid(config: Config) -> dict[str, Any] | None:
    """Read the recorded ``{pid, binary}`` pair, or None when absent/garbled."""

    path = pid_file(config)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    return {"pid": pid, "binary": str(raw.get("binary") or "")}


def _record(config: Config) -> dict[str, Any] | None:
    """``read_pid`` that tolerates a host with no data directory configured."""

    if not (config.minio_data_dir or "").strip():
        return None
    return read_pid(config)


def _write_pid(config: Config, pid: int, binary: Path) -> None:
    path = pid_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "binary": str(binary)}, ensure_ascii=False),
        encoding="utf-8",
    )


def _clear_pid(config: Config) -> None:
    try:
        pid_file(config).unlink()
    except OSError:
        pass


def _binary(config: Config) -> Path:
    raw = (config.minio_binary or "").strip()
    if not raw:
        raise ConfigError(
            "FC_MINIO_BINARY is not configured",
            details={"field": "minio_binary", "hint": "point it at the minio executable"},
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        raise DependencyError(
            "the configured MinIO binary does not exist",
            details={"field": "minio_binary", "path": str(path)},
        )
    return path


def _data_dir(config: Config) -> Path:
    raw = (config.minio_data_dir or "").strip()
    if not raw:
        raise ConfigError(
            "FC_MINIO_DATA_DIR is not configured",
            details={"field": "minio_data_dir", "hint": "point it at the data directory"},
        )
    return Path(raw).expanduser()


def _secret(value: Any) -> str:
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if callable(getter) else str(value)


# -- process identity -------------------------------------------------------


def _parse_tasklist_row(output: str, pid: int) -> str | None:
    """Pick the image name out of ``tasklist /FO CSV /NH`` output.

    A missing pid is not an empty result: Windows prints a localized "no tasks
    are running" notice, which has no comma and no pid column. Treating that
    notice as a process name would make every dead pid look alive.
    """

    lines = [line for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return None
    fields = [field.strip().strip('"') for field in lines[0].split(",")]
    if len(fields) < 2 or fields[1] != str(pid):
        return None
    return fields[0] or None


def _image_name(pid: int) -> str | None:
    """Return the executable name behind ``pid``, or None when it is gone."""

    if os.name == "nt":  # pragma: no cover - exercised on Windows hosts only
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
        return _parse_tasklist_row(completed.stdout or "", pid)
    try:  # pragma: no cover - POSIX fallback
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [part for part in raw.decode("utf-8", "replace").split("\x00") if part]
    return Path(parts[0]).name if parts else None


def owns_process(record: dict[str, Any]) -> bool:
    """True when the recorded pid still runs the recorded binary."""

    expected = Path(record.get("binary") or "").name
    if not expected:
        return False
    actual = _image_name(int(record["pid"]))
    return actual is not None and actual.lower() == expected.lower()


def _terminate(pid: int, *, timeout: float = STOP_WAIT_SECONDS) -> bool:
    """Ask ``pid`` to exit, escalate if it does not, and report the outcome."""

    if os.name == "nt":  # pragma: no cover - Windows only
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
        )
        # taskkill returns before the entry leaves the process table, so poll
        # briefly; reporting "still running" for an already-dead server is worse
        # than waiting a fraction of a second.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _image_name(pid) is None:
                return True
            time.sleep(POLL_INTERVAL)
        return _image_name(pid) is None
    try:  # pragma: no cover - POSIX only
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _image_name(pid) is None:
            return True
        time.sleep(POLL_INTERVAL)
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        return True
    return _image_name(pid) is None


# -- verbs ------------------------------------------------------------------


def state(config: Config) -> dict[str, Any]:
    """Report the endpoint, the switch and what is actually listening."""

    # The pid file lives in the data directory, so a host that never configured
    # one still gets a usable answer: the endpoint is either listening or not.
    record = _record(config)
    running = probe(config.minio_endpoint)
    return {
        "running": running,
        "endpoint": address_arg(config.minio_endpoint),
        "console_endpoint": console_endpoint(config),
        "autostart": bool(config.minio_autostart),
        "pid": record["pid"] if record else None,
        "pid_owned": owns_process(record) if record else False,
        "binary_configured": bool((config.minio_binary or "").strip()),
        "data_dir_configured": bool((config.minio_data_dir or "").strip()),
    }


def start(config: Config, *, wait_seconds: float = START_WAIT_SECONDS) -> dict[str, Any]:
    """Start the store if it is not already answering; wait until it does.

    Idempotent: an endpoint that already answers ``/minio/health/live`` is
    reported as ``already_running`` and nothing is spawned.
    """

    if not is_loopback(config.minio_endpoint):
        raise ConfigError(
            "MinIO may only be started on a loopback address",
            details={"field": "minio_endpoint", "value": config.minio_endpoint},
        )
    endpoint = address_arg(config.minio_endpoint)
    console = console_endpoint(config)
    if probe(config.minio_endpoint):
        record = _record(config)
        return {
            "started": False,
            "already_running": True,
            "running": True,
            "endpoint": endpoint,
            "console_endpoint": console,
            "pid": record["pid"] if record else None,
        }

    binary = _binary(config)
    data_dir = _data_dir(config)
    access = _secret(config.minio_access_key)
    secret = _secret(config.minio_secret_key)
    if not (access and secret):
        raise ConfigError(
            "MinIO credentials are missing",
            details={
                "fields": ["minio_access_key", "minio_secret_key"],
                "hint": "set FC_MINIO_ACCESS_KEY and FC_MINIO_SECRET_KEY in the local .env",
            },
        )

    data_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(binary),
        "server",
        str(data_dir),
        "--address",
        endpoint,
        "--console-address",
        console,
    ]
    environment = dict(os.environ)
    environment["MINIO_ROOT_USER"] = access
    environment["MINIO_ROOT_PASSWORD"] = secret
    log_path = (config.minio_log_file or "").strip()

    process = _spawn(command, environment, log_path)
    _write_pid(config, process.pid, binary)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if probe(config.minio_endpoint):
            return {
                "started": True,
                "already_running": False,
                "running": True,
                "endpoint": endpoint,
                "console_endpoint": console,
                "pid": process.pid,
                "log_file": log_path or None,
            }
        if process.poll() is not None:
            break
        time.sleep(POLL_INTERVAL)

    _terminate(process.pid)
    _clear_pid(config)
    raise DependencyError(
        "MinIO did not become healthy after it was started",
        details={
            "endpoint": endpoint,
            "waited_seconds": wait_seconds,
            "log_file": log_path or None,
            "hint": "check the log file, or start the server by hand to see its output",
        },
    )


def stop(config: Config, *, timeout: float = STOP_WAIT_SECONDS) -> dict[str, Any]:
    """Stop the process recorded in the pid file, if it is still ours."""

    record = _record(config)
    if record is None:
        reason = (
            "no pid file"
            if (config.minio_data_dir or "").strip()
            else "FC_MINIO_DATA_DIR is not configured"
        )
        return {"stopped": False, "running": probe(config.minio_endpoint), "reason": reason}
    if not owns_process(record):
        _clear_pid(config)
        return {
            "stopped": False,
            "running": probe(config.minio_endpoint),
            "reason": "recorded process is gone; stale pid file removed",
        }
    stopped = _terminate(record["pid"], timeout=timeout)
    _clear_pid(config)
    return {"stopped": stopped, "running": probe(config.minio_endpoint), "pid": record["pid"]}


def ensure(config: Config, *, wait_seconds: float = START_WAIT_SECONDS) -> dict[str, Any]:
    """Start the store only when ``minio_autostart`` asks for it.

    Returns ``action`` = ``noop`` (already healthy), ``started`` or ``skipped``.
    ``skipped`` is not an error: autostart is off, which is the default and the
    right answer for a deployment where the store is its own service.
    """

    if probe(config.minio_endpoint):
        record = _record(config)
        return {
            "action": "noop",
            "running": True,
            "endpoint": address_arg(config.minio_endpoint),
            "pid": record["pid"] if record else None,
        }
    if not config.minio_autostart:
        return {
            "action": "skipped",
            "running": False,
            "endpoint": address_arg(config.minio_endpoint),
            "reason": "FC_MINIO_AUTOSTART is not enabled",
        }
    result = start(config, wait_seconds=wait_seconds)
    return {"action": "started", **result}


def _spawn(
    command: list[str], environment: dict[str, str], log_path: str
) -> subprocess.Popen[bytes]:
    """Launch the server detached, so it outlives this process."""

    handle: Any = subprocess.DEVNULL
    opened = None
    if log_path:
        path = Path(log_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        opened = open(path, "ab")  # noqa: SIM115 - handed to the child, closed below
        handle = opened
    kwargs: dict[str, Any] = {
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": handle,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(command, **kwargs)  # noqa: S603 - path comes from config
    finally:
        if opened is not None:
            opened.close()
