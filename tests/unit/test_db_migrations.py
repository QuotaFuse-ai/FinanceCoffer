import re

import pytest

from financecoffer.core.errors import ConfigError, InvalidArgumentError, StorageError
from financecoffer.db.connection import describe_dsn, normalize_dsn, resolve_dsn
from financecoffer.db.migrations import MIGRATIONS_DIR, discover_migrations, render_sql
from financecoffer.db.partitions import parse_month, partition_name


def test_packaged_migrations_are_ordered_and_contiguous() -> None:
    migrations = discover_migrations()

    versions = [item.version for item in migrations]
    assert versions == list(range(1, len(versions) + 1))
    assert migrations[-1].version == 8
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

    positions = [script.index(f"-- >>> 000{version}_") for version in range(1, 9)]
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
