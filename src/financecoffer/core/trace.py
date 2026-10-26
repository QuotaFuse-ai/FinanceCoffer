"""Trace identifiers shared by the CLI, REST API and MCP layers."""

from __future__ import annotations

import re
import uuid

from financecoffer.core.errors import InvalidArgumentError

_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def new_trace_id() -> str:
    """Generate a 32-character hexadecimal trace id."""

    return uuid.uuid4().hex


def normalize_trace_id(value: str | None) -> str:
    """Validate a caller-supplied trace id, or generate a fresh one."""

    if value is None or not value.strip():
        return new_trace_id()
    cleaned = value.strip()
    if not _TRACE_ID_PATTERN.fullmatch(cleaned):
        raise InvalidArgumentError(
            "trace id must be 1-128 characters of letters, digits, '.', '_', ':' or '-'"
        )
    return cleaned
