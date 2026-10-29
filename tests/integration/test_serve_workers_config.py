"""Integration: ``serve api --workers N`` inherits the configuration the CLI resolved.

Regression for QUOT-179 (S-03-1). The multi-worker branch hands uvicorn an import
string, so every worker re-runs ``create_app()``. Until this fix that default was
``load_config()`` -- process environment only, no repo ``.env`` -- so a deployment
configured *only* through ``.env`` came up with ``database=not_configured`` on
``/health`` and answered 503 / ``E_CONFIG`` to every query, while
``--workers 1`` (which passes the parent's already-built app) worked.

The test drives the real CLI in a real subprocess: every ``FC_*`` variable is
stripped from the child environment and the configuration lives solely in a
temporary ``.env`` that the child finds in its working directory. Without that
isolation the defect cannot be observed, because an ambient ``FC_DB_URL`` would
mask it (the same reason ``tests/conftest.py`` keeps the developer's ``.env``
out of the suite).

``workers`` is parametrized over 1 and 2: both must agree, and ``2`` is the case
that used to fail.
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
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
HEALTH_TIMEOUT = 120.0
QUERY_TIMEOUT = 60.0
SYMBOL = "600000"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _dotenv_pairs() -> dict[str, str]:
    """Configuration the temporary ``.env`` carries.

    The repo ``.env`` is the deployment-shaped baseline (embedding endpoint,
    MinIO, ...); the ambient ``FC_*`` variables win over it, exactly like the
    documented precedence. The DSN is replaced by the caller.
    """

    pairs: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.is_file():
        from dotenv import dotenv_values

        stored = {key: value for key, value in dotenv_values(env_file).items()}
        pairs.update(
            {key: value for key, value in stored.items() if key.startswith("FC_") and value}
        )
    ambient = {key: value for key, value in os.environ.items() if key.startswith("FC_")}
    pairs.update({key: value for key, value in ambient.items() if value.strip()})
    return pairs


def _reachable(url: str) -> bool:
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def _child_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("FC_")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH", "")]))
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _start(
    port: int, workers: int, env: dict[str, str], cwd: Path, log_path: Path
) -> subprocess.Popen:
    kwargs: dict[str, object] = {}
    if os.name == "nt":  # pragma: no cover - platform specific
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:  # pragma: no cover - platform specific
        kwargs["start_new_session"] = True
    log = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "financecoffer",
                "serve",
                "api",
                "--port",
                str(port),
                "--workers",
                str(workers),
            ],
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            **kwargs,
        )
    finally:
        log.close()


def _stop(process: subprocess.Popen) -> None:
    """Stop the CLI process and, with it, only the workers it forked."""

    if process.poll() is not None:
        return
    if os.name == "nt":  # pragma: no cover - platform specific
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=60,
        )
    else:  # pragma: no cover - platform specific
        import signal

        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=20)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        process.kill()
        process.wait(timeout=10)


def _get(url: str, timeout: float) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # non-2xx still carries an envelope
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_until_serving(port: int, process: subprocess.Popen, log_path: Path) -> dict:
    """Wait for the first ``/health`` answer, then report what it said.

    The readiness verdict is asserted by the test, not looped on, so a degraded
    worker fails with the actual check statuses instead of a timeout.
    """

    deadline = time.monotonic() + HEALTH_TIMEOUT
    last = "no response yet"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"serve api exited before answering /health (rc={process.returncode}):\n"
                + log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            )
        try:
            status, body = _get(f"http://127.0.0.1:{port}/health", timeout=10)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
            continue
        if status == 200:
            return body.get("data") or {}
        last = json.dumps(body, ensure_ascii=False)[:600]
        time.sleep(0.5)
    raise AssertionError(
        f"/health never answered in {HEALTH_TIMEOUT:.0f}s; last response: {last}\n"
        + log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    )


@pytest.mark.parametrize("workers", [1, 2])
def test_workers_inherit_the_dotenv_configuration(
    migrated_warehouse: str, tmp_path: Path, workers: int
) -> None:
    pairs = _dotenv_pairs()
    embedding_url = pairs.get("FC_EMBEDDING_URL", "http://127.0.0.1:8080/v1/embeddings")
    if not _reachable(embedding_url):
        pytest.skip(
            "the embedding endpoint /health probes is not reachable "
            f"({urlsplit(embedding_url).netloc}); the aggregate status cannot reach ok here"
        )

    # The scratch warehouse is fresh and migrated, so the structured query runs
    # real SQL without depending on what a developer's database happens to hold.
    pairs["FC_DB_URL"] = migrated_warehouse
    env_file = tmp_path / ".env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in pairs.items()), encoding="utf-8"
    )

    child_env = _child_environment()
    assert "FC_DB_URL" not in child_env  # the defect is invisible with an ambient DSN

    port = _free_port()
    log_path = tmp_path / "serve.log"
    process = _start(port, workers, child_env, tmp_path, log_path)
    try:
        data = _wait_until_serving(port, process, log_path)
        checks = data.get("checks") or {}
        assert (checks.get("database") or {}).get("status") == "ok", checks
        assert data.get("status") == "ok", checks

        # Every worker serves with the same resolved DSN, not just the first.
        statuses = []
        for _ in range(workers * 2):
            status, body = _get(f"http://127.0.0.1:{port}/health", timeout=10)
            assert status == 200
            checks = (body.get("data") or {}).get("checks") or {}
            statuses.append((checks.get("database") or {}).get("status"))
        assert statuses == ["ok"] * (workers * 2), statuses

        status, body = _get(
            f"http://127.0.0.1:{port}/v1/structured/{SYMBOL}", timeout=QUERY_TIMEOUT
        )
        error = body.get("error") or {}
        # The scratch warehouse is migrated but empty, so 404 / ``E_NOT_FOUND``
        # is the healthy answer: the SQL ran and found no row. ``E_CONFIG`` would
        # mean the worker never received a DSN at all.
        assert error.get("code") != "E_CONFIG", (status, body)
        assert status == 200 or error.get("code") == "E_NOT_FOUND", (status, body)
    finally:
        _stop(process)
