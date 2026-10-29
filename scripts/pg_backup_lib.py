#!/usr/bin/env python3
"""Shared plumbing for ``scripts/backup.py`` and ``scripts/restore.py``.

Three pieces live here so the backup, the restore and the rehearsal agree on
them byte for byte:

* **Client tool resolution** — ``pg_dump`` / ``pg_restore`` are looked up in a
  fixed order (``PG_DUMP`` / ``PG_RESTORE`` command override, ``PG_BIN``
  directory, then ``PATH``) and a missing binary raises an actionable message
  instead of a ``FileNotFoundError`` traceback.
* **Manifest building** — the checksum manifest lists every user table with its
  row count plus an order-independent content checksum, the schema inventory
  (tables, indexes, constraints, sequences), sequence positions and the
  migration ledger, so two databases can be compared item by item.
* **Manifest comparison** — :func:`diff_manifests` returns one row per
  difference; it is pure data in, pure data out, which is what the unit tests
  exercise.

No password is ever printed: connection details are rendered through
:func:`describe_dsn`, and libpq receives the password through ``PGPASSWORD`` in
the child environment, never on a command line.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_FORMAT = "financecoffer-backup-manifest/1"
MANIFEST_NAME = "manifest.json"
DUMP_NAME = "dump.pgc"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_DEPENDENCY = 5

_TOOL_HINTS = {
    "pg_dump": "PG_DUMP",
    "pg_restore": "PG_RESTORE",
}


class BackupError(RuntimeError):
    """A failure that the CLI prints as ``ERROR: ...`` and exits non-zero."""

    exit_code = EXIT_FAILED


class UsageError(BackupError):
    exit_code = EXIT_USAGE


class ConfigError(BackupError):
    exit_code = EXIT_CONFIG


class DependencyError(BackupError):
    exit_code = EXIT_DEPENDENCY


class ToolNotFound(DependencyError):
    """``pg_dump`` / ``pg_restore`` is unavailable, with advice on how to fix it."""

    def __init__(self, tool: str) -> None:
        override = _TOOL_HINTS.get(tool, tool.upper())
        server_hint = "16"
        super().__init__(
            f"{tool} was not found; a PostgreSQL client of the same major version "
            f"as the server is required.\n"
            f"Pick one of:\n"
            f"  1) install the client (Debian/Ubuntu: apt-get install "
            f"postgresql-client-{server_hint}; macOS: brew install postgresql@{server_hint})\n"
            f"  2) point PG_BIN at the client bin directory, "
            f"e.g. PG_BIN=/usr/lib/postgresql/{server_hint}/bin\n"
            f"  3) set {override} to the full command, e.g. "
            f'{override}="docker compose -f deploy/compose.yaml exec -T postgres {tool}"\n'
            f"No client at all: use the container command documented in "
            f"deploy/README.md (section 升级与回滚)."
        )
        self.tool = tool


# --------------------------------------------------------------------------- #
# environment / DSN
# --------------------------------------------------------------------------- #
def load_env_file(path: Path | None = None) -> None:
    """Load repo ``.env`` into ``os.environ`` without overriding real variables.

    Real process variables win, so a container or a caller that already exports
    ``FC_DB_URL`` keeps it. Values are never echoed.
    """

    env_path = path or (ROOT / ".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _normalize_dsn(dsn: str) -> str:
    return re.sub(
        r"^postgres(?:ql)?\+[a-z0-9_]+://", "postgresql://", dsn.strip(), count=1, flags=re.I
    )


def redact(text: str) -> str:
    """Strip ``user:password`` from any URL-looking fragment of ``text``."""

    return re.sub(r"(postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^:\s/]+:)[^@\s]*@", r"\1***@", text)


def resolve_dsn(env_var: str = "FC_DB_URL", *, what: str = "source database") -> str:
    """Read a DSN from ``env_var`` (``.env`` already loaded by the caller)."""

    raw = os.environ.get(env_var, "").strip()
    if not raw:
        raise ConfigError(
            f"{what} DSN is not set: export {env_var} "
            f"(or add it to the repo .env). The DSN is never passed as a literal "
            f"argument so it cannot leak through the shell history."
        )
    dsn = _normalize_dsn(raw)
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"{env_var} must be a postgresql:// DSN (got {redact(raw)[:40]}...)")
    if not parsed.path.strip("/"):
        raise ConfigError(f"{env_var} must name a database")
    return dsn


def validate_dsn(text: str, *, what: str = "DSN") -> str:
    """Validate a DSN literal and return it normalised (never echo it back)."""

    dsn = _normalize_dsn(text)
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"{what} must be a postgresql:// DSN (got {redact(text)[:40]}...)")
    if not parsed.path.strip("/"):
        raise ConfigError(f"{what} must name a database")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{what} has a non-numeric port") from exc
    return dsn


def describe_dsn(dsn: str) -> dict[str, Any]:
    """Connection details safe to print (no password)."""

    parsed = urlsplit(dsn)
    try:
        port = parsed.port or 5432
    except ValueError:
        port = 5432
    return {
        "host": parsed.hostname or "",
        "port": port,
        "database": unquote(parsed.path.lstrip("/")),
        "user": unquote(parsed.username) if parsed.username else "",
    }


def libpq_env(dsn: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Translate the DSN into libpq ``PG*`` variables for child processes.

    Passing the connection through the environment keeps the password out of
    the command line (and out of any process listing).
    """

    parsed = urlsplit(dsn)
    query = parse_qs(parsed.query)
    env = dict(base if base is not None else os.environ)
    if parsed.hostname:
        env["PGHOST"] = parsed.hostname
    if parsed.port:
        env["PGPORT"] = str(parsed.port)
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    if parsed.path.strip("/"):
        env["PGDATABASE"] = unquote(parsed.path.lstrip("/"))
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    if "sslmode" in query:
        env["PGSSLMODE"] = query["sslmode"][0]
    return env


