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

import hashlib
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


def _child_environment(extra_path: Path | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("FC_")}
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(extra_path) if extra_path else "", str(SRC), env.get("PYTHONPATH", "")])
    )
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


# -- QUOT-195: a fork carries references, never secrets ----------------------


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SITE_CUSTOMIZE = '''"""Record a digest of this process's ``FC_*`` environment, then step aside.

``sitecustomize`` is imported by every interpreter whose ``PYTHONPATH`` contains the
directory this test puts it in: the CLI parent and each uvicorn worker it spawns.
Only digests are recorded, because the claim under test is that a credential is
*absent* from a child environment -- writing the values out to prove they are
absent would itself be the leak.
"""

import hashlib
import json
import os
from pathlib import Path

target = os.environ.get("ENV_DIGEST_DIR")
if target:
    directory = Path(target)
    directory.mkdir(parents=True, exist_ok=True)
    table = {
        name: hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
        for name, value in os.environ.items()
        if name.startswith("FC_")
    }
    (directory / f"env-{os.getpid()}.json").write_text(
        json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "env": table}, sort_keys=True),
        encoding="utf-8",
    )
'''


def _install_env_dumper(support: Path, dump_dir: Path) -> None:
    """Put a ``sitecustomize`` on the child ``PYTHONPATH`` that digests ``FC_*``."""

    support.mkdir(parents=True, exist_ok=True)
    (support / "sitecustomize.py").write_text(_SITE_CUSTOMIZE, encoding="utf-8")
    dump_dir.mkdir(parents=True, exist_ok=True)


def _move_into_files(pairs: dict[str, str], holder: Path) -> dict[str, tuple[str, str]]:
    """Rewrite each configured credential as a ``file:`` reference.

    Returns ``{env name: (reference, plaintext)}`` for the fields that moved, which
    is what the child-environment assertions are made of. ``file:`` is the scheme a
    forked child can resolve on its own: ``keyring:`` would be read from this
    machine's credential store, which no test may do, and ``env:`` keeps the secret
    in the environment -- the exact thing being proven absent.
    """

    moved: dict[str, tuple[str, str]] = {}
    for name in ("FC_DB_URL", "FC_MINIO_ACCESS_KEY", "FC_MINIO_SECRET_KEY"):
        literal = pairs.get(name)
        if not literal:
            continue
        path = holder / f"{name.removeprefix('FC_').lower()}.txt"
        path.write_text(literal, encoding="utf-8")
        reference = f"file:{path.as_posix()}"
        pairs[name] = reference
        moved[name] = (reference, literal)
    return moved


def test_workers_inherit_references_and_no_plaintext(
    migrated_warehouse: str, tmp_path: Path
) -> None:
    """Three things must hold at once, and each is the control for the others.

    The workers serve with a healthy database check, so a reference reached them and
    resolved. Their recorded environment carries the *reference* digest, so the fork
    published what the operator wrote. And no recorded environment carries a
    plaintext digest, so the secret itself never crossed -- the whole point of
    QUOT-195.

    The aggregate ``/health`` status is deliberately not asserted: an embedding
    service this machine is not running would degrade it for reasons that have
    nothing to do with credential transport, and this case is about transport.
    """

    pairs = _dotenv_pairs()
    pairs["FC_DB_URL"] = migrated_warehouse
    holder = tmp_path / "secrets"
    holder.mkdir()
    moved = _move_into_files(pairs, holder)
    assert moved, "no credential is configured locally, so there is nothing to prove here"

    dump_dir = tmp_path / "envdumps"
    support = tmp_path / "support"
    _install_env_dumper(support, dump_dir)

    (tmp_path / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in pairs.items()), encoding="utf-8"
    )

    child_env = _child_environment(support)
    child_env["ENV_DIGEST_DIR"] = dump_dir.as_posix()
    assert not any(key.startswith("FC_") for key in child_env)

    port = _free_port()
    log_path = tmp_path / "serve.log"
    process = _start(port, 2, child_env, tmp_path, log_path)
    try:
        data = _wait_until_serving(port, process, log_path)
        checks = data.get("checks") or {}
        assert (checks.get("database") or {}).get("status") == "ok", checks

        status, body = _get(
            f"http://127.0.0.1:{port}/v1/structured/{SYMBOL}", timeout=QUERY_TIMEOUT
        )
        assert (body.get("error") or {}).get("code") != "E_CONFIG", (status, body)

        dumps = [
            json.loads(path.read_text(encoding="utf-8")) for path in dump_dir.glob("env-*.json")
        ]
        assert dumps, "no process recorded its environment; the probe never ran"

        plaintexts = {_digest(value) for _, value in moved.values()}
        for record in dumps:
            leaked = sorted(set(record["env"].values()) & plaintexts)
            assert not leaked, f"pid {record['pid']} carries a plaintext credential"

        carrying = [
            record
            for record in dumps
            if all(
                record["env"].get(name) == _digest(reference)
                for name, (reference, _) in moved.items()
            )
        ]
        # Two workers, each with the reference in its own environment block. The
        # parent may appear too (it exported them), so this is a floor, not a count.
        assert len(carrying) >= 2, f"{len(carrying)} of {len(dumps)} processes carry the references"
    finally:
        _stop(process)
