"""Lazy PostgreSQL connection helpers.

The DSN is never read at import time and is never echoed back with credentials:
:func:`describe_dsn` returns only host, port, database and user so ``db status``
can report the target without leaking a password.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

from financecoffer.config.loader import load_config
from financecoffer.core.errors import ConfigError, DependencyError
from financecoffer.core.redaction import redact_text

_DRIVER_PREFIX = re.compile(r"^postgres(?:ql)?\+[a-z0-9_]+://", re.IGNORECASE)


def normalize_dsn(dsn: str) -> str:
    """Strip a SQLAlchemy driver suffix (``postgresql+psycopg://``) for psycopg."""

    return _DRIVER_PREFIX.sub("postgresql://", dsn.strip(), count=1)


def resolve_dsn(override: str | None = None) -> str:
    """Resolve the DSN from the CLI override or the unified ``FC_DB_URL`` config."""

    if override:
        return normalize_dsn(override)
    config = load_config()
    if not config.db_url:
        raise ConfigError(
            "database URL is not configured",
            details={"hint": "set FC_DB_URL or pass --db-url"},
        )
    return normalize_dsn(config.db_url)


def describe_dsn(dsn: str) -> dict[str, Any]:
    """Return connection details safe for logs, envelopes and comments."""

    parsed = urlsplit(dsn)
    return {
        "host": parsed.hostname or "",
        "port": parsed.port or 5432,
        "database": unquote(parsed.path.lstrip("/")),
        "user": unquote(parsed.username) if parsed.username else "",
    }


def _import_psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise DependencyError(
            "psycopg is required for database commands",
            details={"hint": "install financecoffer[db]"},
        ) from exc
    return psycopg


def connect(dsn: str, *, timeout: int = 10) -> Any:
    """Open a psycopg connection, mapping driver failures to ``E_DEPENDENCY``."""

    psycopg = _import_psycopg()
    try:
        return psycopg.connect(
            dsn,
            connect_timeout=timeout,
            application_name="fcoffer",
        )
    except psycopg.OperationalError as exc:
        raise DependencyError(
            "cannot connect to PostgreSQL",
            details={
                "database": describe_dsn(dsn),
                "error": redact_text(str(exc).strip()),
            },
        ) from exc


def server_version(dsn: str, *, timeout: int = 10) -> str:
    """Return the server version string for ``db status``."""

    with connect(dsn, timeout=timeout) as conn, conn.cursor() as cur:
        cur.execute("SHOW server_version")
        row = cur.fetchone()
    return str(row[0]) if row else "unknown"
