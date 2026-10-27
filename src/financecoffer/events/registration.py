"""Idempotent registration of validated events in ``ops.etl_event_log``.

At-least-once delivery means the same event arrives more than once. The event
log is the authoritative gate: the primary key on ``event_id`` and the
``uq_etl_event_identity`` constraint on ``(provider, data_type, symbol,
biz_key)`` (``NULLS NOT DISTINCT``) make the database -- not application code --
decide whether an event is new. A repeat is not an error; it returns
``duplicate=true`` and bumps the row's ``attempts`` counter so redelivery
storms are visible.

Two distinct repeats exist and are reported separately:

* ``reason="event_id"`` -- the same deterministic id was delivered again; the
  normal outbox-retry / replay case;
* ``reason="biz_key"`` -- a *different* id arrived for a business fact already
  registered, which means the publisher violated the deterministic-id rule and
  needs a fix upstream.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from financecoffer.core.errors import StorageError
from financecoffer.db.repositories.ops import EventLogRepository
from financecoffer.events.validation import ValidatedEvent

_log = logging.getLogger(__name__)

DuplicateReason = Literal["new", "event_id", "biz_key"]


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of one ``etl_event_log`` registration."""

    event_id: str
    duplicate: bool
    reason: DuplicateReason
    status: str
    attempts: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "duplicate": self.duplicate,
            "reason": self.reason,
            "status": self.status,
            "attempts": self.attempts,
        }


def register_event(session: Session, event: ValidatedEvent) -> RegistrationResult:
    """Register ``event`` once; a duplicate is reported, never raised.

    The caller owns the transaction boundary (``Database.session_scope``).
    """

    repository = EventLogRepository(session)
    row = repository.claim(
        event.event_id,
        provider=event.provider,
        data_type=event.data_type.value,
        biz_key=event.biz_key,
        symbol=event.symbol,
        event_time=event.event_time,
        trace_id=event.trace_id,
    )
    if row is not None:
        return RegistrationResult(
            event_id=row.event_id,
            duplicate=False,
            reason="new",
            status=row.status,
            attempts=row.attempts,
        )

    existing = repository.get(event.event_id)
    if existing is not None:
        touched = repository.record_duplicate_delivery(existing.event_id) or existing
        return RegistrationResult(
            event_id=touched.event_id,
            duplicate=True,
            reason="event_id",
            status=touched.status,
            attempts=touched.attempts,
        )

    identity = repository.find_by_identity(
        provider=event.provider,
        data_type=event.data_type.value,
        symbol=event.symbol,
        biz_key=event.biz_key,
    )
    if identity is not None:
        _log.warning(
            "event %s reuses the business identity of %s (provider=%s, data_type=%s, "
            "symbol=%s, biz_key=%s); the publisher must emit deterministic ids",
            event.event_id,
            identity.event_id,
            event.provider,
            event.data_type.value,
            event.symbol,
            event.biz_key,
        )
        touched = repository.record_duplicate_delivery(identity.event_id) or identity
        return RegistrationResult(
            event_id=touched.event_id,
            duplicate=True,
            reason="biz_key",
            status=touched.status,
            attempts=touched.attempts,
        )

    raise StorageError(
        "event log insert was skipped without a matching row",
        details={"event_id": event.event_id},
    )
