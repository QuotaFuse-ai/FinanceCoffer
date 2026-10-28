import hashlib
import re
from datetime import datetime

import pytest

from financecoffer.core.errors import ConfigError, InvalidArgumentError, StorageError
from financecoffer.db.connection import (
    describe_dsn,
    normalize_dsn,
    resolve_dsn,
    validate_dsn,
)
from financecoffer.db.migrations import (
    MIGRATIONS_DIR,
    AppliedMigration,
    MigrationRunner,
    discover_migrations,
    render_sql,
)
from financecoffer.db.partitions import parse_month, partition_name


def test_packaged_migrations_are_ordered_and_contiguous() -> None:
    migrations = discover_migrations()

    versions = [item.version for item in migrations]
    assert versions == list(range(1, len(versions) + 1))
    assert migrations[-1].version == 12
    names = [item.name for item in migrations]
    assert names == [
        "schemas",
        "ops",
        "ods",
        "dwd",
        "dws",
        "vector",
        "partitions",
        "compliance",
        "ods_raw_text",
        "dwd_archive_columns",
        "ods_symbol_nullable",
        "dwd_financial_statement",
    ]
    for item in migrations:
        assert re.fullmatch(r"[0-9a-f]{64}", item.checksum)
        assert item.sql.strip()


def test_checksum_is_content_addressed(tmp_path) -> None:
    (tmp_path / "0001_one.sql").write_text("SELECT 1;\n", encoding="utf-8")
    first = discover_migrations(tmp_path)[0]
    (tmp_path / "0001_one.sql").write_text("SELECT 2;\n", encoding="utf-8")
    second = discover_migrations(tmp_path)[0]

    assert first.checksum != second.checksum


def test_checksum_ignores_line_endings_but_keeps_the_raw_one(tmp_path) -> None:
    (tmp_path / "0001_one.sql").write_bytes(b"SELECT 1;\nSELECT 2;\n")
    lf = discover_migrations(tmp_path)[0]
    (tmp_path / "0001_one.sql").write_bytes(b"SELECT 1;\r\nSELECT 2;\r\n")
    crlf = discover_migrations(tmp_path)[0]

    # A checkout that rewrites CRLF must still verify against a ledger entry
    # written from either form; the SQL text itself is identical.
    assert lf.checksum == crlf.checksum
    assert lf.raw_checksum != crlf.raw_checksum
    assert crlf.raw_checksum in crlf.accepted_checksums
    assert lf.raw_checksum in crlf.accepted_checksums


def _applied(version: int, name: str, checksum: str) -> AppliedMigration:
    return AppliedMigration(
        version=version,
        name=name,
        checksum=checksum,
        applied_at=datetime(2026, 1, 1, 0, 0),
        duration_ms=1,
    )


def test_verify_accepts_a_crlf_ledger_against_an_lf_file(tmp_path) -> None:
    # QA-A-02: a ledger written from a CRLF checkout must verify against the
    # same migration read from an LF checkout, and vice versa.
    (tmp_path / "0001_one.sql").write_bytes(b"SELECT 1;\n")
    lf = discover_migrations(tmp_path)[0]
    (tmp_path / "0001_one.sql").write_bytes(b"SELECT 1;\r\n")
    crlf = discover_migrations(tmp_path)[0]

    runner = MigrationRunner("postgresql://user:secret@db.local:5432/warehouse")
    runner._verify((lf,), {1: _applied(1, "one", crlf.raw_checksum)})
    runner._verify((crlf,), {1: _applied(1, "one", lf.raw_checksum)})


def test_verify_still_rejects_a_genuinely_edited_migration(tmp_path) -> None:
    (tmp_path / "0001_one.sql").write_bytes(b"SELECT 1;\n")
    edited = discover_migrations(tmp_path)[0]
    stale = _applied(1, "one", hashlib.sha256(b"SELECT 2;\n").hexdigest())

    runner = MigrationRunner("postgresql://user:secret@db.local:5432/warehouse")

    with pytest.raises(StorageError, match="checksum differs"):
        runner._verify((edited,), {1: stale})


def test_duplicate_versions_are_rejected(tmp_path) -> None:
    (tmp_path / "0001_one.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "0001_two.sql").write_text("SELECT 2;\n", encoding="utf-8")

    with pytest.raises(StorageError, match="duplicate migration version"):
        discover_migrations(tmp_path)


def test_bad_file_name_is_rejected(tmp_path) -> None:
    (tmp_path / "1_bad-name.sql").write_text("SELECT 1;\n", encoding="utf-8")

    with pytest.raises(StorageError, match="NNNN_name.sql"):
        discover_migrations(tmp_path)


def test_empty_directory_is_rejected(tmp_path) -> None:
    with pytest.raises(StorageError, match="no migrations"):
        discover_migrations(tmp_path)


def test_render_sql_emits_every_migration_in_order() -> None:
    script = render_sql()

    positions = [script.index(f"-- >>> {version:04d}_") for version in range(1, 12)]
    assert positions == sorted(positions)
    assert script.rstrip().endswith(";")
    assert "CREATE TABLE IF NOT EXISTS ods.stock_raw_data" in script
    assert str(MIGRATIONS_DIR) not in script


def test_resolve_dsn_requires_configuration(monkeypatch) -> None:
    monkeypatch.delenv("FC_DB_URL", raising=False)
    monkeypatch.chdir(MIGRATIONS_DIR.parent)

    with pytest.raises(ConfigError, match="database URL is not configured"):
        resolve_dsn()


def test_resolve_dsn_prefers_override_and_normalizes_driver() -> None:
    dsn = resolve_dsn("postgresql+psycopg://user:secret@db.local:5433/warehouse")

    assert dsn == "postgresql://user:secret@db.local:5433/warehouse"
    assert normalize_dsn(dsn) == dsn


@pytest.mark.parametrize(
    "value",
    [
        "not-a-url",
        "mysql://user:secret@host:3306/db",
        "postgresql://user:secret@host:5432",
        "postgresql://user:secret@host:abc/db",
    ],
)
def test_validate_dsn_rejects_malformed_urls_as_config_errors(value: str) -> None:
    with pytest.raises(ConfigError, match="database URL"):
        validate_dsn(value)


def test_validate_dsn_normalizes_a_driver_url() -> None:
    assert (
        validate_dsn("postgresql+psycopg://user:secret@host:5433/db")
        == "postgresql://user:secret@host:5433/db"
    )


def test_describe_dsn_never_includes_the_password() -> None:
    described = describe_dsn("postgresql://fcoffer:supersecret@127.0.0.1:5432/financecoffer")

    assert described == {
        "host": "127.0.0.1",
        "port": 5432,
        "database": "financecoffer",
        "user": "fcoffer",
    }
    assert "supersecret" not in str(described)


@pytest.mark.parametrize("value", ["2026-11", "2026-1", "1999-12"])
def test_parse_month_accepts_yyyy_mm(value: str) -> None:
    parsed = parse_month(value)

    assert parsed.day == 1
    assert partition_name(parsed).startswith("stock_raw_data_")
    assert len(partition_name(parsed)) == len("stock_raw_data_") + 6


@pytest.mark.parametrize("value", ["2026/11", "202611", "2026-13", "2026-00", ""])
def test_parse_month_rejects_invalid_values(value: str) -> None:
    with pytest.raises(InvalidArgumentError):
        parse_month(value)
