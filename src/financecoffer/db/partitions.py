"""ODS monthly partition helpers.

The DDL creates ``ods.stock_raw_data`` as ``PARTITION BY RANGE (fetch_time)``
plus the ``ods.ensure_partition`` / ``ods.ensure_partitions`` SQL functions.
This module is the Python face of those functions so the CLI can pre-create the
current month and ``--ahead`` months without waiting for a failed insert.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from financecoffer.core.errors import InvalidArgumentError
from financecoffer.db.connection import connect

PARTITIONED_TABLES: tuple[str, ...] = ("ods.stock_raw_data",)

_MONTH_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})$")


def parse_month(value: str) -> date:
    """Parse ``YYYY-MM`` into the first day of that month."""

    match = _MONTH_PATTERN.match(value.strip())
    if match is None:
        raise InvalidArgumentError(
            "--month must use YYYY-MM",
            details={"value": value},
        )
    year = int(match.group("year"))
    month = int(match.group("month"))
    if not 1 <= month <= 12:
        raise InvalidArgumentError(
            "--month must use a month between 01 and 12",
            details={"value": value},
        )
    return date(year, month, 1)


def partition_name(month: date) -> str:
    return f"stock_raw_data_{month:%Y%m}"


def _split_table(table: str) -> tuple[str, str]:
    if table not in PARTITIONED_TABLES:
        raise InvalidArgumentError(
            "unsupported partitioned table",
            details={"table": table, "supported": list(PARTITIONED_TABLES)},
        )
    schema, _, name = table.partition(".")
    return schema, name


def list_partitions(conn: Any, table: str = "ods.stock_raw_data") -> list[dict[str, Any]]:
    """List child partitions with their range bounds and on-disk size."""

    schema, name = _split_table(table)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT child.relname,
                   pg_get_expr(child.relpartbound, child.oid) AS bounds,
                   pg_total_relation_size(child.oid) AS size_bytes
              FROM pg_inherits AS i
              JOIN pg_class AS parent ON parent.oid = i.inhparent
              JOIN pg_namespace AS ns ON ns.oid = parent.relnamespace
              JOIN pg_class AS child ON child.oid = i.inhrelid
             WHERE ns.nspname = %s AND parent.relname = %s
             ORDER BY child.relname
            """,
            (schema, name),
        )
        rows = cur.fetchall()
    return [
        {"partition": str(row[0]), "bounds": str(row[1]), "size_bytes": int(row[2])}
        for row in rows
    ]


def _exists(conn: Any, schema: str, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{schema}.{name}",))
        row = cur.fetchone()
    return bool(row and row[0])


def ensure_partition(
    conn: Any, month: date, *, table: str = "ods.stock_raw_data"
) -> dict[str, Any]:
    """Create one monthly partition if missing; return name plus ``created``."""

    schema, _ = _split_table(table)
    name = partition_name(month)
    existed = _exists(conn, schema, name)
    with conn.cursor() as cur:
        cur.execute("SELECT ods.ensure_partition(%s::date)", (month,))
        row = cur.fetchone()
    return {"partition": str(row[0]) if row else name, "created": not existed}


def ensure_partitions(
    conn: Any,
    *,
    start_month: date | None = None,
    ahead: int = 3,
    table: str = "ods.stock_raw_data",
) -> list[dict[str, Any]]:
    """Ensure partitions for ``start_month`` through ``start_month + ahead``."""

    if ahead < 0:
        raise InvalidArgumentError("--ahead must be >= 0", details={"ahead": ahead})
    start = date((start_month or date.today()).year, (start_month or date.today()).month, 1)
    results: list[dict[str, Any]] = []
    year, month = start.year, start.month
    for _ in range(ahead + 1):
        results.append(ensure_partition(conn, date(year, month, 1), table=table))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return results


def create_partitions(
    dsn: str,
    *,
    start_month: date | None = None,
    ahead: int = 3,
    table: str = "ods.stock_raw_data",
    timeout: int = 10,
) -> list[dict[str, Any]]:
    """Open a connection and ensure partitions for the requested window."""

    with connect(dsn, timeout=timeout) as conn:
        return ensure_partitions(conn, start_month=start_month, ahead=ahead, table=table)
