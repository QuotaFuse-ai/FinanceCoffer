"""The local object-store process lifecycle (DEC-0028).

These tests never start a real MinIO: the spawn and the health probe are both
seamed, so the orchestration (validation, waiting, pid bookkeeping, refusal to
kill a stranger) is exercised without a 112 MB binary on the machine.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from financecoffer.core.config import Config
from financecoffer.core.errors import ConfigError, DependencyError
from financecoffer.ops import minio_process as runtime


class _Process:
    """Stand-in for ``subprocess.Popen`` with only the attributes we use."""

    def __init__(self, pid: int, *, exits: bool = False) -> None:
        self.pid = pid
        self._exits = exits
        self.terminated = False

    def poll(self) -> int | None:
        return 1 if self._exits else None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(tmp_path: Path, **overrides: object) -> Config:
    values: dict[str, object] = {
        # A free port, not the real 9100: a host that happens to run the store
        # would otherwise turn every "should refuse" case into "already up".
        "minio_endpoint": f"127.0.0.1:{_free_port()}",
        "minio_access_key": "probe-access",
        "minio_secret_key": "probe-secret",
        "minio_data_dir": str(tmp_path / "data"),
        "minio_binary": str(tmp_path / "minio.exe"),
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


@pytest.fixture()
def fake_binary(tmp_path: Path) -> Path:
    path = tmp_path / "minio.exe"
    path.write_bytes(b"stub")
    return path


# -- endpoint helpers -------------------------------------------------------


def test_console_endpoint_defaults_to_the_api_port_plus_one() -> None:
    config = Config(minio_endpoint="127.0.0.1:9100")

    assert runtime.console_endpoint(config) == "127.0.0.1:9101"


def test_console_endpoint_honours_the_configured_value() -> None:
    config = Config(minio_endpoint="127.0.0.1:9100", minio_console_endpoint="127.0.0.1:9443")

    assert runtime.console_endpoint(config) == "127.0.0.1:9443"


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("127.0.0.1:9100", True),
        ("http://127.0.0.1:9100", True),
        ("localhost:9100", True),
        ("[::1]:9100", True),
        ("0.0.0.0:9100", False),
        ("minio:9000", False),
        ("10.0.0.5:9100", False),
    ],
)
def test_is_loopback(endpoint: str, expected: bool) -> None:
    assert runtime.is_loopback(endpoint) is expected


def test_address_arg_drops_the_scheme() -> None:
    assert runtime.address_arg("http://127.0.0.1:9100/") == "127.0.0.1:9100"


# -- probe ------------------------------------------------------------------


def test_probe_is_false_when_nothing_listens() -> None:
    assert runtime.probe(f"127.0.0.1:{_free_port()}", timeout=0.3) is False


def test_probe_is_true_when_the_health_endpoint_answers_200() -> None:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            self.send_response(200 if self.path == runtime.PROBE_PATH else 404)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert runtime.probe(f"127.0.0.1:{port}", timeout=2.0) is True
    finally:
        server.shutdown()
        server.server_close()


# -- state ------------------------------------------------------------------


def test_state_without_paths_still_answers() -> None:
    config = Config(minio_endpoint=f"127.0.0.1:{_free_port()}")

    state = runtime.state(config)

    assert state["running"] is False
    assert state["pid"] is None
    assert state["binary_configured"] is False
    assert state["data_dir_configured"] is False
    assert state["autostart"] is False


def test_read_pid_ignores_a_garbled_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runtime.pid_file(config).parent.mkdir(parents=True, exist_ok=True)
    runtime.pid_file(config).write_text("{not json", encoding="utf-8")

    assert runtime.read_pid(config) is None


# -- process identity -------------------------------------------------------


def test_tasklist_row_yields_the_image_name() -> None:
    row = '"minio.exe","43944","Console","1","12,345 K"'

    assert runtime._parse_tasklist_row(row, 43944) == "minio.exe"


def test_tasklist_notice_is_not_a_process_name() -> None:
    # Windows answers a missing pid with a localized sentence, not an empty
    # result; reading its first field as an image name would call every dead
    # pid alive.
    notice = "INFO: No tasks are running which match the specified criteria."

    assert runtime._parse_tasklist_row(notice, 43944) is None


def test_tasklist_row_for_another_pid_is_rejected() -> None:
    row = '"minio.exe","43945","Console","1","12,345 K"'

    assert runtime._parse_tasklist_row(row, 43944) is None


def test_empty_tasklist_output_means_gone() -> None:
    assert runtime._parse_tasklist_row("", 43944) is None
    assert runtime._parse_tasklist_row("   \n\n", 43944) is None


# -- start ------------------------------------------------------------------


def test_start_refuses_a_non_loopback_endpoint(tmp_path: Path, fake_binary: Path) -> None:
    config = _config(tmp_path, minio_endpoint="10.0.0.5:9100", minio_binary=str(fake_binary))

    with pytest.raises(ConfigError):
        runtime.start(config)


def test_start_requires_a_binary(tmp_path: Path) -> None:
    config = _config(tmp_path, minio_binary="")

    with pytest.raises(ConfigError):
        runtime.start(config)


def test_start_reports_a_missing_binary_file(tmp_path: Path) -> None:
    config = _config(tmp_path, minio_binary=str(tmp_path / "nowhere.exe"))

    with pytest.raises(DependencyError):
        runtime.start(config)


def test_start_requires_credentials(tmp_path: Path, fake_binary: Path) -> None:
    config = _config(
        tmp_path,
        minio_binary=str(fake_binary),
        minio_access_key=None,
        minio_secret_key=None,
    )

    with pytest.raises(ConfigError):
        runtime.start(config)


def test_start_spawns_detached_and_records_the_pid(
    tmp_path: Path, fake_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    config = _config(
        tmp_path, minio_binary=str(fake_binary), minio_endpoint=f"127.0.0.1:{port}"
    )
    calls: dict[str, object] = {}

    def fake_spawn(
        command: list[str], environment: dict[str, str], log_path: str
    ) -> _Process:
        calls["command"] = command
        calls["environment"] = environment
        calls["log_path"] = log_path
        return _Process(4321)

    answers = iter([False, False, True])
    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(runtime, "probe", lambda *args, **kwargs: next(answers, True))

    result = runtime.start(config, wait_seconds=5)

    assert result["started"] is True
    assert result["pid"] == 4321
    assert result["endpoint"] == f"127.0.0.1:{port}"
    assert result["console_endpoint"] == f"127.0.0.1:{port + 1}"

    command = calls["command"]
    assert isinstance(command, list)
    assert command[1:3] == ["server", str(tmp_path / "data")]
    address = command[command.index("--address") + 1]
    console = command[command.index("--console-address") + 1]
    assert address == f"127.0.0.1:{port}"
    assert console == f"127.0.0.1:{port + 1}"

    environment = calls["environment"]
    assert isinstance(environment, dict)
    assert environment["MINIO_ROOT_USER"] == "probe-access"
    assert environment["MINIO_ROOT_PASSWORD"] == "probe-secret"

    recorded = json.loads(runtime.pid_file(config).read_text(encoding="utf-8"))
    assert recorded["pid"] == 4321
    assert recorded["binary"] == str(fake_binary)


def test_start_is_idempotent_when_the_endpoint_already_answers(
    tmp_path: Path, fake_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, minio_binary=str(fake_binary))
    monkeypatch.setattr(runtime, "probe", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        runtime, "_spawn", lambda *args, **kwargs: pytest.fail("must not spawn")
    )

    result = runtime.start(config)

    assert result["started"] is False
    assert result["already_running"] is True


def test_start_gives_up_and_cleans_the_pid_file(
    tmp_path: Path, fake_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, minio_binary=str(fake_binary))
    monkeypatch.setattr(runtime, "probe", lambda *args, **kwargs: False)
    monkeypatch.setattr(runtime, "_spawn", lambda *args, **kwargs: _Process(99, exits=True))
    monkeypatch.setattr(runtime, "_terminate", lambda *args, **kwargs: True)

    with pytest.raises(DependencyError):
        runtime.start(config, wait_seconds=0.5)

    assert runtime.pid_file(config).exists() is False


# -- ensure -----------------------------------------------------------------


def test_ensure_skips_when_autostart_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(minio_endpoint="127.0.0.1:9100", minio_autostart=False)
    monkeypatch.setattr(runtime, "probe", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        runtime, "start", lambda *args, **kwargs: pytest.fail("must not start")
    )

    result = runtime.ensure(config)

    assert result["action"] == "skipped"
    assert result["running"] is False


def test_ensure_starts_when_autostart_is_on(
    tmp_path: Path, fake_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, minio_binary=str(fake_binary), minio_autostart=True)
    monkeypatch.setattr(
        runtime,
        "start",
        lambda cfg, **kwargs: {"started": True, "running": True, "pid": 7},
    )

    result = runtime.ensure(config)

    assert result["action"] == "started"
    assert result["pid"] == 7


def test_ensure_is_a_noop_when_the_store_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(minio_endpoint="127.0.0.1:9100", minio_autostart=True)
    monkeypatch.setattr(runtime, "probe", lambda *args, **kwargs: True)

    assert runtime.ensure(config)["action"] == "noop"


# -- stop -------------------------------------------------------------------


def test_stop_without_a_pid_file_says_so(tmp_path: Path) -> None:
    config = _config(tmp_path)

    result = runtime.stop(config)

    assert result["stopped"] is False
    assert result["reason"] == "no pid file"


def test_stop_without_a_data_directory_explains_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(minio_endpoint=f"127.0.0.1:{_free_port()}")
    monkeypatch.setattr(runtime, "probe", lambda *args, **kwargs: True)

    result = runtime.stop(config)

    assert result["stopped"] is False
    assert result["running"] is True
    assert result["reason"] == "FC_MINIO_DATA_DIR is not configured"


def test_stop_removes_a_stale_pid_file_without_killing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    runtime.pid_file(config).parent.mkdir(parents=True, exist_ok=True)
    runtime.pid_file(config).write_text(
        json.dumps({"pid": 999_999, "binary": "some-other.exe"}), encoding="utf-8"
    )
    monkeypatch.setattr(runtime, "owns_process", lambda record: False)
    monkeypatch.setattr(
        runtime, "_terminate", lambda *args, **kwargs: pytest.fail("must not kill")
    )

    result = runtime.stop(config)

    assert result["stopped"] is False
    assert "stale" in str(result["reason"])
    assert runtime.pid_file(config).exists() is False
