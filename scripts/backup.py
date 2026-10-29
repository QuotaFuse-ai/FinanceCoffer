#!/usr/bin/env python3
"""Take a restorable PostgreSQL backup plus a checksum manifest.

``pg_dump -Fc`` writes the database to ``var/backups/<timestamp>/dump.pgc`` and
``manifest.json`` records what the database looked like when the dump was
taken: the migration ledger, the schema inventory (tables, indexes,
constraints, sequences), the row count and an order-independent content
checksum per table, and the sha256 of the dump file itself. ``restore.py``
rebuilds the same manifest from the restored database and compares item by
item, so "the backup restores into an identical database" becomes a check
rather than a belief.

The DSN comes from an environment variable (``FC_DB_URL`` by default, or the
repo ``.env``), never from a command-line literal, and the password travels to
libpq through ``PGPASSWORD`` in the child environment, so it appears in no
argument list and in no output.

    python scripts/backup.py
    python scripts/backup.py --dsn-env FC_DB_URL --out var/backups/nightly
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


def default_out_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "var" / "backups" / stamp


def dump_database(dsn: str, dump_path: Path, *, lock_wait: str) -> None:
    """Stream ``pg_dump -Fc`` into ``dump_path``.

    The dump travels through stdout instead of ``-f`` so the same code works
    when the client runs somewhere else (a container, a jump host): no host
    path has to be resolved by the other side.
    """

    argv = [
        *lib.resolve_tool("pg_dump"),
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--no-password",
        f"--lock-wait-timeout={lock_wait}",
    ]
    result = lib.run_tool(
        argv, env=lib.libpq_env(dsn), what="pg_dump"
    )
    dump_path.write_bytes(result.stdout)


def take_backup(
    dsn: str,
    out_dir: Path,
    *,
    lock_wait: str = "120s",
    progress: Any = None,
) -> dict[str, Any]:
    """Create ``out_dir``, write the dump, then build and store the manifest."""

    out_dir.mkdir(parents=True, exist_ok=True)
    dump_path = out_dir / lib.DUMP_NAME
    dump_database(dsn, dump_path, lock_wait=lock_wait)
    manifest = lib.build_manifest(dsn, dump_path=dump_path, progress=progress)
    lib.write_manifest(manifest, out_dir / lib.MANIFEST_NAME)
    return manifest


def _summary(manifest: dict[str, Any], out_dir: Path) -> str:
    source = manifest["source"]
    dump = manifest["dump"]
    lines = [
        f"backup:   {source['database']} on {source['host']}:{source['port']} "
        f"as {source['user']}",
        f"dump:     {out_dir / dump['file']}",
        f"          {dump['bytes']} bytes, sha256 {dump['sha256']}",
        f"manifest: {out_dir / lib.MANIFEST_NAME}",
        f"schema:   version {manifest['schema_version']} "
        f"({manifest['counts']['tables']} tables, {manifest['counts']['indexes']} indexes, "
        f"{manifest['counts']['constraints']} constraints)",
        f"data:     {manifest['counts']['tables_checked']} tables checksummed, "
        f"{manifest['totals']['rows']} rows",
        f"git:      {manifest['git']['commit'][:12]}"
        f"{' (dirty)' if manifest['git']['dirty'] else ''}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up PostgreSQL with a checksum manifest.")
    parser.add_argument(
        "--dsn-env",
        default="FC_DB_URL",
        help="environment variable holding the source DSN (default: FC_DB_URL)",
    )
    parser.add_argument("--out", type=Path, default=None, help="artifact directory")
    parser.add_argument(
        "--lock-wait",
        default="120s",
        help="pg_dump --lock-wait-timeout value (default: 120s)",
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    return parser


def run(args: argparse.Namespace) -> int:
    lib.load_env_file()
    dsn = lib.resolve_dsn(args.dsn_env)
    out_dir = (args.out or default_out_dir()).resolve()

    print(f"==> pg_dump {lib.describe_dsn(dsn)['database']} -> {out_dir}")
    manifest = take_backup(dsn, out_dir, lock_wait=args.lock_wait)
    print(_summary(manifest, out_dir))
    if args.json:
        print(json.dumps({"out_dir": str(out_dir), "manifest": manifest}, ensure_ascii=False))
    return lib.EXIT_OK


def main() -> None:
    lib.main_guard(lambda: run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