def with_database(dsn: str, database: str) -> str:
    """Return ``dsn`` pointing at another database on the same server."""

    parsed = urlsplit(dsn)
    return parsed._replace(path="/" + database).geturl()


# --------------------------------------------------------------------------- #
# psycopg
# --------------------------------------------------------------------------- #
def connect(dsn: str, *, autocommit: bool = False, timeout: int = 15) -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - install dependent
        raise DependencyError(
            "psycopg is required: install the database extra "
            "(pip install 'financecoffer[db]' or pip install psycopg[binary])"
        ) from exc
    try:
        conn = psycopg.connect(dsn, connect_timeout=timeout, application_name="fcoffer-backup")
    except Exception as exc:  # noqa: BLE001 - driver raises several subclasses
        target = describe_dsn(dsn)
        raise DependencyError(
            f"cannot connect to {target['database']} at "
            f"{target['host']}:{target['port']} as {target['user']}: {redact(str(exc))}"
        ) from exc
    if autocommit:
        conn.autocommit = True
    return conn


# --------------------------------------------------------------------------- #
# pg client tools
# --------------------------------------------------------------------------- #
def resolve_tool(tool: str, env: dict[str, str] | None = None) -> list[str]:
    """Resolve ``tool`` to an argv prefix; raise :class:`ToolNotFound` if absent."""

    source = os.environ if env is None else env
    override = source.get(_TOOL_HINTS.get(tool, tool.upper()), "").strip()
    if override:
        return shlex.split(override)

    bin_dir = source.get("PG_BIN", "").strip()
    if bin_dir:
        for candidate in (Path(bin_dir) / tool, Path(bin_dir) / f"{tool}.exe"):
            if candidate.is_file():
                return [str(candidate)]

    found = shutil.which(tool, path=source.get("PATH"))
    if found:
        return [found]
    raise ToolNotFound(tool)


def run_tool(
    argv: Sequence[str],
    *,
    env: dict[str, str],
    stdin: bytes | None = None,
    what: str,
) -> subprocess.CompletedProcess[bytes]:
    """Run a pg client binary, streaming bytes; raise ``DependencyError`` on failure."""

    try:
        result = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:  # the override named a missing binary
        raise DependencyError(f"{what} could not start: {exc.filename} is not executable") from exc
    if result.returncode != 0:
        detail = redact(result.stderr.decode("utf-8", "replace").strip())[:1200]
        raise DependencyError(f"{what} failed (exit {result.returncode}):\n{detail}")
    return result


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
TABLE_QUERY = """
SELECT n.nspname || '.' || c.relname AS name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
ORDER BY 1
"""

# Partitions are reachable through their parent, so only roots are checksummed:
# doing both would read the same rows twice.
STATS_TABLE_QUERY = """
SELECT n.nspname || '.' || c.relname AS name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
ORDER BY 1
"""

INDEX_QUERY = """
SELECT schemaname || '.' || indexname AS name
FROM pg_indexes
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  AND schemaname NOT LIKE 'pg\\_toast%'
ORDER BY 1
"""

