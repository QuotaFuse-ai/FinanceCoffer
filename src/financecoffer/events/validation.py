"""Executable form of ``docs/specs/event-contract.md`` (v0.3).

``validate_event`` turns one upstream payload into a :class:`ValidatedEvent` or
raises :class:`~financecoffer.core.errors.InvalidArgumentError` whose
``details.errors`` lists one ``{field, reason}`` per problem, using the same
field naming as the REST layer (``content.structured_fields.amount_revenue``,
never ``body.``-prefixed). ``details.reason`` carries the primary bucket the
contract uses for the dirty-data counter: ``schema``, ``event_id`` or
``compliance``.

Nothing here computes business values: no biz_key is derived from the content,
no quality status is filled in, and the raw payload is kept as given so the ODS
layer (T-005) stores exactly what upstream sent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from financecoffer.core.compliance import ComplianceMark, extract_mark
from financecoffer.core.errors import InvalidArgumentError
from financecoffer.events.models import (
    MAX_EVENT_BYTES,
    AnnouncementContent,
    BaseContent,
    EventDataType,
    EventEnvelope,
    FinancialContent,
    HeartbeatContent,
    KlineContent,
    Market,
    NorthboundContent,
    QuoteContent,
    ResearchContent,
    RiskContent,
    SourceInfo,
)
from financecoffer.query import normalize_symbol

_CONTENT_MODELS: dict[EventDataType, type[BaseContent]] = {
    EventDataType.FINANCIAL: FinancialContent,
    EventDataType.ANNOUNCEMENT: AnnouncementContent,
    EventDataType.KLINE: KlineContent,
    EventDataType.QUOTE: QuoteContent,
    EventDataType.RISK: RiskContent,
    EventDataType.RESEARCH: ResearchContent,
    EventDataType.NORTHBOUND: NorthboundContent,
    EventDataType.HEARTBEAT: HeartbeatContent,
}

#: Contract section 4: these payloads describe a market or a crawler round, so
#: they legitimately carry ``symbol=null``; every other type requires one.
_SYMBOL_OPTIONAL_TYPES = frozenset({EventDataType.NORTHBOUND, EventDataType.HEARTBEAT})

_REASON_SCHEMA = "schema"
_REASON_EVENT_ID = "event_id"
_REASON_COMPLIANCE = "compliance"

Problem = dict[str, str]


@dataclass(frozen=True)
class ValidatedEvent:
    """A payload that passed validation, ready for idempotent registration.

    ``raw`` is the untouched input; T-005 persists it as-is. ``compliance`` is
    ``None`` whenever the switch is off, which is also when the compliance
    fields were never read.
    """

    schema_version: str
    event_id: str
    biz_key: str
    event_time: datetime
    data_type: EventDataType
    symbol: str | None
    market: Market
    source_info: SourceInfo
    content: BaseContent
    trace_id: str | None
    compliance: ComplianceMark | None
    raw: Mapping[str, Any]

    @property
    def provider(self) -> str:
        return self.source_info.provider

    def summary(self) -> dict[str, Any]:
        """Envelope-level view for ``ingest event`` output; content is omitted."""

        data: dict[str, Any] = {
            "event_id": self.event_id,
            "biz_key": self.biz_key,
            "data_type": self.data_type.value,
            "symbol": self.symbol,
            "market": self.market.value,
            "event_time": _rfc3339(self.event_time),
            "provider": self.provider,
            "quality_status": self.content.quality_metrics.quality_status.value,
        }
        if self.compliance is not None:
            data["compliance"] = self.compliance.as_dict()
        return data


def compute_event_id(
    *,
    provider: str,
    data_type: EventDataType | str,
    symbol: str | None,
    biz_key: str,
) -> str:
    """Return the deterministic id from contract section 2."""

    value = data_type.value if isinstance(data_type, EventDataType) else data_type
    material = f"{provider}|{value}|{symbol or ''}|{biz_key}"
    return "evt_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def validate_event(
    payload: Mapping[str, Any],
    *,
    compliance_enabled: bool = False,
) -> ValidatedEvent:
    """Validate one event payload and return its normalized form.

    Raises ``InvalidArgumentError`` with ``details.errors`` when the payload
    breaks the contract; every ``data_type`` in the contract has a model.
    """

    raw = dict(payload)
    try:
        envelope = EventEnvelope.model_validate(raw)
    except ValidationError as exc:
        raise InvalidArgumentError(
            "event does not match the event contract",
            details={"errors": _problems_from(exc), "reason": _REASON_SCHEMA},
        ) from exc

    problems: list[Problem] = []
    problems.extend(_check_symbol(envelope))
    problems.extend(_check_size(raw, envelope.content))
    content = _parse_content(envelope, problems)
    if problems or content is None:
        raise InvalidArgumentError(
            "event does not match the event contract",
            details={"errors": problems, "reason": _REASON_SCHEMA},
        )

    expected = compute_event_id(
        provider=envelope.source_info.provider,
        data_type=envelope.data_type,
        symbol=envelope.symbol,
        biz_key=envelope.biz_key,
    )
    if envelope.event_id != expected:
        raise InvalidArgumentError(
            "event_id does not match sha256(provider|data_type|symbol|biz_key)",
            details={
                "errors": [
                    {
                        "field": "event_id",
                        "reason": (
                            "does not match sha256(provider|data_type|symbol|biz_key); "
                            "the same business fact must always produce the same id"
                        ),
                    }
                ],
                "reason": _REASON_EVENT_ID,
            },
        )

    compliance = _compliance_mark(raw.get("source_info"), enabled=compliance_enabled)

    return ValidatedEvent(
        schema_version=envelope.schema_version,
        event_id=envelope.event_id,
        biz_key=envelope.biz_key,
        event_time=envelope.event_time,
        data_type=envelope.data_type,
        symbol=envelope.symbol,
        market=envelope.market,
        source_info=envelope.source_info,
        content=content,
        trace_id=envelope.trace_id,
        compliance=compliance,
        raw=raw,
    )


def _parse_content(envelope: EventEnvelope, problems: list[Problem]) -> BaseContent | None:
    model = _CONTENT_MODELS[envelope.data_type]
    try:
        return model.model_validate(envelope.content)
    except ValidationError as exc:
        problems.extend(_problems_from(exc, prefix="content"))
        return None


def _check_symbol(envelope: EventEnvelope) -> list[Problem]:
    if not envelope.symbol:
        if envelope.data_type in _SYMBOL_OPTIONAL_TYPES:
            return []
        return [
            {
                "field": "symbol",
                "reason": f"symbol is required for data_type={envelope.data_type.value}",
            }
        ]
    try:
        canonical = normalize_symbol(envelope.symbol)
    except InvalidArgumentError as exc:
        return [{"field": "symbol", "reason": exc.message}]
    if canonical != envelope.symbol:
        return [{"field": "symbol", "reason": f"symbol must use its canonical form {canonical!r}"}]
    return []


def _check_size(raw: Mapping[str, Any], content: Mapping[str, Any]) -> list[Problem]:
    encoded = len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    if encoded <= MAX_EVENT_BYTES or _has_object_ref(content):
        return []
    return [
        {
            "field": "content",
            "reason": (
                f"event payload is {encoded} bytes, over the {MAX_EVENT_BYTES} byte limit; "
                "upload it to MinIO and pass content.object_ref"
            ),
        }
    ]


def _has_object_ref(content: Mapping[str, Any]) -> bool:
    structured = content.get("structured_fields")
    candidates = (
        content.get("object_ref"),
        structured.get("object_ref") if isinstance(structured, Mapping) else None,
    )
    return any(isinstance(candidate, str) and candidate.strip() for candidate in candidates)


def _compliance_mark(source_info: Any, *, enabled: bool) -> ComplianceMark | None:
    try:
        info = source_info if isinstance(source_info, Mapping) else None
        return extract_mark(info, enabled=enabled)
    except InvalidArgumentError as exc:
        field = (exc.details or {}).get("field", "source_info.compliance_tier")
        raise InvalidArgumentError(
            "source_info cannot be compliance-marked",
            details={
                "errors": [{"field": str(field), "reason": exc.message}],
                "reason": _REASON_COMPLIANCE,
            },
        ) from exc


def _problems_from(error: ValidationError, *, prefix: str | None = None) -> list[Problem]:
    problems: list[Problem] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"])
        if prefix:
            location = f"{prefix}.{location}" if location else prefix
        problems.append(
            {
                "field": location or "event",
                "reason": str(item["msg"]).removeprefix("Value error, "),
            }
        )
    return problems


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["ValidatedEvent", "compute_event_id", "validate_event"]
