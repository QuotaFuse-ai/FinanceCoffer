"""Forward-only SQL migration runner.

Migrations are ordered ``NNNN_name.sql`` files shipped inside this package. The
runner keeps its ledger in ``ops.schema_version`` (version, name, sha256
checksum, duration) and applies each file inside its own transaction while
holding a session-level advisory lock, so two concurrent ``db migrate`` calls
serialize instead of racing.

Design rules:

* importing this module never touches the database;
* every applied migration is checksum-verified before anything new runs;
* repeated execution is a no-op because applied versions are skipped;
* down migrations are intentionally unsupported (forward-only, DEC-0008).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from financecoffer.core.errors import InvalidArgumentError, StorageError
from financecoffer.core.redaction import redact_text
from financecoffer.db.connection import connect, describe_dsn, resolve_dsn

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_FILE_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
_ADVISORY_LOCK_SQL = "SELECT pg_advisory_lock(hashtextextended('financecoffer:migrate', 0))"
_ADVISORY_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended('financecoffer:migrate', 0))"

LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.schema_version (
    version     INTEGER     PRIMARY KEY,
    name        TEXT        NOT NULL,
    checksum    CHAR(64)    NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms INTEGER     NOT NULL,
    applied_by  TEXT        NOT NULL DEFAULT current_user
);
"""


@dataclass(frozen=True, slots=True)
class Migration:
    """One ordered migration file."""

    version: int
    name: str
    path: Path
    sql: str
    #: sha256 of the file with line endings normalised to ``\n``. Content is
    #: what the ledger protects; a checkout that rewrites CRLF must not look
    #: like an edited migration.
    checksum: str
    #: sha256 of the bytes exactly as they sit on disk. Kept so ledgers written
    #: before normalisation (which recorded raw bytes) still verify.
    raw_checksum: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "name": self.name, "checksum": self.checksum}

    @property
    def accepted_checksums(self) -> set[str]:
        """Checksums the ledger may legitimately hold for this file.

        A ledger written from a CRLF checkout stores the digest of the CRLF
        bytes, while an LF checkout stores the digest of the LF bytes. Both are
        the same migration, so both digests verify; a genuinely edited file
        matches neither. The ledger value is a digest, so it cannot be
        normalised after the fact -- the equivalent is accepting the digest of
        the other line-ending form of the current content.
        """

        return {self.checksum, self.raw_checksum, self.crlf_checksum} - {""}

    @property
    def crlf_checksum(self) -> str:
        """sha256 of this file's content with CRLF line endings."""

        normalised = self.sql.replace("\r\n", "\n").replace("\n", "\r\n")
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """A row from ``ops.schema_version``."""

    version: int
    name: str
    checksum: str
    applied_at: datetime
    duration_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "checksum": self.checksum,
            "applied_at": self.applied_at.astimezone(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Outcome of ``fcoffer db migrate``."""

    database: dict[str, Any]
    from_version: int
    to_version: int
    applied: tuple[AppliedMigration, ...]
    partitions: tuple[str, ...] = ()
    partitions_created: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.partitions_created)

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "changed": self.changed,
            "applied": [item.as_dict() for item in self.applied],
            "partitions": list(self.partitions),
            "partitions_created": list(self.partitions_created),
        }


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Outcome of ``fcoffer db status``."""

    database: dict[str, Any]
    current_version: int
    head_version: int
    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "current_version": self.current_version,
            "head_version": self.head_version,
            "pending_count": len(self.pending),
            "pending": [item.as_dict() for item in self.pending],
            "applied": [item.as_dict() for item in self.applied],
        }


def discover_migrations(directory: Path | None = None) -> tuple[Migration, ...]:
    """Read migrations from ``directory`` (default: the packaged SQL folder)."""

    root = directory or MIGRATIONS_DIR
    if not root.is_dir():
        raise StorageError("migrations directory is missing", details={"path": str(root)})

    migrations: list[Migration] = []
    seen: dict[int, str] = {}
    for path in sorted(root.glob("*.sql")):
        match = _FILE_PATTERN.match(path.name)
        if match is None:
            raise StorageError(
                "migration file name must match NNNN_name.sql",
                details={"file": path.name},
            )
        version = int(match.group("version"))
        if version in seen:
            raise StorageError(
                "duplicate migration version",
                details={"version": version, "files": [seen[version], path.name]},
            )
        raw = path.read_bytes()
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                sql=raw.decode("utf-8"),
                checksum=hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
                raw_checksum=hashlib.sha256(raw).hexdigest(),
            )
        )
        seen[version] = path.name

    if not migrations:
        raise StorageError("no migrations found", details={"path": str(root)})
    return tuple(sorted(migrations, key=lambda item: item.version))


def render_sql(migrations: tuple[Migration, ...] | None = None) -> str:
    """Return every migration in order, annotated for ``db migrate --sql``."""

    items = migrations if migrations is not None else discover_migrations()
    blocks: list[str] = [
        "-- FinanceCoffer forward-only migrations (generated by `fcoffer db migrate --sql`)"
    ]
    for migration in items:
        blocks.append(f"\n-- >>> {migration.version:04d}_{migration.name}.sql")
        blocks.append(migration.sql.strip())
    return "\n".join(blocks).rstrip() + "\n"


