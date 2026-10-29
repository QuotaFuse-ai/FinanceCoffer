#!/usr/bin/env python3
"""Start, stop and inspect the local FinanceCoffer API process.

The thin ``start-local`` / ``stop-local`` / ``status-local`` wrappers call this
module so the behaviour is identical on Windows and POSIX. Only the interpreter
from ``sys.executable`` (set by the wrapper) is used; no virtualenv is created.

State is kept outside the repository, under ``FC_STATE_DIR`` (defaults to
``%LOCALAPPDATA%\\FinanceCoffer`` on Windows and
``${XDG_STATE_HOME:-~/.local/state}/financecoffer`` elsewhere). That directory
holds ``run/api.json`` plus ``run/api.pid`` and the service log.

The stop command only terminates a PID recorded by this launcher after checking
that the running command line still belongs to the API process; processes the
launcher did not start are never touched.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "api"
DEFAULT_PORT = 8100
HEALTH_TIMEOUT = 30.0
STOP_TIMEOUT = 10.0


def state_root() -> Path:
    override = os.environ.get("FC_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "FinanceCoffer"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "financecoffer"


RUN_DIR = state_root() / "run"
LOG_DIR = state_root() / "logs"
PID_FILE = RUN_DIR / f"{SERVICE}.pid"
RUN_FILE = RUN_DIR / f"{SERVICE}.json"
LOG_FILE = LOG_DIR / f"{SERVICE}.log"


def _load_dotenv() -> None:
    """Load repository .env values that the process environment does not set."""

    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _port() -> int:
    raw = os.environ.get("API_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"ERROR: API_PORT is not an integer: {raw!r}") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"ERROR: API_PORT out of range: {port}")
    return port


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _health(port: int, timeout: float = 2.0) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200, response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc)


def _wait_healthy(port: int, process: subprocess.Popen[bytes]) -> tuple[bool, str]:
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last = ""
    while time.monotonic() < deadline:
        ok, last = _health(port)
        if ok:
            return True, last
        if process.poll() is not None:
            break
        time.sleep(1.0)
    return False, last


def _read_run_meta() -> dict[str, Any] | None:
    try:
        return json.loads(RUN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _windows_command_line(pid: int) -> str | None:
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = result.stdout.strip()
    return line or None


def _posix_command_line(pid: int) -> str | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return ""
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.is_file():
        try:
            return proc.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            return ""
    return ""


def _command_line(pid: int) -> str | None:
    if os.name == "nt":
        return _windows_command_line(pid)
    return _posix_command_line(pid)


def _is_ours(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    line = _command_line(pid)
    if line is None:
        return False
    if not line:
        # Alive but /proc is unavailable (non-Linux POSIX); trust our own record.
        return os.name != "nt"
    lowered = line.lower()
    return "financecoffer" in lowered and "serve" in lowered and "api" in lowered


def _tail_log(lines: int = 20) -> str:
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log file)"
    return "\n".join(content[-lines:])


def start() -> int:
    port = _port()
    if _port_open(port):
        ok, body = _health(port)
        if ok:
            print(f"Port {port} is already served by a healthy FinanceCoffer API; reusing it.")
            print(f"Health: http://127.0.0.1:{port}/health")
            return 0
        print(f"ERROR: port {port} is in use but /health did not answer.")
        print("Stop the process that owns the port or set API_PORT to a free port.")
        return 1

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    src = str(ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    env["PYTHONUNBUFFERED"] = "1"

    command = [
        sys.executable,
        "-m",
        "financecoffer",
        "serve",
        "api",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]

    log_handle = LOG_FILE.open("ab")
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": log_handle,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(command, **kwargs)
    finally:
        log_handle.close()

    meta = {
        "service": SERVICE,
        "pid": process.pid,
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "command": command,
        "log": str(LOG_FILE),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "owner": "scripts/local_runner.py",
    }
    PID_FILE.write_text(f"{process.pid}\n", encoding="utf-8")
    RUN_FILE.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    ok, body = _wait_healthy(port, process)
    if not ok:
        print(f"ERROR: API did not become healthy on port {port} within {HEALTH_TIMEOUT:.0f}s.")
        print("Log tail:")
        print(_tail_log())
        _terminate(process.pid)
        _cleanup()
        return 1

    print(f"API started on http://127.0.0.1:{port} (pid {process.pid}).")
    print(f"Log: {LOG_FILE}")
    print(f"Health: {body}")
    return 0


def stop() -> int:
    meta = _read_run_meta()
    if meta is None:
        print("No API run record found; nothing started by this launcher to stop.")
        return 0
    pid = meta.get("pid")
    if not _is_ours(pid):
        print(f"PID {pid} from the run record is not a live FinanceCoffer API process.")
        print("Leaving it untouched and clearing the stale run record.")
        _cleanup()
        return 0

    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"ERROR: taskkill failed: {result.stderr.strip() or result.stdout.strip()}")
            return 1
    else:
        _terminate(pid)

    _cleanup()
    print(f"API stopped (pid {pid}).")
    return 0


def _terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
        return
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return
    time.sleep(0.2)


def _cleanup() -> None:
    for path in (PID_FILE, RUN_FILE):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def status() -> int:
    port = _port()
    meta = _read_run_meta()
    pid = meta.get("pid") if meta else None
    ours = _is_ours(pid)
    healthy, body = _health(port)

    if ours:
        print(f"API: running (pid {pid}), started by this launcher.")
    elif _port_open(port):
        print(f"API: port {port} is served by a process this launcher did not start.")
    else:
        print("API: not running.")

    if meta:
        print(f"  url:  {meta.get('url')}")
        print(f"  log:  {meta.get('log')}")
        print(f"  pid file: {PID_FILE}")
    print(f"  health: {'ok' if healthy else 'unavailable'} at http://127.0.0.1:{port}/health")
    if healthy:
        print(f"  body: {body}")
    return 0 if healthy else 1


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"start", "stop", "status"}:
        print("usage: local_runner.py start|stop|status", file=sys.stderr)
        return 2
    if sys.version_info < (3, 11):  # noqa: UP036 - clear message for unsupported interpreters
        print("ERROR: Python 3.11 or newer is required.", file=sys.stderr)
        return 2
    _load_dotenv()
    if argv[1] == "start":
        return start()
    if argv[1] == "stop":
        return stop()
    return status()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
