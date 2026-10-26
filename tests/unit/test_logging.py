import io
import json
import logging

from financecoffer.core.logging import (
    JsonFormatter,
    configure_logging,
    get_logger,
    resolve_level,
)


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="financecoffer.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_structured_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record("started", trace_id="abc")))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "financecoffer.test"
    assert payload["message"] == "started"
    assert payload["trace_id"] == "abc"
    assert payload["ts"].endswith("Z")


def test_json_formatter_redacts_url_credentials_in_message() -> None:
    payload = json.loads(
        JsonFormatter().format(
            _record("connect postgresql://financecoffer:s3cr3t@127.0.0.1:5432/financecoffer")
        )
    )

    assert "s3cr3t" not in payload["message"]
    assert "***" in payload["message"]


def test_json_formatter_redacts_secret_extra_fields() -> None:
    payload = json.loads(
        JsonFormatter().format(
            _record("config loaded", db_password="s3cr3t", api_key="sk-live-123", symbol="600000")
        )
    )

    assert payload["db_password"] == "***"
    assert payload["api_key"] == "***"
    assert payload["symbol"] == "600000"


def test_resolve_level_reads_env_var(monkeypatch) -> None:
    monkeypatch.setenv("FC_LOG_LEVEL", "debug")
    assert resolve_level(None) == logging.DEBUG

    monkeypatch.setenv("FC_LOG_LEVEL", "not-a-level")
    assert resolve_level(None) == logging.INFO

    assert resolve_level("WARNING") == logging.WARNING


def test_configure_logging_writes_json_to_the_given_stream() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logger = get_logger("etl")
    logger.info("hello %s", "world", extra={"token": "abc123"})

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["logger"] == "financecoffer.etl"
    assert payload["message"] == "hello world"
    assert payload["token"] == "***"


def test_configure_logging_is_idempotent() -> None:
    first, second = io.StringIO(), io.StringIO()
    configure_logging("INFO", stream=first)
    configure_logging("INFO", stream=second)
    get_logger().info("once")

    assert first.getvalue() == ""
    assert second.getvalue().count("\n") == 1
