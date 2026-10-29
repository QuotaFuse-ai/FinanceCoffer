#!/usr/bin/env python3
"""Restore a backup taken by ``scripts/backup.py`` into a database.

Safety rules, in this order:

1. ``--target-dsn`` is mandatory. It takes a literal DSN or ``env:NAME`` — use
   the ``env:`` form so the password never lands in the shell history.
2. The dump file must match the sha256 recorded in the manifest; a truncated or
   swapped dump is refused before anything is written.
3. The target must be an **empty** database. A non-empty target is refused
   unless ``--force`` *and* ``--confirm <database name>`` are both given, and
   that second confirmation is the only way to get past the check. A
   maintenance database (``postgres``, ``template0``, ``template1``) is refused
   outright — it can look empty, yet restoring into it litters the cluster's
   admin database.
4. The target may not be the database the backup came from.

After restoring, the checksum manifest is rebuilt from the restored database
and compared item by item with the backup manifest; the table is printed and
written to a report directory. Exit code 0 means every item matched.

    python scripts/restore.py --backup-dir var/backups/20261029T031500Z \\
        --target-dsn env:FC_RESTORE_DSN
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pg_backup_lib as lib  # noqa: E402

ROOT = lib.ROOT

# Cluster-level databases that must never receive a restore: they may report as
# empty while still being the admin database of the whole cluster.
MAINTENANCE_DATABASES = frozenset({"postgres", "template0", "template1"})

# Objects that make a database "not empty": relations of any kind a restore
# would collide with, including sequences created by ``db migrate``.
NON_EMPTY_QUERY = """
SELECT count(*)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
"""


def resolve_target_dsn(value: str) -> str:
    """``env:VAR`` reads the DSN from the environment; anything else is literal."""

    text = value.strip()
    if not text:
        raise lib.UsageError("--target-dsn is empty")
    if text.startswith("env:"):
        return lib.resolve_dsn(text[len("env:") :], what="target database")
    return lib.validate_dsn(text, what="--target-dsn")


def count_objects(dsn: str) -> int:
    conn = lib.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(NON_EMPTY_QUERY)
            row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def restore_dump(dsn: str, dump: bytes, *, force: bool) -> None:
    argv = [
        *lib.resolve_tool("pg_restore"),
        "--no-owner",
        "--no-privileges",
        "--no-password",
        "--exit-on-error",
        "--single-transaction",
        "--dbname",
        lib.describe_dsn(dsn)["database"],
    ]
    if force:
        # The target is not empty, so the objects the dump creates must go.
        argv[1:1] = ["--clean", "--if-exists"]
    lib.run_tool(argv, env=lib.libpq_env(dsn), stdin=dump, what="pg_restore")


def default_report_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "var" / "restore-reports" / stamp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore a FinanceCoffer backup.")
    parser.add_argument("--backup-dir", type=Path, required=True, help="directory from backup.py")
    parser.add_argument(
        "--target-dsn",
        required=True,
        help="target DSN literal, or env:NAME to read it from the environment",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow a non-empty target (still needs --confirm <database>)",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="second confirmation: the target database name, typed by hand",
    )
    parser.add_argument("--report-dir", type=Path, default=None, help="where to write the diff")
    parser.add_argument("--dry-run", action="store_true", help="run the safety checks only")
    parser.add_argument("--json", action="store_true", help="print the diff as JSON")
    return parser


def check_target_name(database: str) -> None:
    """Refuse the cluster's own databases before anything is written."""

    if database.lower() in MAINTENANCE_DATABASES:
        raise lib.BackupError(
            f"refusing to restore into {database}: it is a cluster maintenance "
            f"database. Restore into a dedicated, empty database instead."
        )


def _preflight(backup_dir: Path) -> tuple[dict[str, Any], Path]:
    manifest = lib.read_manifest(backup_dir / lib.MANIFEST_NAME)
    dump_info = manifest.get("dump") or {}
    dump_path = backup_dir / str(dump_info.get("file", lib.DUMP_NAME))
    if not dump_path.is_file():
        raise lib.UsageError(f"dump file missing: {dump_path}")
    actual = lib.sha256_file(dump_path)
    expected = dump_info.get("sha256")
    if expected and actual != expected:
        raise lib.BackupError(
            f"dump file does not match its manifest: sha256 {actual} != {expected}; "
            f"the backup is corrupt or was replaced"
        )
    return manifest, dump_path


def run(args: argparse.Namespace) -> int:
    lib.load_env_file()
    backup_dir = args.backup_dir.resolve()
    manifest, dump_path = _preflight(backup_dir)
    target_dsn = resolve_target_dsn(args.target_dsn)
    target = lib.describe_dsn(target_dsn)

    source_db = (manifest.get("source") or {}).get("database")
    check_target_name(target["database"])
    if source_db and source_db == target["database"]:
        raise lib.BackupError(
            f"refusing to restore into {target['database']}: it is the database the "
            f"backup came from. Restore into a fresh database instead."
        )

    objects = count_objects(target_dsn)
    print(
        f"==> backup  {backup_dir} "
        f"({manifest.get('created_at')}, schema v{manifest.get('schema_version')})"
    )
    print(
        f"==> target  {target['database']} on {target['host']}:{target['port']} "
        f"as {target['user']}"
    )
    if objects:
        print(f"!!  target is not empty: {objects} existing relations")
        if not args.force:
            raise lib.BackupError(
                "target database is not empty; restore needs an empty database. "
                "Drop and recreate it, or pass --force --confirm <database name> "
                "to overwrite the objects present in the dump."
            )
        if args.confirm != target["database"]:
            raise lib.UsageError(
                "--force also requires --confirm <database name>: "
                f'type --confirm "{target["database"]}" to overwrite it'
            )
        print("!!  --force --confirm accepted: objects present in the dump will be dropped")
    else:
        print("==> target is empty")

    if args.dry_run:
        print("==> dry run: safety checks passed, nothing written")
        return lib.EXIT_OK

    dump = dump_path.read_bytes()
    restore_dump(target_dsn, dump, force=bool(args.force and objects))
    print(f"==> pg_restore finished ({len(dump)} bytes)")

    restored = lib.build_manifest(target_dsn)
    rows = lib.diff_manifests(manifest, restored)
    passed, total = lib.summarize(rows)
    table = lib.render_diff(rows, only_failures=not args.json)

    report_dir = (args.report_dir or default_report_dir()).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    lib.write_manifest(restored, report_dir / "restored-manifest.json")
    (report_dir / "diff.txt").write_text(table + "\n", encoding="utf-8")

    print(table)
    print(f"== 比对结果：{passed}/{total} 项一致 ==")
    print(f"report: {report_dir}")
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "area": row.area,
                        "item": row.item,
                        "expected": row.expected,
                        "actual": row.actual,
                        "ok": row.ok,
                    }
                    for row in rows
                ],
                ensure_ascii=False,
            )
        )
    return lib.EXIT_OK if passed == total else lib.EXIT_FAILED


def main() -> None:
    lib.main_guard(lambda: run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
