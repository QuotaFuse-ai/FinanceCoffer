"""Shared redaction helpers for logs, config dumps and error details."""

from __future__ import annotations

import re
from typing import Any

from pydantic import SecretStr

REDACTED = "***"

# Substrings that mark a mapping key as sensitive. Matching is case-insensitive.
SECRET_KEY_MARKERS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "access_key",
    "authorization",
    "credential",
)

_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^:/@\s]+):(?P<password>[^@/\s]+)@"
)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|token|password|passwd|secret|access[_-]?key|secret[_-]?key)"
    r"(\s*[=:]\s*)([^\s,;'\"]+)"
)


def is_secret_key(key: object) -> bool:
    """Return True when a mapping key looks like it holds a credential."""

    name = str(key).lower()
    return any(marker in name for marker in SECRET_KEY_MARKERS)


def redact_text(text: str) -> str:
    """Redact URL credentials and ``key=value`` style secrets in free text."""

    redacted = _URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:***@", text)
    return _KEY_VALUE_SECRET.sub(r"\1\2***", redacted)


def redact_value(key: object, value: Any) -> Any:
    """Return a value that is safe to serialize into logs or config dumps."""

    if isinstance(value, SecretStr):
        return REDACTED
    if is_secret_key(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    return value