def _resolve_target(revision: str, migrations: tuple[Migration, ...]) -> int:
    available = [item.version for item in migrations]
    head = available[-1]
    text = (revision or "head").strip().lower()
    if text == "head":
        return head
    try:
        target = int(text.removeprefix("v"))
    except ValueError as exc:
        raise InvalidArgumentError(
            "--revision must be 'head' or a numeric version",
            details={"revision": revision, "available": available},
        ) from exc
    if target not in available:
        raise InvalidArgumentError(
            "unknown migration revision",
            details={"revision": target, "available": available},
        )
    return target


class MigrationRunner:
    """Apply and inspect the ordered migration chain."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        directory: Path | None = None,
        timeout: int = 10,
    ) -> None:
        self._dsn = resolve_dsn(dsn)
        self._directory = directory
        self._timeout = timeout

    @property
    def database(self) -> dict[str, Any]:
        return describe_dsn(self._dsn)

    def _migrations(self) -> tuple[Migration, ...]:
        return discover_migrations(self._directory)

    def _connect(self) -> Any:
        return connect(self._dsn, timeout=self._timeout)

    def _ensure_ledger(self, conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute(LEDGER_DDL)
        conn.commit()

    def _applied(self, conn: Any) -> dict[int, AppliedMigration]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, name, checksum, applied_at, duration_ms"
                " FROM ops.schema_version ORDER BY version"
            )
            rows = cur.fetchall()
        return {
            int(row[0]): AppliedMigration(
                version=int(row[0]),
                name=str(row[1]),
                checksum=str(row[2]).strip(),
                applied_at=row[3],
                duration_ms=int(row[4]),
            )
            for row in rows
        }

    def _verify(
        self, migrations: tuple[Migration, ...], applied: dict[int, AppliedMigration]
    ) -> None:
        known = {item.version: item for item in migrations}
        unknown = sorted(set(applied) - set(known))
        if unknown:
            raise StorageError(
                "database has migrations unknown to this build",
                details={"versions": unknown, "hint": "deploy a newer fcoffer"},
            )
        for version, row in sorted(applied.items()):
            migration = known[version]
            if row.checksum not in migration.accepted_checksums:
                raise StorageError(
                    "applied migration checksum differs from the file",
                    details={
                        "version": version,
                        "name": migration.name,
                        "expected": row.checksum,
                        "actual": migration.checksum,
                        "hint": "never edit an applied migration; add a new one",
                    },
                )

    def status(self) -> StatusReport:
        migrations = self._migrations()
        with self._connect() as conn:
            self._ensure_ledger(conn)
            applied = self._applied(conn)
            self._verify(migrations, applied)
            current = max(applied, default=0)
            pending = tuple(item for item in migrations if item.version > current)
        return StatusReport(
            database=self.database,
            current_version=current,
            head_version=migrations[-1].version,
            applied=tuple(applied[key] for key in sorted(applied)),
            pending=pending,
        )

    def apply(
        self,
        *,
        revision: str = "head",
        ahead_months: int = 3,
        create_partitions: bool = True,
    ) -> MigrationReport:
        if ahead_months < 0:
            raise InvalidArgumentError(
                "--ahead must be >= 0",
                details={"field": "ahead", "value": ahead_months},
            )
        migrations = self._migrations()
        target = _resolve_target(revision, migrations)

        results: list[AppliedMigration] = []
        partitions: list[str] = []
        created: list[str] = []

        from financecoffer.db.partitions import ensure_partitions

        with self._connect() as conn:
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(_ADVISORY_LOCK_SQL)
                self._ensure_ledger(conn)
                applied = self._applied(conn)
                self._verify(migrations, applied)
                current = max(applied, default=0)
                if target < current:
                    raise InvalidArgumentError(
                        "down migrations are not supported",
                        details={"current_version": current, "requested": target},
                    )

                for migration in migrations:
                    if not current < migration.version <= target:
                        continue
                    started = time.perf_counter()
                    try:
                        with conn.transaction():
                            with conn.cursor() as cur:
                                cur.execute(migration.sql)
                                cur.execute(
                                    "INSERT INTO ops.schema_version"
                                    " (version, name, checksum, duration_ms)"
                                    " VALUES (%s, %s, %s, %s)",
                                    (
                                        migration.version,
                                        migration.name,
                                        migration.checksum,
                                        int((time.perf_counter() - started) * 1000),
                                    ),
                                )
                    except Exception as exc:  # noqa: BLE001 - re-raised as E_STORAGE
                        raise StorageError(
                            "migration failed",
                            details={
                                "version": migration.version,
                                "name": migration.name,
                                "error": redact_text(str(exc).strip()),
                            },
                        ) from exc
                    # Read the committed row back so the report matches the
                    # server clock recorded in the ledger, not the client clock.
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT applied_at, duration_ms FROM ops.schema_version"
                            " WHERE version = %s",
                            (migration.version,),
                        )
                        applied_at, duration_ms = cur.fetchone()
                    results.append(
                        AppliedMigration(
                            version=migration.version,
                            name=migration.name,
                            checksum=migration.checksum,
                            applied_at=applied_at,
                            duration_ms=int(duration_ms),
                        )
                    )

                if create_partitions and target >= 1:
                    rows = ensure_partitions(conn, ahead=ahead_months)
                    partitions = [row["partition"] for row in rows]
                    created = [row["partition"] for row in rows if row["created"]]
            finally:
                with conn.cursor() as cur:
                    cur.execute(_ADVISORY_UNLOCK_SQL)

        return MigrationReport(
            database=self.database,
            from_version=current,
            to_version=max(target, current),
            applied=tuple(results),
            partitions=tuple(partitions),
            partitions_created=tuple(created),
        )
