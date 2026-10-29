"""Checks for the backup / restore tooling in ``scripts/``.

The database round trip itself is rehearsed by ``scripts/backup_drill.py``
against throwaway ``financecoffer_qa_*`` databases (it needs a live server, so
it is not a unit test). What is asserted here is everything that can go wrong
silently in the repository:

* the committed scripts carry no machine-specific path and no credential,
* the DSN really comes from the environment,
* a missing ``pg_dump`` produces an actionable message rather than a traceback,
* and the manifest comparison actually reports a difference — a comparison that
  never fails would make the whole rehearsal meaningless.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
TOOLING = {
    "backup.py": ("backup", "--dsn-env"),
    "restore.py": ("restore", "--target-dsn"),
    "backup_drill.py": ("drill", "financecoffer_qa_"),
    "pg_backup_lib.py": ("manifest", "pg_dump"),
}

# A drive letter only counts when it starts a path: ``of:\n`` inside an
# f-string is not ``C:\...``.
_DRIVE_PATH = re.compile(r"(?<![\w])[A-Za-z]:\\[A-Za-z0-9_.$]")
_DSN_WITH_PASSWORD = re.compile(r"postgresql://[^\s/@'\"]+:[^\s/@'\"]+@")


def _load_module(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclass annotation resolution looks the class's module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_lib() -> ModuleType:
    return _load_module("pg_backup_lib_under_test", "pg_backup_lib.py")


@pytest.mark.parametrize("name", sorted(TOOLING))
def test_script_exists_and_mentions_its_contract(name: str) -> None:
    path = SCRIPTS / name
    assert path.is_file(), f"{name} is missing"
    text = path.read_text(encoding="utf-8")
    for fragment in TOOLING[name]:
        assert fragment in text, f"{name} never mentions {fragment!r}"


@pytest.mark.parametrize("name", sorted(TOOLING))
def test_script_has_no_machine_specific_path(name: str) -> None:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "Administrator" not in text
    assert not _DRIVE_PATH.search(text), "machine-specific path in committed script"


@pytest.mark.parametrize("name", sorted(TOOLING))
def test_script_ships_no_credentials(name: str) -> None:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "fcoffer:fcoffer" not in text
    assert not _DSN_WITH_PASSWORD.search(text), "a literal DSN with a password is committed"
    if name != "pg_backup_lib.py":
        # The password rides in libpq's PG* environment, never in argv: the
        # entry points reach the database through the shared plumbing.
        assert "lib.libpq_env" in text or "lib.connect" in text, (
            f"{name} connects without going through pg_backup_lib"
        )
    assert "--password" not in text, f"{name} must use --no-password"


def test_scripts_read_the_dsn_from_the_environment() -> None:
    backup = (SCRIPTS / "backup.py").read_text(encoding="utf-8")
    restore = (SCRIPTS / "restore.py").read_text(encoding="utf-8")
    assert '"FC_DB_URL"' in backup
    assert "env:" in restore and "FC_DB_URL" in (SCRIPTS / "pg_backup_lib.py").read_text(
        encoding="utf-8"
    )
    # restore must refuse to run without an explicit target
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "restore.py"), "--backup-dir", "unused"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 2
    assert "--target-dsn" in result.stderr


def test_restore_refuses_the_cluster_maintenance_databases() -> None:
    """``postgres`` looks empty to an object count, yet a restore into it
    litters the admin database of the whole cluster."""

    restore = _load_module("restore_under_test", "restore.py")
    for name in ("postgres", "template0", "template1", "POSTGRES"):
        with pytest.raises(restore.lib.BackupError) as caught:
            restore.check_target_name(name)
        assert "maintenance" in str(caught.value)
    restore.check_target_name("financecoffer_qa_restore_ok")


def test_drill_prints_the_required_final_line() -> None:
    text = (SCRIPTS / "backup_drill.py").read_text(encoding="utf-8")
    assert "== 备份恢复演练：" in text
    assert "项通过 ==" in text


def test_drill_refuses_a_prefix_outside_the_qa_series() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "backup_drill.py"), "--prefix", "financecoffer"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 2
    assert "financecoffer_qa_" in result.stderr


def test_missing_client_tool_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    lib = load_lib()
    monkeypatch.delenv("PG_DUMP", raising=False)
    monkeypatch.delenv("PG_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(lib.ToolNotFound) as caught:
        lib.resolve_tool("pg_dump")
    message = str(caught.value)
    assert "pg_dump" in message
    assert "PG_BIN" in message and "PG_DUMP" in message
    assert "deploy/README.md" in message
    assert caught.value.exit_code == lib.EXIT_DEPENDENCY
    assert "Traceback" not in message


def _manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "format": "financecoffer-backup-manifest/1",
        "schema_version": 12,
        "migrations": [{"version": 12, "name": "0012_x", "checksum": "abc"}],
        "inventory": {
            "tables": ["ops.a", "ops.b"],
            "indexes": ["ops.a_pkey", "ops.idx_b"],
            "constraints": ["ops.a:a_pkey:p", "ops.b:b_pkey:p"],
        },
        "counts": {},
        "sequences": {"ops.a_id_seq": "7"},
        "tables": [
            {
                "table": "ops.a",
                "rows": 2,
                "checksum": {"sum": "100", "xor": "50"},
            },
            {
                "table": "ops.b",
                "rows": 1,
                "checksum": {"sum": "7", "xor": "7"},
            },
        ],
        "totals": {"rows": 3},
    }
    manifest.update(overrides)
    return manifest


def test_identical_manifests_compare_clean() -> None:
    lib = load_lib()
    rows = lib.diff_manifests(_manifest(), _manifest())
    passed, total = lib.summarize(rows)
    assert passed == total
    assert total > 0


def test_diff_catches_a_changed_row() -> None:
    """The negative case the rehearsal depends on: one edited row must show up."""

    lib = load_lib()
    expected = _manifest()
    actual = _manifest()
    actual["tables"][0]["checksum"] = {"sum": "999", "xor": "50"}
    rows = lib.diff_manifests(expected, actual)
    failed = [row for row in rows if not row.ok]
    assert failed, "an edited row compared as identical — the diff is fake"
    assert any(row.item == "ops.a 内容校验" for row in failed)
    assert all(row.item != "ops.b 内容校验" for row in failed)


def test_diff_catches_a_changed_row_count() -> None:
    lib = load_lib()
    actual = _manifest()
    actual["tables"][1]["rows"] = 2
    failed = [row for row in lib.diff_manifests(_manifest(), actual) if not row.ok]
    assert [row.item for row in failed] == ["ops.b 行数"]


def test_diff_catches_missing_table_and_sequence_drift() -> None:
    lib = load_lib()
    actual = _manifest()
    actual["inventory"]["tables"] = ["ops.a"]
    actual["inventory"]["indexes"] = ["ops.a_pkey"]
    actual["sequences"] = {"ops.a_id_seq": "9"}
    failed = {row.item for row in lib.diff_manifests(_manifest(), actual) if not row.ok}
    assert "表缺失 ops.b" in failed
    assert "索引缺失 ops.idx_b" in failed
    assert "表数量" in failed
    assert "ops.a_id_seq" in failed


def test_render_diff_highlights_only_failures_when_asked() -> None:
    lib = load_lib()
    actual = _manifest()
    actual["schema_version"] = 11
    rows = lib.diff_manifests(_manifest(), actual)
    text = lib.render_diff(rows, only_failures=True)
    assert "schema_version" in text
    assert "DIFF" in text
    assert "ops.a 行数" not in text


def test_manifest_roundtrip(tmp_path: Path) -> None:
    lib = load_lib()
    path = tmp_path / "manifest.json"
    lib.write_manifest(_manifest(), path)
    assert lib.read_manifest(path)["schema_version"] == 12
    path.write_text(json.dumps({"format": "other"}), encoding="utf-8")
    with pytest.raises(lib.UsageError):
        lib.read_manifest(path)


def test_describe_dsn_never_returns_the_password() -> None:
    lib = load_lib()
    details = lib.describe_dsn("postgresql://alice:s3cret@db.example:5433/warehouse")
    assert details == {
        "host": "db.example",
        "port": 5433,
        "database": "warehouse",
        "user": "alice",
    }
    assert "s3cret" not in json.dumps(details)
