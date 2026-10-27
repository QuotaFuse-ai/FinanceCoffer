"""Tests for S-06-1 ``fcoffer config show`` / ``config check`` redaction."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from financecoffer.cli import app

RUNNER = CliRunner()

DSN = "postgresql://fcoffer:sup3r-s3cret@127.0.0.1:5432/financecoffer"
SECRETS = {
    "FC_DB_URL": DSN,
    "FC_MINIO_ACCESS_KEY": "minio-access-key",
    "FC_MINIO_SECRET_KEY": "minio-s3cret",
    "FC_EMBEDDING_API_KEY": "sk-live-s3cret",
}


def _configure(monkeypatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)


def test_config_show_text_output_hides_every_secret(monkeypatch) -> None:
    _configure(monkeypatch)

    result = RUNNER.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    text = result.stdout
    for value in ("sup3r-s3cret", "minio-access-key", "minio-s3cret", "sk-live-s3cret"):
        assert value not in text
    assert "db_url: postgresql://fcoffer:***@" in text
    assert "minio_access_key: ***" in text
    assert "minio_secret_key: ***" in text
    assert "embedding_api_key: ***" in text


def test_config_show_json_output_hides_every_secret(monkeypatch) -> None:
    _configure(monkeypatch)

    result = RUNNER.invoke(app, ["config", "show", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["meta"]["source"] == "config"
    shown = payload["data"]
    assert shown["db_url"] == "postgresql://fcoffer:***@127.0.0.1:5432/financecoffer"
    assert shown["minio_access_key"] == "***"
    assert shown["minio_secret_key"] == "***"
    assert shown["embedding_api_key"] == "***"
    for value in ("sup3r-s3cret", "minio-access-key", "minio-s3cret", "sk-live-s3cret"):
        assert value not in result.stdout


def test_config_show_redacts_a_dsn_passed_on_the_command_line(monkeypatch) -> None:
    monkeypatch.delenv("FC_DB_URL", raising=False)

    result = RUNNER.invoke(app, ["--db-url", DSN, "config", "show"])

    assert result.exit_code == 0
    assert "sup3r-s3cret" not in result.stdout
    assert "postgresql://fcoffer:***@" in result.stdout


def test_config_show_verbose_logs_do_not_echo_secrets(monkeypatch) -> None:
    _configure(monkeypatch)

    result = RUNNER.invoke(app, ["--verbose", "config", "show"])

    assert result.exit_code == 0
    assert "sup3r-s3cret" not in result.stdout
    assert "sup3r-s3cret" not in (result.stderr or "")


def test_config_show_accepts_the_specs_redact_flag(monkeypatch) -> None:
    _configure(monkeypatch)

    result = RUNNER.invoke(app, ["config", "show", "--redact"])

    assert result.exit_code == 0
    assert "sup3r-s3cret" not in result.stdout


def test_config_check_reports_missing_credentials_as_warnings(monkeypatch) -> None:
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)

    result = RUNNER.invoke(app, ["config", "check"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["valid"] is True
    assert payload["data"]["db_url_configured"] is False
    assert payload["data"]["minio_configured"] is False
    assert any("FC_DB_URL" in warning for warning in payload["data"]["warnings"])


def test_config_check_never_echoes_secrets(monkeypatch) -> None:
    _configure(monkeypatch)

    result = RUNNER.invoke(app, ["config", "check"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["db_url_configured"] is True
    assert payload["data"]["minio_configured"] is True
    assert payload["data"]["warnings"] == []
    assert "sup3r-s3cret" not in result.stdout