CONSTRAINT_QUERY = """
SELECT n.nspname || '.' || c.relname || ':' || con.conname || ':' || con.contype::text AS name
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
ORDER BY 1
"""

SEQUENCE_QUERY = """
SELECT schemaname || '.' || sequencename AS name,
       COALESCE(last_value::text, '-') AS last_value
FROM pg_sequences
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1
"""

MIGRATION_QUERY = """
SELECT version, name, checksum
FROM ops.schema_version
ORDER BY version
"""


def _query(conn: Any, sql: str) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def quote_ident(name: str) -> str:
    """Quote ``schema.table`` for use in ``FROM``."""

    schema, _, table = name.partition(".")
    return '"' + schema.replace('"', '""') + '"."' + table.replace('"', '""') + '"'


def table_stats(conn: Any, table: str) -> dict[str, Any]:
    """Row count plus an order-independent checksum of the table content.

    ``sum`` and ``bit_xor`` over the first 64 bits of ``md5(row::text)`` are
    both commutative, so the pair does not depend on physical row order — a
    ``VACUUM``, a different plan or a restored heap produces the same values.
    Together they catch an added row, a removed row and an edited row.
    """

    sql = (
        "SELECT count(*)::bigint, "
        "COALESCE(sum(('x' || substr(md5(t::text), 1, 16))::bit(64)::bigint::numeric), 0)::text, "
        "COALESCE(bit_xor(('x' || substr(md5(t::text), 1, 16))::bit(64)::bigint), 0)::text "
        f"FROM {quote_ident(table)} AS t"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    assert row is not None
    return {"table": table, "rows": int(row[0]), "checksum": {"sum": row[1], "xor": row[2]}}


def _git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    commit = run("rev-parse", "HEAD")
    return {
        "commit": commit or "unknown",
        "subject": run("log", "-1", "--pretty=%s") or "unknown",
        "dirty": bool(run("status", "--porcelain")),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    dsn: str,
    *,
    dump_path: Path | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Collect the full comparison manifest for the database behind ``dsn``."""

    conn = connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            server_version = str(cur.fetchone()[0])

        inventory = {
            "tables": [row[0] for row in _query(conn, TABLE_QUERY)],
            "indexes": [row[0] for row in _query(conn, INDEX_QUERY)],
            "constraints": [row[0] for row in _query(conn, CONSTRAINT_QUERY)],
        }
        sequences = {row[0]: row[1] for row in _query(conn, SEQUENCE_QUERY)}
        migrations = [
            {"version": int(row[0]), "name": row[1], "checksum": row[2]}
            for row in _query(conn, MIGRATION_QUERY)
        ]

        tables = []
        for (table,) in _query(conn, STATS_TABLE_QUERY):
            tables.append(table_stats(conn, table))
            if progress is not None:
                progress(f"  {table}: {tables[-1]['rows']} rows")
    finally:
        conn.close()

    manifest: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "source": describe_dsn(dsn),
        "server_version": server_version,
        "git": _git_state(),
        "schema_version": migrations[-1]["version"] if migrations else 0,
        "migrations": migrations,
        "inventory": inventory,
        "counts": {name: len(items) for name, items in inventory.items()} | {
            "sequences": len(sequences),
            "tables_checked": len(tables),
        },
        "sequences": sequences,
        "tables": tables,
        "totals": {"rows": sum(item["rows"] for item in tables)},
    }
    if dump_path is not None:
        manifest["dump"] = {
            "file": dump_path.name,
            "bytes": dump_path.stat().st_size,
            "sha256": sha256_file(dump_path),
            "format": "pg_dump custom",
        }
    return manifest


def write_manifest(manifest: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise UsageError(f"manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError(f"manifest is not valid JSON: {path} ({exc})") from exc
    if data.get("format") != MANIFEST_FORMAT:
        raise UsageError(
            f"unexpected manifest format {data.get('format')!r}, expected {MANIFEST_FORMAT!r}"
        )
    return data


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DiffRow:
    """One comparison line: what was expected, what was found."""

    area: str
    item: str
    expected: str
    actual: str

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


def _rows_table(index: dict[str, Any], table: str) -> dict[str, Any] | None:
    for entry in index.get("tables", []):
        if entry.get("table") == table:
            return entry
    return None


def diff_manifests(backup: dict[str, Any], restored: dict[str, Any]) -> list[DiffRow]:
    """Compare a backup manifest with the manifest rebuilt after a restore.

    Everything the restore is supposed to reproduce is compared: the migration
    ledger, the schema object sets, per-table row counts and content checksums,
    and sequence positions. Only fields that legitimately differ between two
    databases (timestamps, host, git state) are excluded.
    """

    rows: list[DiffRow] = []

    rows.append(
        DiffRow(
            "账本",
            "schema_version",
            str(backup.get("schema_version", "?")),
            str(restored.get("schema_version", "?")),
        )
    )
    rows.append(
        DiffRow(
            "账本",
            "迁移记录数",
            str(len(backup.get("migrations", []))),
            str(len(restored.get("migrations", []))),
        )
    )
    for area, key in (
        ("表", "tables"),
        ("索引", "indexes"),
        ("约束", "constraints"),
    ):
        rows.append(
            DiffRow(
                "结构",
                f"{area}数量",
                str(len(backup.get("inventory", {}).get(key, []))),
                str(len(restored.get("inventory", {}).get(key, []))),
            )
        )
    rows.append(
        DiffRow(
            "结构",
            "序列数量",
            str(len(backup.get("sequences", {}))),
            str(len(restored.get("sequences", {}))),
        )
    )

    for key, label in (("tables", "表"), ("indexes", "索引"), ("constraints", "约束")):
        expected = set(backup.get("inventory", {}).get(key, []))
        actual = set(restored.get("inventory", {}).get(key, []))
        for missing in sorted(expected - actual):
            rows.append(DiffRow("结构", f"{label}缺失 {missing}", "存在", "缺失"))
        for extra in sorted(actual - expected):
            rows.append(DiffRow("结构", f"{label}多余 {extra}", "不存在", "多余"))

    backup_tables = {entry["table"]: entry for entry in backup.get("tables", [])}
    restored_tables = {entry["table"]: entry for entry in restored.get("tables", [])}
    for name in sorted(set(backup_tables) | set(restored_tables)):
        expected = backup_tables.get(name)
        actual = restored_tables.get(name)
        if expected is None:
            rows.append(
                DiffRow("数据", f"{name} 行数", "0", str(actual["rows"] if actual else "?"))
            )
            continue
        if actual is None:
            rows.append(DiffRow("数据", f"{name} 行数", str(expected["rows"]), "表缺失"))
            continue
        rows.append(DiffRow("数据", f"{name} 行数", str(expected["rows"]), str(actual["rows"])))
        rows.append(
            DiffRow(
                "数据",
                f"{name} 内容校验",
                f"{expected['checksum']['sum']}/{expected['checksum']['xor']}",
                f"{actual['checksum']['sum']}/{actual['checksum']['xor']}",
            )
        )

    backup_sequences = backup.get("sequences", {})
    restored_sequences = restored.get("sequences", {})
    for name in sorted(set(backup_sequences) | set(restored_sequences)):
        rows.append(
            DiffRow(
                "序列",
                name,
                str(backup_sequences.get(name, "缺失")),
                str(restored_sequences.get(name, "缺失")),
            )
        )
    return rows


def summarize(rows: Iterable[DiffRow]) -> tuple[int, int]:
    rows = list(rows)
    return sum(1 for row in rows if row.ok), len(rows)


def render_diff(rows: Sequence[DiffRow], *, only_failures: bool = False) -> str:
    """Render the diff as an aligned text table."""

    selected = [row for row in rows if row.ok is False] if only_failures else list(rows)
    if not selected:
        return "（无差异）"
    header = ("结果", "区域", "项目", "备份", "恢复后")
    body = [
        ("OK" if row.ok else "DIFF", row.area, row.item, _clip(row.expected), _clip(row.actual))
        for row in selected
    ]
    return _table(header, body)


def _clip(value: str, width: int = 42) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _width(text: str) -> int:
    # CJK glyphs occupy two terminal columns; without this the table skews.
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _table(header: Sequence[str], body: Sequence[Sequence[str]]) -> str:
    widths = [
        max(_width(str(row[index])) for row in [header, *body]) for index in range(len(header))
    ]

    def fmt(row: Sequence[str]) -> str:
        cells = [
            str(cell) + " " * (widths[index] - _width(str(cell)))
            for index, cell in enumerate(row)
        ]
        return "| " + " | ".join(cells) + " |"

    separator = "| " + " | ".join("-" * width for width in widths) + " |"
    return "\n".join([fmt(header), separator, *(fmt(row) for row in body)])


def main_guard(runner: Any) -> None:
    """Run ``runner()`` and turn :class:`BackupError` into a one-line error + exit."""

    try:
        code = runner()
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130) from None
    raise SystemExit(code)

