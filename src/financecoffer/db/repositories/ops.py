"""Ops repositories: ETL bookkeeping, heartbeats, offsets, DLQ and archive jobs.

The event log is the authoritative idempotency gate: ``claim`` returns ``None``
when ``event_id`` (or the provider/data_type/symbol/biz_key identity) is
already present, so the caller skips the event instead of writing it twice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from financecoffer.db.models.ops import (
    ArchiveJob,
    EtlConsumerOffset,
    EtlDlq,
    EtlEventLog,
    SourceHeartbeat,
)
from financecoffer.db.repositories.base import BaseRepository


class EventLogRepository(BaseRepository[EtlEventLog]):
    model = EtlEventLog

    def claim(
        self,
        event_id: str,
        *,
        provider: str,
        data_type: str,
        biz_key: str,
        symbol: str | None = None,
        event_time: datetime | None = None,
        trace_id: str | None = None,
    ) -> EtlEventLog | None:
        """Register an event once; ``None`` means it was already claimed."""

        return self.insert_ignore_conflict(
            event_id=event_id,
            provider=provider,
            data_type=data_type,
            symbol=symbol,
            biz_key=biz_key,
            event_time=event_time,
            trace_id=trace_id,
        )

    def find_by_identity(
        self,
        *,
        provider: str,
        data_type: str,
        symbol: str | None,
        biz_key: str,
    ) -> EtlEventLog | None:
        """Return the row owning this business identity, if any.

        ``symbol IS NULL`` has to be written out: the unique constraint uses
        ``NULLS NOT DISTINCT``, so two null symbols are the same identity.
        """

        symbol_filter = (
            self.model.symbol.is_(None) if symbol is None else self.model.symbol == symbol
        )
        statement = select(self.model).where(
            self.model.provider == provider,
            self.model.data_type == data_type,
            symbol_filter,
            self.model.biz_key == biz_key,
        )
        return self._one_or_none(statement)

    def record_duplicate_delivery(self, event_id: str) -> EtlEventLog | None:
        """Count one redelivery of an already-registered event."""

        return self._transition(event_id, attempts=self.model.attempts + 1)

    def requeue(self, event_id: str) -> EtlEventLog | None:
        """Hand an event back to the consumer after an ``ods replay``.

        Replay never rewrites ODS; the event log is the durable queue, so a
        replay is a status reset. ``attempts`` is left alone because it counts
        processing attempts, which ``begin`` bumps.
        """

        return self._transition(
            event_id,
            status="received",
            error=None,
            processed_at=None,
        )

    def begin(self, event_id: str) -> EtlEventLog | None:
        return self._transition(
            event_id,
            status="processing",
            attempts=self.model.attempts + 1,
        )

    def succeed(self, event_id: str) -> EtlEventLog | None:
        return self._transition(
            event_id,
            status="done",
            error=None,
            processed_at=func.now(),
        )

    def fail(self, event_id: str, error: str) -> EtlEventLog | None:
        return self._transition(event_id, status="failed", error=error)

    def to_dlq(self, event_id: str, error: str) -> EtlEventLog | None:
        return self._transition(event_id, status="dlq", error=error)

    def _transition(self, event_id: str, **values: Any) -> EtlEventLog | None:
        statement = (
            update(self.model)
            .where(self.model.event_id == event_id)
            .values(**values)
            .returning(self.model)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return self._one_or_none(statement)


class HeartbeatRepository(BaseRepository[SourceHeartbeat]):
    model = SourceHeartbeat

    def record(
        self,
        source: str,
        *,
        last_seen: datetime,
        status: str,
        items: int = 0,
        run_id: str | None = None,
    ) -> SourceHeartbeat:
        """Upsert the current heartbeat; heartbeat state is not history."""

        statement = (
            pg_insert(self.model)
            .values(
                source=source,
                last_seen=last_seen,
                status=status,
                items=items,
                run_id=run_id,
            )
            .on_conflict_do_update(
                index_elements=[self.model.source],
                set_={
                    "last_seen": last_seen,
                    "status": status,
                    "items": items,
                    "run_id": run_id,
                    "updated_at": func.now(),
                },
            )
            .returning(self.model)
        )
        return self._one(statement)


class ConsumerOffsetRepository(BaseRepository[EtlConsumerOffset]):
    model = EtlConsumerOffset

    def offset(
        self, consumer_group: str, stream: str, shard: str = "0"
    ) -> EtlConsumerOffset | None:
        return self.get(consumer_group, stream, shard)

    def save(
        self,
        *,
        consumer_group: str,
        stream: str,
        shard: str = "0",
        last_event_id: str | None = None,
        last_event_time: datetime | None = None,
    ) -> EtlConsumerOffset:
        statement = (
            pg_insert(self.model)
            .values(
                consumer_group=consumer_group,
                stream=stream,
                shard=shard,
                last_event_id=last_event_id,
                last_event_time=last_event_time,
            )
            .on_conflict_do_update(
                index_elements=[
                    self.model.consumer_group,
                    self.model.stream,
                    self.model.shard,
                ],
                set_={
                    "last_event_id": last_event_id,
                    "last_event_time": last_event_time,
                    "updated_at": func.now(),
                },
            )
            .returning(self.model)
        )
        return self._one(statement)


class DlqRepository(BaseRepository[EtlDlq]):
    model = EtlDlq

    def enqueue(
        self,
        *,
        stream: str,
        reason: str,
        raw_event: dict[str, Any],
        event_id: str | None = None,
        entry_id: str | None = None,
        error_detail: str | None = None,
    ) -> EtlDlq:
        return self.add(
            event_id=event_id,
            stream=stream,
            entry_id=entry_id,
            reason=reason,
            raw_event=raw_event,
            error_detail=error_detail,
        )

    def pending(self, *, limit: int = 100) -> list[EtlDlq]:
        statement = (
            select(self.model)
            .where(self.model.status == "pending")
            .order_by(self.model.created_at, self.model.id)
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def find_pending(self, dlq_id: int) -> EtlDlq | None:
        statement = select(self.model).where(
            self.model.id == dlq_id, self.model.status == "pending"
        )
        return self._one_or_none(statement)

    def find_pending_by_event(self, event_id: str) -> EtlDlq | None:
        """Return the newest pending entry for ``event_id``, if any."""

        statement = (
            select(self.model)
            .where(self.model.event_id == event_id, self.model.status == "pending")
            .order_by(self.model.id.desc())
            .limit(1)
        )
        return self._one_or_none(statement)

    def find_pending_by_entry(self, stream: str, entry_id: str) -> EtlDlq | None:
        """Return the pending entry already recorded for a stream message.

        Used to make failing a message idempotent: a redelivery after a
        successful enqueue that failed to acknowledge must not create a second
        DLQ row.
        """

        statement = (
            select(self.model)
            .where(
                self.model.stream == stream,
                self.model.entry_id == entry_id,
                self.model.status == "pending",
            )
            .order_by(self.model.id.desc())
            .limit(1)
        )
        return self._one_or_none(statement)

    def latest(self, *, limit: int = 1) -> list[EtlDlq]:
        statement = select(self.model).order_by(self.model.created_at.desc()).limit(limit)
        return list(self.session.scalars(statement))

    def depth(self) -> int:
        statement = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.status == "pending")
        )
        return int(self.session.scalar(statement) or 0)

    def record_retry(self, dlq_id: int, *, error: str | None = None) -> EtlDlq | None:
        """Count one retry; ``error=None`` marks the entry as recovered."""

        values: dict[str, Any] = {
            "retry_count": self.model.retry_count + 1,
            "status": "pending" if error is not None else "retried",
        }
        if error is not None:
            values["error_detail"] = error
        statement = (
            update(self.model)
            .where(self.model.id == dlq_id)
            .values(**values)
            .returning(self.model)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return self._one_or_none(statement)

    def mark_retried(self, dlq_id: int) -> EtlDlq | None:
        statement = (
            update(self.model)
            .where(self.model.id == dlq_id)
            .values(status="retried", retry_count=self.model.retry_count + 1)
            .returning(self.model)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return self._one_or_none(statement)

    def purge(self, dlq_id: int) -> EtlDlq | None:
        statement = (
            update(self.model)
            .where(self.model.id == dlq_id)
            .values(status="purged")
            .returning(self.model)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return self._one_or_none(statement)


class ArchiveJobRepository(BaseRepository[ArchiveJob]):
    model = ArchiveJob

    def create(
        self, *, table_name: str, object_key: str, symbol: str | None = None
    ) -> ArchiveJob:
        return self.add(table_name=table_name, object_key=object_key, symbol=symbol)

    def mark_running(self, job_id: int) -> ArchiveJob | None:
        statement = (
            update(self.model)
            .where(self.model.id == job_id)
            .values(
                status="running",
                attempts=self.model.attempts + 1,
                started_at=func.now(),
            )
            .returning(self.model)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return self._one_or_none(statement)

    def mark_done(
        self, job_id: int, *, rows_archived: int, bytes_archived: int
    ) -> ArchiveJob | None:
        statement = (
            update(self.model)
            .where(self.model.id == job_id)
            .values(
                status="done",
                rows_archived=rows_archived,
                bytes_archived=bytes_archived,
                finished_at=func.now(),
            )
            .returning(self.model)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return self._one_or_none(statement)

    def mark_failed(self, job_id: int, error: str) -> ArchiveJob | None:
        statement = (
            update(self.model)
            .where(self.model.id == job_id)
            .values(status="failed", error=error, finished_at=func.now())
            .returning(self.model)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return self._one_or_none(statement)
