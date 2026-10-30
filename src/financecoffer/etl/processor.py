"""Turn one stream entry into ODS and DWD rows inside a single transaction.

The pipeline mirrors ``fcoffer ingest event`` exactly -- validate, register in
``ops.etl_event_log``, append to ODS, write the DWD fact -- and adds the
consumer's bookkeeping (``begin`` / ``succeed``) so a delivered event is
visible in ``etl status``. The caller owns the transaction:

* a success commits event-log status, ODS row and DWD row together;
* a failure rolls all of them back, so a poisoned event never leaves a dirty
  ODS row behind and the event log does not claim it was processed.

``heartbeat`` is the exception: contract 3.8 says it is liveness state, not
history, so it is upserted into ``ops.source_heartbeat`` and never lands in
ODS, DWD or DWS.

Duplicates are reported, not raised. ``force`` is reserved for ``etl dlq
retry``: it lets a previously failed event be written again, but never past a
``done`` event and never past a business identity owned by another event id.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from financecoffer.core.errors import (
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
    NotImplementedYetError,
    StorageError,
)
from financecoffer.db.repositories.ops import EventLogRepository, HeartbeatRepository
from financecoffer.dwd import write_event
from financecoffer.events import register_event, validate_event
from financecoffer.events.models import EventDataType, HeartbeatContent
from financecoffer.events.registration import RegistrationResult
from financecoffer.events.validation import ValidatedEvent
from financecoffer.ods import land_event, resolve_source

_log = logging.getLogger(__name__)

PAYLOAD_FIELD = "payload"

WriteStatus = Literal["written", "duplicate"]


@dataclass(frozen=True)
class ProcessResult:
    """What one entry produced, as counted by the consumer and by the CLI."""

    status: WriteStatus
    event_id: str
    reason: str
    event_log_status: str
    ods_id: int | None = None
    dwd_table: str | None = None
    dwd_version: int | None = None
    dwd_id: int | None = None

    @property
    def written(self) -> bool:
        return self.status == "written"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "event_id": self.event_id,
            "reason": self.reason,
            "event_log_status": self.event_log_status,
            "ods": {"id": self.ods_id} if self.ods_id is not None else None,
            "dwd": (
                {
                    "table": self.dwd_table,
                    "version": self.dwd_version,
                    "id": self.dwd_id,
                }
                if self.dwd_table is not None
                else None
            ),
        }


@dataclass(frozen=True)
class FailureInfo:
    """A classified failure: what to record, and whether a retry can help."""

    reason: str
    message: str
    retryable: bool
    details: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True)
class HeartbeatResult:
    """The ``ops.source_heartbeat`` row one heartbeat event refreshed."""

    source: str
    last_seen: datetime
    status: str
    items: int
    run_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "last_seen": self.last_seen.astimezone(UTC).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
            "status": self.status,
            "items": self.items,
            "run_id": self.run_id,
        }


_REASON_MAX = 100

# ``ops.etl_event_log`` column widths: a rejected payload is truncated to fit
# rather than allowed to fail the bookkeeping insert.
_EVENT_ID_MAX = 100
_PROVIDER_MAX = 50
_DATA_TYPE_MAX = 50
_BIZ_KEY_MAX = 200
_SYMBOL_MAX = 20
_UNKNOWN_PROVIDER = "unknown"
_UNKNOWN_DATA_TYPE = "unknown"


def classify_failure(exc: BaseException) -> FailureInfo:
    """Map an exception to a DLQ ``reason`` and retryability.

    Validation and contract failures are deterministic: the same bytes will
    fail again, so retrying only delays the DLQ. Storage, dependency and
    unexpected errors may be transient and are retried up to the configured
    attempt limit.
    """

    if isinstance(exc, InvalidArgumentError):
        details = exc.details or {}
        hint = details.get("reason")
        reason = hint if isinstance(hint, str) and hint else "validation"
        return FailureInfo(
            reason=reason[:_REASON_MAX],
            message=exc.message,
            retryable=False,
            details=details,
        )
    if isinstance(exc, NotImplementedYetError):
        return FailureInfo(reason="unsupported", message=exc.message, retryable=False)
    if isinstance(exc, StorageError):
        return FailureInfo("storage", exc.message, True, exc.details)
    if isinstance(exc, DependencyError):
        return FailureInfo("dependency", exc.message, True, exc.details)
    if isinstance(exc, FinanceCofferError):
        return FailureInfo(exc.code.value.lower()[:_REASON_MAX], exc.message, False, exc.details)
    if isinstance(exc, SQLAlchemyError):
        return FailureInfo("storage", "database operation failed", True)
    return FailureInfo("internal", "unexpected processing error", True)


def parse_entry_fields(fields: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    """Split a stream entry into the parsed payload and its exact bytes.

    The ``payload`` field carries the whole event document as a JSON string.
    That string -- not a re-serialization -- is what ODS stores as ``raw_text``,
    so ``ods show --raw`` returns exactly what upstream published.
    """

    raw = fields.get(PAYLOAD_FIELD)
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidArgumentError(
            "stream entry has no payload field",
            details={"reason": "payload", "field": PAYLOAD_FIELD},
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidArgumentError(
            "stream payload is not valid JSON",
            details={
                "reason": "payload",
                "field": PAYLOAD_FIELD,
                "error": f"line {exc.lineno} column {exc.colno}: {exc.msg}",
            },
        ) from exc
    if not isinstance(payload, Mapping):
        raise InvalidArgumentError(
            "stream payload must be a JSON object",
            details={
                "reason": "payload",
                "field": PAYLOAD_FIELD,
                "type": type(payload).__name__,
            },
        )
    return payload, raw


def event_id_hint(payload: Mapping[str, Any] | None) -> str | None:
    """Best-effort event id for failure bookkeeping; never raises."""

    if not payload:
        return None
    value = payload.get("event_id")
    return value if isinstance(value, str) and value.strip() else None


def rejection_event_id(payload: Mapping[str, Any], raw_text: str) -> str:
    """The id a rejected payload is booked under.

    The payload's own ``event_id`` wins so an operator can match the failure
    against what upstream sent. A payload too broken to carry one gets a
    deterministic synthetic id, so the same bytes do not create a new row on
    every redelivery.
    """

    hinted = event_id_hint(payload)
    if hinted is not None:
        return hinted[:_EVENT_ID_MAX]
    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:32]
    return f"invalid_{digest}"


def _text_field(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def rejection_identity(payload: Mapping[str, Any], event_id: str) -> dict[str, Any]:
    """Columns a rejected payload fills, with explicit placeholders.

    Validation failed, so nothing here is trusted; the placeholders keep the
    row insertable (``provider`` / ``data_type`` / ``biz_key`` are NOT NULL)
    without pretending the payload got that far.
    """

    source = payload.get("source_info")
    provider = _text_field(source.get("provider")) if isinstance(source, Mapping) else None
    data_type = _text_field(payload.get("data_type"))
    biz_key = _text_field(payload.get("biz_key"))
    symbol = _text_field(payload.get("symbol"))
    return {
        "provider": (provider or _UNKNOWN_PROVIDER)[:_PROVIDER_MAX],
        "data_type": (data_type or _UNKNOWN_DATA_TYPE)[:_DATA_TYPE_MAX],
        "biz_key": (biz_key or f"unvalidated:{event_id}")[:_BIZ_KEY_MAX],
        "symbol": symbol[:_SYMBOL_MAX] if symbol is not None else None,
    }


def record_rejection(
    session: Session,
    *,
    payload: Mapping[str, Any],
    raw_text: str,
    exc: InvalidArgumentError,
) -> None:
    """Persist a rejected payload's failure in its own transaction.

    The caller rolls the processing transaction back when an event fails, so
    the failure row is committed on a separate session -- otherwise the only
    trace of an invalid event would disappear with the rollback. Bookkeeping
    must never mask the validation error, so any failure here is logged and
    swallowed.
    """

    event_id = rejection_event_id(payload, raw_text)
    identity = rejection_identity(payload, event_id)
    details = exc.details or {}
    reason = details.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = "validation"
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    try:
        with Session(bind=engine) as failure_session:
            EventLogRepository(failure_session).record_failure(
                event_id,
                provider=identity["provider"],
                data_type=identity["data_type"],
                biz_key=identity["biz_key"],
                symbol=identity["symbol"],
                error_class=type(exc).__name__,
                reason=reason,
                error=exc.message,
            )
            failure_session.commit()
    except Exception as bookkeeping_exc:  # noqa: BLE001 - never mask the original error
        _log.warning(
            "could not record rejected event %s in ops.etl_event_log: %s",
            event_id,
            bookkeeping_exc,
        )


def dump_payload(payload: Mapping[str, Any]) -> str:
    """Serialize an operator-supplied payload for ODS ``raw_text``."""

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def record_heartbeat(
    session: Session, event: ValidatedEvent, *, source: str | None = None
) -> HeartbeatResult:
    """Upsert ``ops.source_heartbeat`` for one heartbeat event.

    ``last_seen`` is the round's ``finished_at`` when the publisher sent one
    (the moment the hub was last alive) and ``event_time`` otherwise. The row
    is keyed by ``source``, so a heartbeat overwrites the previous state
    instead of accumulating history -- that is why replaying it from a backfill
    is refused.
    """

    fields = cast(HeartbeatContent, event.content).structured_fields
    last_seen = fields.finished_at or event.event_time
    resolved = source or resolve_source(event)
    row = HeartbeatRepository(session).record(
        resolved,
        last_seen=last_seen,
        status=fields.status.value,
        items=fields.items_collected,
        run_id=fields.run_id,
    )
    return HeartbeatResult(
        source=row.source,
        last_seen=row.last_seen,
        status=row.status,
        items=row.items,
        run_id=row.run_id,
    )


def process_payload(
    session: Session,
    *,
    payload: Mapping[str, Any],
    raw_text: str,
    compliance_enabled: bool,
    force: bool = False,
) -> ProcessResult:
    """Validate, register, land and write one event inside ``session``.

    Raises :class:`~financecoffer.core.errors.FinanceCofferError` (or a
    ``SQLAlchemyError``) on failure; the caller rolls back and decides between a
    retry and the dead-letter queue.
    """

    try:
        event = validate_event(payload, compliance_enabled=compliance_enabled)
    except InvalidArgumentError as exc:
        # This row is the only trace of the rejection: the caller's transaction
        # is rolled back, taking any in-transaction insert with it.
        record_rejection(session, payload=payload, raw_text=raw_text, exc=exc)
        raise
    registration = register_event(session, event)
    if registration.duplicate and not _reprocess_allowed(registration, force=force):
        _log.info(
            "event %s already registered (status=%s, reason=%s); skipping",
            registration.event_id,
            registration.status,
            registration.reason,
        )
        return ProcessResult(
            status="duplicate",
            event_id=registration.event_id,
            reason=registration.reason,
            event_log_status=registration.status,
        )

    event_log = EventLogRepository(session)
    event_log.begin(registration.event_id)
    if event.data_type is EventDataType.HEARTBEAT:
        record_heartbeat(session, event)
        event_log.succeed(registration.event_id)
        return ProcessResult(
            status="written",
            event_id=registration.event_id,
            reason=registration.reason,
            event_log_status="done",
        )

    landing = land_event(session, event, raw_text=raw_text)
    fact = write_event(session, event, compliance_enabled=compliance_enabled)
    event_log.succeed(registration.event_id)
    return ProcessResult(
        status="written",
        event_id=registration.event_id,
        reason=registration.reason,
        event_log_status="done",
        ods_id=landing.record_id,
        dwd_table=fact.table,
        dwd_version=fact.version,
        dwd_id=fact.row_id,
    )


def _reprocess_allowed(registration: RegistrationResult, *, force: bool) -> bool:
    if not force:
        return False
    if registration.reason == "biz_key":
        # A different event id owns this business fact; writing would create a
        # second version of the same fact under a broken publisher side.
        return False
    return registration.status != "done"


__all__ = [
    "FailureInfo",
    "HeartbeatResult",
    "PAYLOAD_FIELD",
    "ProcessResult",
    "classify_failure",
    "dump_payload",
    "event_id_hint",
    "parse_entry_fields",
    "process_payload",
    "record_heartbeat",
]
