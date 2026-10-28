"""Archive, status, restore and verify flows for cold DWD data (T-012).

The service keeps the two stores in the only safe order: the object is written
to MinIO and read back for confirmation *before* PostgreSQL is touched. A row
that fails anywhere in that sequence stays hot and usable, and the failure is
counted in the returned envelope instead of aborting the batch.

Every row has a deterministic object key (see :mod:`financecoffer.archive.layout`),
so a rerun that finds an object with the expected SHA-256 skips the upload and
only re-applies the marker.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Text, and_, cast, func, literal, or_, select, tuple_

from financecoffer.archive.layout import (
    TABLES,
    ArchiveCandidate,
    TableSpec,
    build_candidate,
    cold_cutoff,
    key_from_url,
    plain_mapping,
    sha256_hex,
)
from financecoffer.archive.storage import ObjectStorage, S3Storage
from financecoffer.core.config import Config
from financecoffer.core.errors import (
    FinanceCofferError,
    InvalidArgumentError,
    StorageError,
)
from financecoffer.core.redaction import redact_text
from financecoffer.db.models.ops import ArchiveJob
from financecoffer.db.repositories.ops import ArchiveJobRepository
from financecoffer.db.session import Database
from financecoffer.ods.service import parse_instant
from financecoffer.query import normalize_symbol

DEFAULT_MIN_BYTES = 8192
BATCH_SIZE = 200
DEFAULT_SAMPLE = 100
MAX_SAMPLE = 10_000
MAX_REPORTED = 50
_MAX_ERROR_CHARS = 1000


@dataclass(slots=True)
class _TableStats:
    table: str
    candidates: int = 0
    archived: int = 0
    skipped: int = 0
    bytes: int = 0
    failed: int = 0

    def payload(self, *, dry_run: bool) -> dict[str, Any]:
        return {
            "table": self.table,
            "candidates": self.candidates,
            "archived": 0 if dry_run else self.archived,
            "skipped_existing": 0 if dry_run else self.skipped,
            "bytes": self.bytes,
            "failed": self.failed,
        }


@dataclass(frozen=True, slots=True)
class _ArchivedRef:
    spec: TableSpec
    primary_key: dict[str, Any]
    url: str
    content_hash: str | None


class ArchiveService:
    """Move cold rows to MinIO, read them back and report on both sides."""

    def __init__(
        self,
        *,
        config: Config,
        dsn: str | None = None,
        timeout: int = 30,
        storage: ObjectStorage | None = None,
        prefix: str = "",
    ) -> None:
        self.config = config
        self.dsn = dsn or config.db_url
        self.timeout = timeout
        self.bucket = config.minio_bucket
        self.prefix = prefix
        self._storage = storage
        self._database: Database | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def database(self) -> Database:
        if self._database is None:
            self._database = Database(self.dsn)
        return self._database

    @property
    def storage(self) -> ObjectStorage:
        if self._storage is None:
            self._storage = S3Storage(self.config)
        return self._storage

    def close(self) -> None:
        if self._database is not None:
            self._database.dispose()
            self._database = None

    def __enter__(self) -> ArchiveService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- validation ---------------------------------------------------------

    def _resolve_years(self, value: int | None) -> int:
        years = self.config.cold_after_years if value is None else value
        if years < 0:
            raise InvalidArgumentError(
                "--older-than-years must be zero or greater",
                details={"field": "older_than_years"},
            )
        return years

    @staticmethod
    def _resolve_min_bytes(value: int | None) -> int:
        size = DEFAULT_MIN_BYTES if value is None else value
        if size < 1:
            raise InvalidArgumentError(
                "--min-bytes must be at least 1",
                details={"field": "min_bytes"},
            )
        return size

    @staticmethod
    def _resolve_limit(value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise InvalidArgumentError(
                "--limit must be at least 1",
                details={"field": "limit"},
            )
        return value

    @staticmethod
    def _resolve_sample(value: int | None) -> int:
        sample = DEFAULT_SAMPLE if value is None else value
        if sample < 1 or sample > MAX_SAMPLE:
            raise InvalidArgumentError(
                f"--sample must be between 1 and {MAX_SAMPLE}",
                details={"field": "sample"},
            )
        return sample

    # -- run ----------------------------------------------------------------

    def run(
        self,
        *,
        older_than_years: int | None = None,
        min_bytes: int | None = None,
        dry_run: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Archive cold rows; ``--dry-run`` reports only, writing nothing."""

        years = self._resolve_years(older_than_years)
        size = self._resolve_min_bytes(min_bytes)
        max_rows = self._resolve_limit(limit)
        cutoff = cold_cutoff(datetime.now(UTC).date(), years)

        failures: list[dict[str, Any]] = []
        stats: list[_TableStats] = []
        attempted = 0

        for spec in TABLES:
            remaining = None if max_rows is None else max_rows - attempted
            if remaining is not None and remaining <= 0:
                break
            table_stats = _TableStats(table=spec.name)
            stats.append(table_stats)
            for row in self._iter_cold_rows(spec, cutoff, size, limit=remaining):
                attempted += 1
                if dry_run:
                    self._preview_row(spec, row, table_stats, failures)
                else:
                    self._archive_row(spec, row, table_stats, failures)

        archived = sum(item.archived for item in stats)
        failed = sum(item.failed for item in stats)
        return {
            "dry_run": dry_run,
            "older_than_years": years,
            "cutoff": cutoff.isoformat(),
            "min_bytes": size,
            "limit": max_rows,
            "candidates": sum(item.candidates for item in stats),
            "archived": archived,
            "skipped_existing": sum(item.skipped for item in stats),
            "bytes": sum(item.bytes for item in stats),
            "failed": failed,
            "tables": [item.payload(dry_run=dry_run) for item in stats],
            "failures": failures,
        }

    def _cold_predicate(self, spec: TableSpec, cutoff: date, min_bytes: int) -> Any:
        model = spec.model
        conditions: list[Any] = [model.is_cold.is_(False)]
        age = spec.date_field < cutoff
        if spec.big_column is None:
            conditions.append(age)
            return and_(*conditions)
        big = getattr(model, spec.big_column)
        size_expr = (
            func.octet_length(cast(big, Text)) if spec.big_is_json else func.octet_length(big)
        )
        conditions.append(or_(age, size_expr > min_bytes))
        return and_(*conditions)

    def _iter_cold_rows(
        self,
        spec: TableSpec,
        cutoff: date,
        min_bytes: int,
        *,
        limit: int | None,
    ) -> Iterator[Any]:
        """Stream candidates in primary-key order with keyset pagination.

        Rows archived inside the loop drop out of the predicate, so the cursor
        (not an offset) is what keeps the next batch from skipping anything.
        """

        predicate = self._cold_predicate(spec, cutoff, min_bytes)
        pk_fields = spec.pk_fields
        yielded = 0
        cursor: tuple[Any, ...] | None = None
        while True:
            statement = select(spec.model).where(predicate)
            if cursor is not None:
                statement = statement.where(
                    tuple_(*pk_fields) > tuple_(*(literal(value) for value in cursor))
                )
            statement = statement.order_by(*pk_fields).limit(BATCH_SIZE)
            with self.database.session_scope() as session:
                rows = list(session.scalars(statement))
            if not rows:
                return
            for row in rows:
                if limit is not None and yielded >= limit:
                    return
                yielded += 1
                yield row
            if len(rows) < BATCH_SIZE:
                return
            cursor = tuple(getattr(rows[-1], name) for name in spec.pk_columns)

    def _preview_row(
        self,
        spec: TableSpec,
        row: Any,
        table_stats: _TableStats,
        failures: list[dict[str, Any]],
    ) -> None:
        table_stats.candidates += 1
        primary_key = {name: getattr(row, name) for name in spec.pk_columns}
        try:
            candidate = build_candidate(
                spec, row, bucket=self.bucket, prefix=self.prefix
            )
        except FinanceCofferError as exc:
            self._count_failure(
                spec,
                primary_key,
                reason=str(exc),
                table_stats=table_stats,
                failures=failures,
            )
            return
        if candidate.integrity_error is not None:
            self._count_failure(
                spec,
                primary_key,
                reason=candidate.integrity_error,
                object_key=candidate.object_key,
                symbol=candidate.symbol,
                table_stats=table_stats,
                failures=failures,
            )
            return
        table_stats.bytes += len(candidate.payload)

    def _archive_row(
        self,
        spec: TableSpec,
        row: Any,
        table_stats: _TableStats,
        failures: list[dict[str, Any]],
    ) -> None:
        table_stats.candidates += 1
        primary_key = {name: getattr(row, name) for name in spec.pk_columns}
        try:
            candidate = build_candidate(
                spec, row, bucket=self.bucket, prefix=self.prefix
            )
        except FinanceCofferError as exc:
            self._count_failure(
                spec,
                primary_key,
                reason=str(exc),
                table_stats=table_stats,
                failures=failures,
            )
            return
        if candidate.integrity_error is not None:
            self._count_failure(
                spec,
                primary_key,
                reason=candidate.integrity_error,
                object_key=candidate.object_key,
                symbol=candidate.symbol,
                table_stats=table_stats,
                failures=failures,
            )
            return

        try:
            uploaded = self._ensure_object(candidate)
        except Exception as exc:
            self._count_failure(
                spec,
                primary_key,
                reason=self._reason(exc),
                object_key=candidate.object_key,
                symbol=candidate.symbol,
                table_stats=table_stats,
                failures=failures,
            )
            return

        try:
            self._mark_archived(spec, candidate)
        except Exception as exc:
            self._count_failure(
                spec,
                primary_key,
                reason=self._reason(exc),
                object_key=candidate.object_key,
                symbol=candidate.symbol,
                table_stats=table_stats,
                failures=failures,
            )
            return

        table_stats.archived += 1
        if not uploaded:
            table_stats.skipped += 1
        table_stats.bytes += len(candidate.payload)

    def _ensure_object(self, candidate: ArchiveCandidate) -> bool:
        """Upload unless a matching object exists; return whether bytes were sent."""

        existing = self.storage.head(candidate.object_key)
        if existing is not None and existing.sha256 == candidate.expected_sha256:
            return False
        self.storage.put(
            candidate.object_key,
            candidate.payload,
            content_type=candidate.content_type,
            sha256=candidate.expected_sha256,
        )
        confirmed = self.storage.head(candidate.object_key)
        if confirmed is None:
            raise StorageError(
                "uploaded object cannot be found",
                details={"bucket": self.bucket, "key": candidate.object_key},
            )
        if confirmed.size != len(candidate.payload):
            raise StorageError(
                "uploaded object size does not match the payload",
                details={
                    "bucket": self.bucket,
                    "key": candidate.object_key,
                    "expected": len(candidate.payload),
                    "actual": confirmed.size,
                },
            )
        if confirmed.sha256 is not None and confirmed.sha256 != candidate.expected_sha256:
            raise StorageError(
                "uploaded object sha256 does not match the payload",
                details={"bucket": self.bucket, "key": candidate.object_key},
            )
        return True

    def _mark_archived(self, spec: TableSpec, candidate: ArchiveCandidate) -> None:
        """Apply the cold marker and the job record in one transaction."""

        key = self._primary_key_value(spec, candidate.primary_key)
        with self.database.session_scope() as session:
            repository = ArchiveJobRepository(session)
            job = repository.create(
                table_name=spec.name,
                object_key=candidate.object_key,
                symbol=candidate.symbol,
            )
            repository.mark_running(job.id)
            row = session.get(spec.model, key, with_for_update=True)
            if row is None:
                raise StorageError(
                    "row disappeared before it could be marked cold",
                    details={"table": spec.name, "object_key": candidate.object_key},
                )
            row.is_cold = True
            row.minio_url = candidate.minio_url
            if spec.kind == "announcement":
                row.clean_content = None
            repository.mark_done(
                job.id,
                rows_archived=1,
                bytes_archived=len(candidate.payload),
            )

    @staticmethod
    def _primary_key_value(spec: TableSpec, primary_key: dict[str, Any]) -> Any:
        values = tuple(primary_key[name] for name in spec.pk_columns)
        return values[0] if len(values) == 1 else values

    def _count_failure(
        self,
        spec: TableSpec,
        primary_key: dict[str, Any],
        *,
        reason: str,
        object_key: str | None = None,
        symbol: str | None = None,
        table_stats: _TableStats,
        failures: list[dict[str, Any]],
    ) -> None:
        table_stats.failed += 1
        journal_error = None
        if object_key is not None:
            journal_error = self._record_failed_job(spec, object_key, symbol, reason)
        if len(failures) >= MAX_REPORTED:
            return
        entry: dict[str, Any] = {
            "table": spec.name,
            "primary_key": plain_mapping(primary_key),
            "reason": reason,
        }
        if object_key is not None:
            entry["object_key"] = object_key
        if journal_error is not None:
            entry["journal_error"] = journal_error
        failures.append(entry)

    def _record_failed_job(
        self, spec: TableSpec, object_key: str, symbol: str | None, reason: str
    ) -> str | None:
        """Book a failed row; return a journal error instead of hiding one."""

        try:
            with self.database.session_scope() as session:
                repository = ArchiveJobRepository(session)
                job = repository.create(
                    table_name=spec.name,
                    object_key=object_key,
                    symbol=symbol,
                )
                repository.mark_failed(job.id, error=reason[:_MAX_ERROR_CHARS])
        except Exception as exc:
            return redact_text(str(exc)) or type(exc).__name__
        return None

    @staticmethod
    def _reason(exc: Exception) -> str:
        if isinstance(exc, FinanceCofferError):
            return exc.message
        return redact_text(str(exc)) or type(exc).__name__

    # -- status -------------------------------------------------------------

    def status(self, *, symbol: str | None = None) -> dict[str, Any]:
        """Hot/cold counts, archived bytes and cold rows without a URL."""

        normalized = normalize_symbol(symbol) if symbol else None
        tables = [self._status_table(spec, normalized) for spec in TABLES]
        totals = {
            "rows": sum(item["rows"] for item in tables),
            "hot": sum(item["hot"] for item in tables),
            "cold": sum(item["cold"] for item in tables),
            "archived_bytes": sum(item["archived_bytes"] for item in tables),
            "anomalies": sum(item["anomalies"] for item in tables),
            "failed_jobs": sum(item["failed_jobs"] for item in tables),
        }
        return {"symbol": normalized, "totals": totals, "tables": tables}

    def _status_table(self, spec: TableSpec, symbol: str | None) -> dict[str, Any]:
        model = spec.model
        cold = model.is_cold.is_(True)
        missing_url = and_(cold, model.minio_url.is_(None))
        statement = select(
            func.count().label("rows"),
            func.count().filter(cold).label("cold"),
            func.count().filter(missing_url).label("anomalies"),
        )
        if symbol is not None:
            statement = statement.where(model.symbol == symbol)
        bytes_statement = select(func.coalesce(func.sum(ArchiveJob.bytes_archived), 0)).where(
            ArchiveJob.table_name == spec.name,
            ArchiveJob.status == "done",
        )
        failed_statement = select(func.count()).select_from(ArchiveJob).where(
            ArchiveJob.table_name == spec.name,
            ArchiveJob.status == "failed",
        )
        if symbol is not None:
            bytes_statement = bytes_statement.where(ArchiveJob.symbol == symbol)
            failed_statement = failed_statement.where(ArchiveJob.symbol == symbol)

        with self.database.session_scope() as session:
            counts = session.execute(statement).one()
            archived_bytes = session.execute(bytes_statement).scalar_one()
            failed_jobs = session.execute(failed_statement).scalar_one()

        rows = int(counts.rows)
        cold_rows = int(counts.cold)
        return {
            "table": spec.name,
            "rows": rows,
            "hot": rows - cold_rows,
            "cold": cold_rows,
            "archived_bytes": int(archived_bytes or 0),
            "anomalies": int(counts.anomalies),
            "failed_jobs": int(failed_jobs or 0),
        }

    # -- restore ------------------------------------------------------------

    def restore(
        self,
        symbol: str,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Read archived rows back and verify them against their hashes.

        Announcements are written back into PostgreSQL; versioned facts and risk
        events are downloaded, verified and reported only, because their rows
        were never emptied.
        """

        normalized = normalize_symbol(symbol)
        start = parse_instant(since, field_name="since")
        end = parse_instant(until, field_name="until", end_of_day=True)

        failures: list[dict[str, Any]] = []
        restored = 0
        verified = 0
        scanned = 0
        missing_chunks = 0

        for spec in TABLES:
            for ref in self._archived_refs(spec, normalized, start, end):
                scanned += 1
                expected = ref.content_hash if spec.kind == "announcement" else None
                body, metadata = self._download(ref, failures)
                if body is None:
                    missing_chunks += 1
                    continue
                target = expected if expected is not None else metadata
                if target is None:
                    missing_chunks += 1
                    self._append_failure(
                        failures,
                        spec,
                        ref,
                        "object has no sha256 metadata to verify against",
                    )
                    continue
                if sha256_hex(body) != target:
                    missing_chunks += 1
                    self._append_failure(failures, spec, ref, "sha256 mismatch")
                    continue
                if spec.kind != "announcement":
                    verified += 1
                    continue
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError:
                    missing_chunks += 1
                    self._append_failure(failures, spec, ref, "body is not valid UTF-8")
                    continue
                try:
                    self._restore_announcement(ref, text)
                except Exception as exc:
                    self._append_failure(failures, spec, ref, self._reason(exc))
                    continue
                restored += 1

        failed = scanned - restored - verified
        return {
            "symbol": normalized,
            "since": start.isoformat() if start else None,
            "until": end.isoformat() if end else None,
            "scanned": scanned,
            "restored": restored,
            "verified": verified,
            "failed": failed,
            "fc_missing_chunk_total": missing_chunks,
            "failures": failures,
        }

    def _archived_refs(
        self,
        spec: TableSpec,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[_ArchivedRef]:
        model = spec.model
        pk_columns = [getattr(model, name) for name in spec.pk_columns]
        extra = [model.content_hash] if spec.kind == "announcement" else []
        statement = select(*pk_columns, model.minio_url, *extra).where(
            model.is_cold.is_(True),
            model.minio_url.is_not(None),
        )
        if symbol is not None:
            statement = statement.where(model.symbol == symbol)
        if start is not None:
            statement = statement.where(spec.date_field >= start.date())
        if end is not None:
            statement = statement.where(spec.date_field < end.date())
        with self.database.session_scope() as session:
            records = list(session.execute(statement))
        for record in records:
            values = tuple(record)
            ref = _ArchivedRef(
                spec=spec,
                primary_key=dict(zip(spec.pk_columns, values[: len(pk_columns)], strict=False)),
                url=str(values[len(pk_columns)]),
                content_hash=str(values[len(pk_columns) + 1]) if extra else None,
            )
            yield ref

    def _download(
        self, ref: _ArchivedRef, failures: list[dict[str, Any]]
    ) -> tuple[bytes | None, str | None]:
        try:
            key = key_from_url(ref.url, self.bucket)
            body, metadata = self.storage.get(key)
        except Exception as exc:
            self._append_failure(failures, ref.spec, ref, self._reason(exc))
            return None, None
        return body, metadata.sha256

    def _restore_announcement(self, ref: _ArchivedRef, text: str) -> None:
        with self.database.session_scope() as session:
            key = self._primary_key_value(ref.spec, ref.primary_key)
            row = session.get(ref.spec.model, key, with_for_update=True)
            if row is None:
                raise StorageError(
                    "announcement row disappeared before restore",
                    details={"table": ref.spec.name, "primary_key": plain_mapping(ref.primary_key)},
                )
            row.clean_content = text
            row.is_cold = False

    def _append_failure(
        self,
        failures: list[dict[str, Any]],
        spec: TableSpec,
        ref: _ArchivedRef,
        reason: str,
    ) -> None:
        if len(failures) >= MAX_REPORTED:
            return
        failures.append(
            {
                "table": spec.name,
                "primary_key": plain_mapping(ref.primary_key),
                "object_key": self._key_or_none(ref.url),
                "reason": reason,
            }
        )

    def _key_or_none(self, url: str) -> str | None:
        try:
            return key_from_url(url, self.bucket)
        except FinanceCofferError:
            return None

    # -- verify -------------------------------------------------------------

    def verify(self, *, sample: int | None = None) -> dict[str, Any]:
        """Download a random sample of archived objects and compare hashes."""

        size = self._resolve_sample(sample)
        refs = [ref for spec in TABLES for ref in self._archived_refs(spec)]
        chosen = refs if len(refs) <= size else random.sample(refs, size)

        failures: list[dict[str, Any]] = []
        passed = 0
        for ref in chosen:
            body, metadata = self._download(ref, failures)
            if body is None:
                continue
            target = ref.content_hash if ref.spec.kind == "announcement" else metadata
            if target is None:
                self._append_failure(
                    failures,
                    ref.spec,
                    ref,
                    "object has no sha256 metadata to verify against",
                )
                continue
            if sha256_hex(body) != target:
                self._append_failure(failures, ref.spec, ref, "sha256 mismatch")
                continue
            passed += 1

        failed = len(chosen) - passed
        return {
            "sample": size,
            "archived": len(refs),
            "sampled": len(chosen),
            "passed": passed,
            "failed": failed,
            "failures": failures,
        }
