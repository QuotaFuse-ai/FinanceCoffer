"""Structured logging to stderr with mandatory secret redaction.

Logs are JSON lines by default. The level comes from ``FC_LOG_LEVEL`` unless an
explicit level is passed. ``trace_id`` can be attached per record via
``logger.info("...", extra={"trace_id": ...})``.

Never log config objects directly: :class:`Config.redacted_dict` exists for
that. This module additionally scrubs URL credentials and secret-looking keys
as a second line of defence.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

from financecoffer.core.redaction import redact_text, redact_value

LOGGER_NAME = "financecoffer"
LOG_LEVEL_ENV = "FC_LOG_LEVEL"

# Attributes present on every ``logging.LogRecord``; everything else is an
# explicit ``extra`` supplied by the caller.
_RESERVED_RECORD_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS or key.startswith("_"):
                continue
            payload[key] = redact_value(key, value)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable fallback used when JSON output is explicitly disabled."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def resolve_level(level: str | int | None) -> int:
    """Resolve a level name, falling back to the env var and then ``INFO``."""

    candidate: str | int | None = level
    if candidate is None:
        candidate = os.environ.get(LOG_LEVEL_ENV, "INFO")
    if isinstance(candidate, int):
        return candidate
    resolved = logging.getLevelName(str(candidate).strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def configure_logging(
    level: str | int | None = None,
    *,
    stream: TextIO | None = None,
    json_output: bool = True,
) -> None:
    """Install a single stderr handler on the root logger.

    Idempotent: calling it again replaces the previous handler, which keeps test
    suites and embedders from duplicating records.
    """

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolve_level(level))


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a namespaced logger (``financecoffer.<name>``)."""

    if not name:
        return logging.getLogger(LOGGER_NAME)
    if name == LOGGER_NAME or name.startswith(f"{LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
