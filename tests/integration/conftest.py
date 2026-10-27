"""Shared PostgreSQL scratch database for integration tests.

Set ``FC_TEST_DB_URL`` to a maintenance DSN (for example
``.../postgres``). When the server is unreachable the dependent tests skip, so
the suite stays green on a machine without PostgreSQL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from financecoffer.db import MigrationRunner, connect

MAINTENANCE_DSN = os.environ.get(
    "FC_TEST_DB_URL", "postgresql://fcoffer:fcoffer@127.0.0.1:5432/postgres"
)


def _admin_connect():  # type: ignore[no-untyped-def]
    return connect(MAINTENANCE_DSN, timeout=5)


@pytest.fixture(scope="session")
def migrated_warehouse() -> Iterator[str]:
    """Create a scratch database, migrate it to head, drop it afterwards."""

    try:
        admin = _admin_connect()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL is not reachable at {MAINTENANCE_DSN.rsplit('@', 1)[-1]}: {exc}")

    name = f"fcoffer_t003_it_{uuid.uuid4().hex[:10]}"
    try:
        admin.autocommit = True
        admin.execute(f'CREATE DATABASE "{name}"')
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot create a scratch database: {exc}")
    finally:
        admin.close()

    dsn = MAINTENANCE_DSN.rsplit("/", 1)[0] + f"/{name}"
    try:
        MigrationRunner(dsn, timeout=10).apply()
        yield dsn
    finally:
        cleanup = _admin_connect()
        try:
            cleanup.autocommit = True
            cleanup.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            cleanup.close()
