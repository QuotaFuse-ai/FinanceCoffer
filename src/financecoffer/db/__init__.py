"""Database layer: connection, forward-only migrations and partition helpers.

Nothing in this package connects to PostgreSQL at import time; call sites pass
an explicit DSN resolved from the unified configuration.
"""

from financecoffer.db.connection import (
    connect,
    describe_dsn,
    normalize_dsn,
    resolve_dsn,
    validate_dsn,
)
from financecoffer.db.migrations import (
    MIGRATIONS_DIR,
    Migration,
    MigrationReport,
    MigrationRunner,
    StatusReport,
    discover_migrations,
)
from financecoffer.db.partitions import (
    PARTITIONED_TABLES,
    create_partitions,
    list_partitions,
    parse_month,
)

__all__ = [
    "MIGRATIONS_DIR",
    "PARTITIONED_TABLES",
    "Migration",
    "MigrationReport",
    "MigrationRunner",
    "StatusReport",
    "connect",
    "create_partitions",
    "describe_dsn",
    "discover_migrations",
    "list_partitions",
    "normalize_dsn",
    "parse_month",
    "resolve_dsn",
    "validate_dsn",
]
