"""End-to-end migration tests against a throwaway PostgreSQL database.

The suite needs a reachable PostgreSQL 16 + pgvector server. It is driven by
``FC_TEST_DB_URL`` (a maintenance database such as ``.../postgres``); when that
server is unreachable every test here skips, so ``pytest`` stays green on a
machine without PostgreSQL. Each run creates and drops its own database, so it
never touches the warehouse configured in ``FC_DB_URL``.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from financecoffer.db import MigrationRunner, connect, discover_migrations

MAINTENANCE_DSN = os.environ.get(
    "FC_TEST_DB_URL", "postgresql://fcoffer:fcoffer@127.0.0.1:5432/postgres"
)
MIGRATIONS = discover_migrations()
HEAD = MIGRATIONS[-1].version
VERSIONS = [migration.version for migration in MIGRATIONS]

SNAPSHOT_INSERT = (
    "INSERT INTO dwd.stock_financial_snapshot "
    "(symbol, report_date, report_type, version, amount_revenue, quality_status, "
    " fetch_time, event_id, provider) "
    "VALUES (%s, '2026-09-30', 'Q3', 1, %s, 'normal', now(), %s, 'eastmoney')"
)

ODS_INSERT = (
    "INSERT INTO ods.stock_raw_data "
    "(event_id, data_type, symbol, source, biz_key, fetch_time, raw_json) "
    "VALUES (%s, 'financial', %s, 'eastmoney', '2026Q3|Q3', now(), '{}'::jsonb) "
    "RETURNING tableoid::regclass::text"
)


def _admin_connect() -> Any:
    return connect(MAINTENANCE_DSN, timeout=5)


@pytest.fixture(scope="module")
def warehouse() -> Any:
    """Create an empty database, migrate it once, drop it afterwards."""

    try:
        admin = _admin_connect()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL is not reachable at {MAINTENANCE_DSN.rsplit('@', 1)[-1]}: {exc}")

    name = f"fcoffer_t002_it_{uuid.uuid4().hex[:10]}"
    try:
        admin.autocommit = True
        admin.execute(f'CREATE DATABASE "{name}"')
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot create a scratch database: {exc}")
    finally:
        admin.close()

    dsn = MAINTENANCE_DSN.rsplit("/", 1)[0] + f"/{name}"
    runner = MigrationRunner(dsn, timeout=10)
    try:
        first = runner.apply()
        yield {"dsn": dsn, "runner": runner, "first": first}
    finally:
        cleanup = _admin_connect()
        try:
            cleanup.autocommit = True
            cleanup.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            cleanup.close()


def test_empty_database_reaches_head(warehouse: dict[str, Any]) -> None:
    first = warehouse["first"]

    assert [item.version for item in first.applied] == VERSIONS
    assert first.from_version == 0
    assert first.to_version == HEAD
    assert first.changed is True


def test_repeated_migrate_is_a_no_op(warehouse: dict[str, Any]) -> None:
    second = warehouse["runner"].apply()

    assert second.applied == ()
    assert second.partitions_created == ()
    assert second.changed is False
    assert second.from_version == HEAD
    assert second.to_version == HEAD


def test_status_reports_no_pending_migrations(warehouse: dict[str, Any]) -> None:
    report = warehouse["runner"].status()

    assert report.current_version == HEAD
    assert report.head_version == HEAD
    assert report.pending == ()
    assert report.database["database"].startswith("fcoffer_t002_it_")
    assert set(report.database) == {"host", "port", "database", "user"}


def test_catalog_matches_the_spec(warehouse: dict[str, Any]) -> None:
    with connect(warehouse["dsn"]) as conn:
        relkind = conn.execute(
            "SELECT relkind FROM pg_class WHERE oid = 'ods.stock_raw_data'::regclass"
        ).fetchone()[0]
        assert relkind == "p"

        partitions = conn.execute(
            "SELECT count(*) FROM pg_inherits WHERE inhparent = 'ods.stock_raw_data'::regclass"
        ).fetchone()[0]
        assert partitions >= 4

        version_column = conn.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'dwd' AND table_name = 'stock_financial_snapshot' "
            "AND column_name = 'version'"
        ).fetchone()
        assert version_column == ("NO",)

        embedding = conn.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'vector.announcement_chunk'::regclass AND attname = 'embedding'"
        ).fetchone()[0]
        assert embedding == "vector(1024)"

        hnsw = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'vector' AND indexname = 'idx_ann_chunk_embedding'"
        ).fetchone()[0]
        assert "USING hnsw" in hnsw
        assert "vector_cosine_ops" in hnsw

        ops_tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'ops'"
            )
        }
        assert {
            "source_heartbeat",
            "etl_event_log",
            "etl_consumer_offset",
            "etl_dlq",
            "archive_job",
            "schema_version",
        } <= ops_tables


def test_history_rows_are_append_only(warehouse: dict[str, Any]) -> None:
    with pytest.raises(Exception, match="append-only"):
        with connect(warehouse["dsn"]) as conn:
            conn.execute(SNAPSHOT_INSERT, ("600000", 123.4567, "evt_a"))
            conn.execute(
                "UPDATE dwd.stock_financial_snapshot "
                "SET amount_revenue = 999 WHERE symbol = '600000'"
            )


def test_archive_columns_may_still_be_updated(warehouse: dict[str, Any]) -> None:
    with connect(warehouse["dsn"]) as conn:
        conn.execute(SNAPSHOT_INSERT, ("600001", 1.0000, "evt_b"))
        changed = conn.execute(
            "UPDATE dwd.stock_financial_snapshot "
            "SET is_cold = TRUE, minio_url = 'minio://financecoffer/600001.json' "
            "WHERE symbol = '600001'"
        ).rowcount
        assert changed == 1


def test_duplicate_event_id_is_rejected(warehouse: dict[str, Any]) -> None:
    insert = (
        "INSERT INTO ops.etl_event_log (event_id, provider, data_type, symbol, biz_key) "
        "VALUES ('evt_dup', 'eastmoney', 'financial', '600000', '2026Q3|Q3')"
    )
    with pytest.raises(Exception, match="duplicate key"):
        with connect(warehouse["dsn"]) as conn:
            conn.execute(insert)
            conn.execute(insert)


def test_ods_inserts_land_in_a_monthly_partition(warehouse: dict[str, Any]) -> None:
    with connect(warehouse["dsn"]) as conn:
        partition = conn.execute(ODS_INSERT, ("evt_ods", "600000")).fetchone()[0]
        assert partition.startswith("ods.stock_raw_data_")
