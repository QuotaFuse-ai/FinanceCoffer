"""Turn one stream entry into ODS and DWD rows inside a single transaction.

The pipeline mirrors ``fcoffer ingest event`` exactly -- validate, register in
``ops.etl_event_log``, append to ODS, write the DWD fact -- and adds the
consumer's bookkeeping (``begin`` / ``succeed``) so a delivered event is
visible in ``etl status``. The caller owns the transaction:

* a success commits event-log status, ODS row and DWD row together;
* a failure rolls all of them back, so a poisoned event never leaves a dirty
  ODS row behind and the event log does not claim it was processed.

Duplicates are reported, not raised. ``force`` is reserved for ``etl dlq
retry``: it lets a previously failed event be written again, but never past a
``done`` event and never past a business identity owned by another event id.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from financecoffer.core.errors import (
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
    NotImplementedYetError,
    StorageError,
)
from financecoffer.db.repositories.ops import EventLogRepository
from financecoffer.dwd import write_event
from financecoffer.events import register_event, validate_event
from financecoffer.events.registration import RegistrationResult
from financecoffer.ods import land_event

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


_REASON_MAX = 100


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


def dump_payload(payload: Mapping[str, Any]) -> str:
    """Serialize an operator-supplied payload for ODS ``raw_text``."""

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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

    event = validate_event(payload, compliance_enabled=compliance_enabled)
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
    "PAYLOAD_FIELD",
    "ProcessResult",
    "classify_failure",
    "dump_payload",
    "event_id_hint",
    "parse_entry_fields",
    "process_payload",
]
