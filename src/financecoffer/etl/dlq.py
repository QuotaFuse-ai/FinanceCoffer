"""Dead-letter queue: persist failures, replay them on demand.

A failed entry is written twice, on purpose:

* ``fc:stockdata:dlq`` keeps the original message so the loss is visible to
  anything watching Redis, and the consumer can be told to re-read it;
* ``ops.etl_dlq`` keeps the audit record -- source stream, entry id, reason,
  error text, attempt count -- so ``etl dlq list`` and ``etl dlq retry`` work
  without Redis and survive a stream trim.

The stream entry is written first: if the database insert then fails, the
message is left unacknowledged and redelivered, and the pending-row check makes
that redelivery idempotent instead of writing a second copy.

Retrying processes the stored payload inline, through the same transaction path
as normal consumption, so an operator learns immediately whether the fix
worked.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from financecoffer.core.config import Config
from financecoffer.core.errors import NotFoundError
from financecoffer.db.models.ops import EtlDlq
from financecoffer.db.repositories.ops import DlqRepository, EventLogRepository
from financecoffer.db.session import Database
from financecoffer.etl.processor import (
    PAYLOAD_FIELD,
    FailureInfo,
    classify_failure,
    dump_payload,
    process_payload,
)

_log = logging.getLogger(__name__)

DLQ_LIST_FIELDS: tuple[str, ...] = (
    "payload",
    "event_id",
    "reason",
    "error",
    "attempts",
    "source_stream",
    "source_entry_id",
    "failed_at",
    "dlq_stream",
    "dlq_entry_id",
)


@dataclass(frozen=True)
class DlqRecord:
    """One dead-letter row, as reported by ``etl consume`` / ``etl dlq``."""

    dlq_id: int
    event_id: str | None
    reason: str
    attempts: int
    stream_entry_id: str | None
    dirty: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.dlq_id,
            "event_id": self.event_id,
            "reason": self.reason,
            "attempts": self.attempts,
            "dlq_stream_entry": self.stream_entry_id,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def entry_fields(entry_id: str, fields: Mapping[str, Any]) -> dict[str, str]:
    """Copy the stream entry into the string map ``XADD`` accepts."""

    record: dict[str, str] = {}
    for key, value in fields.items():
        record[str(key)] = value if isinstance(value, str) else json.dumps(value, default=str)
    record["source_entry_id"] = entry_id
    return record


def enqueue_failure(
    *,
    client: Any,
    database: Database,
    config: Config,
    source_stream: str,
    entry_id: str,
    fields: Mapping[str, Any],
    failure: FailureInfo,
    attempts: int,
    event_id: str | None = None,
) -> DlqRecord:
    """Record ``entry_id`` in both DLQs; safe to call twice for one entry."""

    with database.session_scope() as session:
        existing = DlqRepository(session).find_pending_by_entry(source_stream, entry_id)
        if existing is not None:
            return _to_record(existing)

    payload = fields.get(PAYLOAD_FIELD)
    raw_event: dict[str, Any] = {
        "payload": payload if isinstance(payload, str) else None,
        "fields": entry_fields(entry_id, fields),
        "source_stream": source_stream,
        "source_entry_id": entry_id,
        "reason": failure.reason,
        "error": failure.message,
        "attempts": attempts,
        "failed_at": _now(),
        "dlq_stream": config.etl_dlq_stream,
    }
    stream_entry_id = client.xadd(
        config.etl_dlq_stream,
        {
            "event_id": event_id or "",
            "reason": failure.reason,
            "error": failure.message[:_MESSAGE_MAX],
            "attempts": str(attempts),
            "source_stream": source_stream,
            "source_entry_id": entry_id,
            "failed_at": raw_event["failed_at"],
            "payload": raw_event["payload"] or "",
        },
    )
    raw_event["dlq_entry_id"] = stream_entry_id

    with database.session_scope() as session:
        row = DlqRepository(session).enqueue(
            stream=source_stream,
            reason=failure.reason,
            raw_event=raw_event,
            event_id=event_id,
            entry_id=entry_id,
            error_detail=failure.message,
        )
        if event_id:
            EventLogRepository(session).to_dlq(event_id, failure.message)
        _log.warning(
            "event %s moved to DLQ (%s) after %d attempt(s): %s",
            event_id or entry_id,
            failure.reason,
            attempts,
            failure.message,
        )
        return DlqRecord(
            dlq_id=row.id,
            event_id=row.event_id,
            reason=row.reason,
            attempts=attempts,
            stream_entry_id=stream_entry_id,
            dirty=True,
        )


_MESSAGE_MAX = 2000


def _to_record(row: EtlDlq) -> DlqRecord:
    raw = row.raw_event or {}
    return DlqRecord(
        dlq_id=row.id,
        event_id=row.event_id,
        reason=row.reason,
        attempts=int(raw.get("attempts") or 0),
        stream_entry_id=raw.get("dlq_entry_id"),
        dirty=False,
    )


def list_entries(database: Database, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return pending dead letters, oldest first."""

    with database.session_scope() as session:
        rows = DlqRepository(session).pending(limit=limit)
        return [_describe(row) for row in rows]


