"""Upstream event contract: models, validation and idempotent registration.

The package is the single entry point for incoming events: :func:`validate_event`
checks a payload against ``docs/specs/event-contract.md`` (v0.3) and
:func:`register_event` records it once in ``ops.etl_event_log``. Persisting the
payload into ODS is deliberately out of scope (T-005 consumes ``ValidatedEvent``).
"""

from __future__ import annotations

from financecoffer.events.models import (
    MAX_EVENT_BYTES,
    SUPPORTED_SCHEMA_VERSION,
    AnnouncementContent,
    BaseContent,
    Confidence,
    EventDataType,
    EventEnvelope,
    FinancialContent,
    KlineContent,
    Market,
    QualityMetrics,
    QualityStatus,
    QuoteContent,
    RiskContent,
    SourceInfo,
)
from financecoffer.events.registration import RegistrationResult, register_event
from financecoffer.events.validation import ValidatedEvent, compute_event_id, validate_event

__all__ = [
    "MAX_EVENT_BYTES",
    "SUPPORTED_SCHEMA_VERSION",
    "AnnouncementContent",
    "BaseContent",
    "Confidence",
    "EventDataType",
    "EventEnvelope",
    "FinancialContent",
    "KlineContent",
    "Market",
    "QualityMetrics",
    "QualityStatus",
    "QuoteContent",
    "RegistrationResult",
    "RiskContent",
    "SourceInfo",
    "ValidatedEvent",
    "compute_event_id",
    "register_event",
    "validate_event",
]
