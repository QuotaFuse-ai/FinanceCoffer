from pathlib import Path

import pytest
from pydantic import ValidationError

from financecoffer.config import (
    ENV_PREFIX,
    Config,
    default_env_file,
    env_var_name,
    load_cli_config,
    load_config,
    loader,
)
from financecoffer.core.errors import ConfigError


def test_defaults_match_spec() -> None:
    config = load_config(env={})

    assert config.embedding_dim == 1024
    assert config.embedding_provider == "local"
    assert config.embedding_model == "BAAI/bge-m3"
    assert config.minio_bucket == "financecoffer"
    assert config.event_consumer == "redis"
    assert config.event_stream == "fc:stockdata:events"
    assert config.event_group == "fc-etl"
    assert config.redis_url == "redis://127.0.0.1:6379/1"
    assert config.log_level == "INFO"
    assert config.cold_after_years == 3
    assert config.compliance_enabled is False
    assert config.db_url is None
    assert config.mcp_token is None


def test_compliance_switch_reads_fc_env_flag() -> None:
    assert load_config(env={"FC_COMPLIANCE_ENABLED": "true"}).compliance_enabled is True
    assert load_config(env={"FC_COMPLIANCE_ENABLED": "false"}).compliance_enabled is False
    assert load_config(env={}).compliance_enabled is False


def test_env_prefix_is_fc() -> None:
    assert ENV_PREFIX == "FC_"
    assert env_var_name("minio_bucket") == "FC_MINIO_BUCKET"
    assert env_var_name("db_url") == "FC_DB_URL"


def test_only_fc_prefixed_known_fields_are_read() -> None:
    config = load_config(
        env={
            "FC_MINIO_BUCKET": "custom-bucket",
            "PATH": "/usr/bin",
            "MINIO_BUCKET": "other",
            "FC_NOT_A_REAL_FIELD": "ignored",
        }
    )

    assert config.minio_bucket == "custom-bucket"


def test_empty_env_value_keeps_default() -> None:
    config = load_config(env={"FC_MINIO_BUCKET": "", "FC_LOG_LEVEL": "  "})

    assert config.minio_bucket == "financecoffer"
    assert config.log_level == "INFO"


def test_precedence_defaults_then_dotenv_then_env_then_overrides(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FC_MINIO_BUCKET=from-dotenv\nFC_LOG_LEVEL=debug\nFC_EVENT_GROUP=from-dotenv-group\n",
        encoding="utf-8",
    )

    config = load_config(
        env_file=env_file,
        env={"FC_MINIO_BUCKET": "from-env", "FC_EVENT_GROUP": "from-env-group"},
        overrides={"minio_bucket": "from-override", "cold_after_years": 5},
    )

    assert config.minio_bucket == "from-override"
    assert config.event_group == "from-env-group"
    assert config.log_level == "DEBUG"
    assert config.cold_after_years == 5


def test_missing_env_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={}, env_file=tmp_path / "missing.env")

    assert excinfo.value.code.value == "E_CONFIG"
    assert "not found" in str(excinfo.value)


def test_unknown_override_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={}, overrides={"bogus": 1})

    assert "bogus" in str(excinfo.value)


def test_invalid_values_are_config_errors_without_leaking_input() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"FC_EMBEDDING_DIM": "abc"})

    error = excinfo.value
    assert error.exit_code == 3
    assert error.details is not None
    assert error.details["errors"][0]["field"] == "embedding_dim"
    assert "abc" not in str(error)
    assert "abc" not in repr(error.details)


def test_db_url_must_be_postgres_and_password_is_not_echoed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"FC_DB_URL": "mysql://user:topsecret@127.0.0.1:3306/db"})

    assert "topsecret" not in repr(excinfo.value.details)


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_config(env={"FC_LOG_LEVEL": "LOUD"})


def test_secrets_are_masked_and_url_credentials_redacted() -> None:
    config = load_config(
        env={
            "FC_DB_URL": "postgresql+psycopg://user:supersecret@127.0.0.1:5432/financecoffer",
            "FC_MINIO_SECRET_KEY": "minio-secret",
            "FC_MCP_TOKEN": "mcp-token",
        }
    )

    assert config.minio_secret_key is not None
    assert config.minio_secret_key.get_secret_value() == "minio-secret"
    assert "minio-secret" not in repr(config)
    assert "mcp-token" not in repr(config)

    shown = config.redacted_dict()
    assert shown["minio_secret_key"] == "***"
    assert shown["mcp_token"] == "***"
    assert "supersecret" not in str(shown["db_url"])
    assert "user:***@" in str(shown["db_url"])


def test_config_is_immutable() -> None:
    config = Config()

    with pytest.raises(ValidationError):
        config.log_level = "DEBUG"  # type: ignore[misc]


# -- CLI config: the repo ``.env`` participates ----------------------------


def test_default_env_file_is_none_when_absent(tmp_path: Path) -> None:
    assert default_env_file(tmp_path / "missing.env") is None


def test_default_env_file_returns_the_path_when_present(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FC_LOG_LEVEL=DEBUG\n", encoding="utf-8")

    assert default_env_file(env_file) == env_file


def test_load_cli_config_reads_the_discovered_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FC_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.setattr(loader, "default_env_file", lambda *_args, **_kwargs: env_file)
    monkeypatch.delenv("FC_LOG_LEVEL", raising=False)

    assert load_cli_config().log_level == "DEBUG"


def test_load_cli_config_lets_db_url_win_over_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FC_DB_URL=postgresql://from:env@127.0.0.1:5432/env\n", encoding="utf-8")
    monkeypatch.setattr(loader, "default_env_file", lambda *_args, **_kwargs: env_file)
    monkeypatch.delenv("FC_DB_URL", raising=False)

    config = load_cli_config(db_url="postgresql://from:flag@127.0.0.1:5432/flag")

    assert config.db_url == "postgresql://from:flag@127.0.0.1:5432/flag"


def test_load_cli_config_does_not_mask_a_malformed_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FC_LOG_LEVEL=LOUD\n", encoding="utf-8")
    monkeypatch.setattr(loader, "default_env_file", lambda *_args, **_kwargs: env_file)
    monkeypatch.delenv("FC_LOG_LEVEL", raising=False)

    with pytest.raises(ConfigError):
        load_cli_config()