def _describe(row: EtlDlq) -> dict[str, Any]:
    raw = row.raw_event or {}
    return {
        "id": row.id,
        "event_id": row.event_id,
        "reason": row.reason,
        "status": row.status,
        "retry_count": row.retry_count,
        "source_stream": row.stream,
        "source_entry_id": row.entry_id,
        "attempts": raw.get("attempts"),
        "error": row.error_detail,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "payload": raw.get("payload"),
    }


def retry_dlq(
    *,
    client: Any,
    database: Database,
    config: Config,
    dlq_id: int | None = None,
    event_id: str | None = None,
    all_pending: bool = False,
    payload: Mapping[str, Any] | None = None,
    payload_text: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Replay one dead letter (or every pending one) through the write path.

    ``payload`` overrides the stored document, which is how ``etl dlq retry
    --file`` applies an upstream fix. A successful replay removes the entry
    from the Redis DLQ and marks the database row ``retried``; a failed replay
    keeps it pending and bumps ``retry_count``.
    """

    if payload is not None and (dlq_id is None and event_id is None):
        raise NotFoundError(
            "a corrected payload needs exactly one dlq id or event id",
            details={"field": "id"},
        )
    if payload is not None and all_pending:
        raise NotFoundError(
            "a corrected payload cannot be applied to every pending entry",
            details={"field": "all"},
        )

    with database.session_scope() as session:
        repository = DlqRepository(session)
        if all_pending:
            rows = repository.pending(limit=limit)
        elif dlq_id is not None:
            row = repository.find_pending(dlq_id)
            if row is None:
                raise NotFoundError(
                    "dead letter not found or already resolved",
                    details={"id": dlq_id, "hint": "run `fcoffer etl dlq list`"},
                )
            rows = [row]
        else:
            assert event_id is not None
            row = repository.find_pending_by_event(event_id)
            if row is None:
                raise NotFoundError(
                    "no pending dead letter for this event",
                    details={"event_id": event_id, "hint": "run `fcoffer etl dlq list`"},
                )
            rows = [row]
        targets = [(row.id, dict(row.raw_event or {})) for row in rows]

    results = [
        _retry_one(
            client=client,
            database=database,
            config=config,
            dlq_id=target_id,
            raw_event=raw_event,
            payload=payload,
            payload_text=payload_text,
        )
        for target_id, raw_event in targets
    ]
    counts = {"retried": 0, "duplicate": 0, "failed": 0}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    return {"requested": len(results), **counts, "results": results}


def _retry_one(
    *,
    client: Any,
    database: Database,
    config: Config,
    dlq_id: int,
    raw_event: Mapping[str, Any],
    payload: Mapping[str, Any] | None,
    payload_text: str | None,
) -> dict[str, Any]:
    stored = raw_event.get("payload")
    if payload is not None:
        document = payload
        text = payload_text if payload_text is not None else dump_payload(payload)
    else:
        if not isinstance(stored, str) or not stored.strip():
            return _finish_failure(
                database=database,
                dlq_id=dlq_id,
                error="dead letter carries no payload",
            )
        try:
            parsed = json.loads(stored)
        except json.JSONDecodeError as exc:
            return _finish_failure(
                database=database, dlq_id=dlq_id, error=f"stored payload is not JSON: {exc.msg}"
            )
        if not isinstance(parsed, Mapping):
            return _finish_failure(
                database=database, dlq_id=dlq_id, error="stored payload is not a JSON object"
            )
        document = parsed
        text = stored

    try:
        with database.session_scope() as session:
            result = process_payload(
                session,
                payload=document,
                raw_text=text,
                compliance_enabled=config.compliance_enabled,
                force=True,
            )
    except Exception as exc:  # noqa: BLE001 - every failure is reported per entry
        failure = classify_failure(exc)
        return _finish_failure(database=database, dlq_id=dlq_id, error=failure.message)

    with database.session_scope() as session:
        DlqRepository(session).record_retry(dlq_id, error=None)
    stream_entry = raw_event.get("dlq_entry_id")
    if isinstance(stream_entry, str) and stream_entry:
        try:
            client.xdel(config.etl_dlq_stream, stream_entry)
        except Exception as exc:  # noqa: BLE001 - the database row already moved on
            _log.warning(
                "could not remove %s from %s: %s",
                stream_entry,
                config.etl_dlq_stream,
                exc,
            )

    status = "retried" if result.written else "duplicate"
    return {
        "id": dlq_id,
        "status": status,
        "event_id": result.event_id,
        "detail": result.as_dict(),
    }


def _finish_failure(*, database: Database, dlq_id: int, error: str) -> dict[str, Any]:
    with database.session_scope() as session:
        DlqRepository(session).record_retry(dlq_id, error=error)
    return {"id": dlq_id, "status": "failed", "error": error}


__all__ = [
    "DLQ_LIST_FIELDS",
    "DlqRecord",
    "enqueue_failure",
    "entry_fields",
    "list_entries",
    "retry_dlq",
]
