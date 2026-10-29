"""``fcoffer minio`` command wiring (DEC-0028).

The command module's configuration loader is replaced, so these tests do not
depend on the machine's ``.env`` or on a running store.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from financecoffer.cli import app
from financecoffer.cli.commands import objstore
from financecoffer.core.config import Config

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _patch_config(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    monkeypatch.setattr(objstore, "load_cli_config", lambda **kwargs: config)


def test_help_lists_the_minio_group() -> None:
    result = _invoke("--help")

    assert result.exit_code == 0
    assert "minio" in result.stdout


def test_status_reports_a_stopped_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, Config(minio_endpoint="127.0.0.1:1"))

    result = _invoke("minio", "status")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["running"] is False
    assert payload["data"]["endpoint"] == "127.0.0.1:1"


def test_ensure_reports_skipped_when_autostart_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, Config(minio_endpoint="127.0.0.1:1", minio_autostart=False))

    result = _invoke("minio", "ensure")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["action"] == "skipped"


def test_start_without_a_binary_fails_as_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, Config(minio_endpoint="127.0.0.1:1"))

    result = _invoke("minio", "start")

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_CONFIG"
    assert payload["error"]["details"]["field"] == "minio_binary"


def test_stop_without_a_pid_file_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, Config(minio_endpoint="127.0.0.1:1"))

    result = _invoke("minio", "stop")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["stopped"] is False
