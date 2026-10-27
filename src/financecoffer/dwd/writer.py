"""Map validated events onto DWD fact rows.

The writer is the only place where an event's payload becomes a DWD row:

* versioned facts (financial / kline / quote) append ``MAX(version) + 1`` for a
  business key; a replayed ``event_id`` is skipped and never bumps a version;
* announcements dedupe on both ``event_id`` and ``content_hash``;
* risk events are inserted once by ``event_id``;
* quality metrics are copied through untouched -- a ``bad`` event still lands;
* money stays :class:`~decimal.Decimal` end to end and is rejected when it is
  not exactly representable in the column's ``NUMERIC(p, 4)`` precision, so the
  database never rounds a value silently;
* compliance columns are written only when the switch is on (DEC-0010).

No code path here issues ``UPDATE`` or ``DELETE``: history is append-only and a
correction is a new version. The caller owns the transaction boundary, so the
DWD rows commit or roll back together with the ODS row and the event-log claim.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal, cast

from sqlalchemy.orm import Session

from financecoffer.core.compliance import columns as compliance_columns
from financecoffer.core.errors import InvalidArgumentError
from financecoffer.db.models.dwd import (
    StockAnnouncement,
    StockFinancialSnapshot,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)
from financecoffer.db.repositories.dwd import (
    AnnouncementRepository,
    FinancialSnapshotRepository,
    KlineRepository,
    QuoteSnapshotRepository,
    RiskEventRepository,
)
from financecoffer.events.models import (
    AnnouncementContent,
    EventDataType,
    FinancialContent,
    KlineContent,
    QuoteContent,
    RiskContent,
)
from financecoffer.events.validation import ValidatedEvent
from financecoffer.ods.landing import fetch_time_for

_log = logging.getLogger(__name__)

_SCALE = 4
_QUANTUM = Decimal("0.0001")
#: Enough precision to compare a 24-digit NUMERIC without touching its value.
_CHECK_PRECISION = 60

WriteStatus = Literal["inserted", "skipped"]


@dataclass(frozen=True)
class WriteOutcome:
    """Result of one DWD write, as reported to ``ingest event``."""

    status: WriteStatus
    table: str
    event_id: str
    version: int | None = None
    row_id: int | None = None

    @property
    def inserted(self) -> bool:
        return self.status == "inserted"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "table": self.table,
            "event_id": self.event_id,
            "version": self.version,
            "id": self.row_id,
        }


_Handler = Callable[[Session, ValidatedEvent, Mapping[str, Any]], WriteOutcome]


def write_event(
    session: Session,
    event: ValidatedEvent,
    *,
    compliance_enabled: bool,
) -> WriteOutcome:
    """Write ``event`` into its DWD table inside the caller's transaction.

    Raises :class:`InvalidArgumentError` (``E_INVALID_ARGUMENT``) for a
    ``data_type`` without a DWD table or an amount that cannot be stored
    exactly; the caller then rolls the whole ingest back.
    """

    handler = _WRITERS.get(event.data_type)
    if handler is None:
        raise InvalidArgumentError(
            f"no DWD table for data_type={event.data_type.value}",
            details={
                "field": "data_type",
                "value": event.data_type.value,
                "supported": sorted(item.value for item in _WRITERS),
            },
        )
    return handler(session, event, _compliance_values(event, compliance_enabled))


def _write_financial(
    session: Session, event: ValidatedEvent, compliance: Mapping[str, Any]
) -> WriteOutcome:
    content = cast(FinancialContent, event.content)
    fields = content.structured_fields
    row = FinancialSnapshotRepository(session).insert_version(
        symbol=_require_symbol(event),
        report_date=fields.report_date,
        report_type=fields.report_type,
        amount_revenue=_exact_numeric(
            fields.amount_revenue,
            precision=20,
            field_name="content.structured_fields.amount_revenue",
        ),
        amount_net_profit=_exact_numeric(
            fields.amount_net_profit,
            precision=20,
            field_name="content.structured_fields.amount_net_profit",
        ),
        balance_check_result=_balance_check_result(event),
        **_quality_values(event),
        fetch_time=fetch_time_for(event),
        event_id=event.event_id,
        provider=event.provider,
        **compliance,
    )
    return _outcome(StockFinancialSnapshot, event, row)


def _write_announcement(
    session: Session, event: ValidatedEvent, compliance: Mapping[str, Any]
) -> WriteOutcome:
    content = cast(AnnouncementContent, event.content)
    structured = content.structured_fields
    row = AnnouncementRepository(session).insert_once(
        symbol=_require_symbol(event),
        ann_date=_announcement_date(structured),
        ann_type=_optional_text(
            structured.get("ann_type"), field_name="content.structured_fields.ann_type"
        ),
        title=_optional_text(
            structured.get("title"), field_name="content.structured_fields.title"
        ),
        content_hash=_content_hash(content),
        clean_content=content.clean_content,
        minio_url=None,
        **_quality_values(event),
        fetch_time=fetch_time_for(event),
        event_id=event.event_id,
        provider=event.provider,
        **compliance,
    )
    return _outcome(StockAnnouncement, event, row)


def _write_kline(
    session: Session, event: ValidatedEvent, compliance: Mapping[str, Any]
) -> WriteOutcome:
    content = cast(KlineContent, event.content)
    fields = content.structured_fields
    row = KlineRepository(session).insert_version(
        symbol=_require_symbol(event),
        trade_date=fields.trade_date,
        adjust=fields.adjust,
        open=_exact_numeric(fields.open, precision=20, field_name="content.structured_fields.open"),
        high=_exact_numeric(fields.high, precision=20, field_name="content.structured_fields.high"),
        low=_exact_numeric(fields.low, precision=20, field_name="content.structured_fields.low"),
        close=_exact_numeric(
            fields.close, precision=20, field_name="content.structured_fields.close"
        ),
        volume=_exact_numeric(
            fields.volume, precision=24, field_name="content.structured_fields.volume"
        ),
        amount=_exact_numeric(
            fields.amount, precision=24, field_name="content.structured_fields.amount"
        ),
        **_quality_values(event),
        fetch_time=fetch_time_for(event),
        event_id=event.event_id,
        provider=event.provider,
        **compliance,
    )
    return _outcome(StockKline, event, row)


def _write_quote(
    session: Session, event: ValidatedEvent, compliance: Mapping[str, Any]
) -> WriteOutcome:
    content = cast(QuoteContent, event.content)
    fields = content.structured_fields
    row = QuoteSnapshotRepository(session).insert_version(
        symbol=_require_symbol(event),
        trade_date=fields.trade_date,
        close=_exact_numeric(
            fields.close, precision=20, field_name="content.structured_fields.close"
        ),
        total_market_cap=_exact_numeric(
            fields.total_market_cap,
            precision=24,
            field_name="content.structured_fields.total_market_cap",
        ),
        pe_ttm=_exact_numeric(
            fields.pe_ttm, precision=20, field_name="content.structured_fields.pe_ttm"
        ),
        **_quality_values(event),
        fetch_time=fetch_time_for(event),
        event_id=event.event_id,
        provider=event.provider,
        **compliance,
    )
    return _outcome(StockQuoteSnapshot, event, row)


def _write_risk(
    session: Session, event: ValidatedEvent, compliance: Mapping[str, Any]
) -> WriteOutcome:
    content = cast(RiskContent, event.content)
    fields = content.structured_fields
    row = RiskEventRepository(session).insert_once(
        symbol=_require_symbol(event),
        occur_date=fields.occur_date,
        risk_type=fields.risk_type,
        severity=fields.severity,
        summary=fields.summary,
        source_announcement_ref=content.source_announcement_ref,
        **_quality_values(event),
        fetch_time=fetch_time_for(event),
        event_id=event.event_id,
        provider=event.provider,
        **compliance,
    )
    return _outcome(StockRiskEvent, event, row)


_WRITERS: dict[EventDataType, _Handler] = {
    EventDataType.FINANCIAL: _write_financial,
    EventDataType.ANNOUNCEMENT: _write_announcement,
    EventDataType.KLINE: _write_kline,
    EventDataType.QUOTE: _write_quote,
    EventDataType.RISK: _write_risk,
}


def _outcome(model: type[Any], event: ValidatedEvent, row: Any) -> WriteOutcome:
    table = f"{model.__table__.schema}.{model.__tablename__}"
    if row is None:
        _log.info("DWD row already exists, event %s skipped", event.event_id)
        return WriteOutcome(status="skipped", table=table, event_id=event.event_id)
    return WriteOutcome(
        status="inserted",
        table=table,
        event_id=event.event_id,
        version=getattr(row, "version", None),
        row_id=getattr(row, "id", None),
    )


def _require_symbol(event: ValidatedEvent) -> str:
    if event.symbol is None:
        raise InvalidArgumentError(
            f"symbol is required for data_type={event.data_type.value}",
            details={"field": "symbol"},
        )
    return event.symbol


def _quality_values(event: ValidatedEvent) -> dict[str, Any]:
    """Copy upstream quality judgement verbatim; never recompute or downgrade."""

    metrics = event.content.quality_metrics
    return {
        "quality_status": metrics.quality_status.value,
        "confidence": None if metrics.confidence is None else metrics.confidence.value,
    }


def _balance_check_result(event: ValidatedEvent) -> Any:
    return (event.content.quality_metrics.model_extra or {}).get("balance_check_result")


def _compliance_values(event: ValidatedEvent, enabled: bool) -> dict[str, Any]:
    if not enabled:
        # DEC-0010: off means both columns stay NULL, even if a mark was parsed.
        return compliance_columns(None)
    if event.compliance is None:
        raise InvalidArgumentError(
            "compliance marking is enabled but the event carries no mark",
            details={"field": "source_info.compliance_tier"},
        )
    return compliance_columns(event.compliance)


def _exact_numeric(value: Decimal | None, *, precision: int, field_name: str) -> Decimal | None:
    """Return ``value`` unchanged or reject it when ``NUMERIC(precision, 4)``
    would round or overflow it.

    ``Decimal.quantize`` is the test: if the value differs from its 4-decimal
    form, the database would round it. Nothing is ever rounded or truncated on
    the way in -- a bad amount fails the whole ingest instead.
    """

    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise InvalidArgumentError(
            f"{field_name} must be a Decimal, got {type(value).__name__}",
            details={"field": field_name, "type": type(value).__name__},
        )

    numeric = f"NUMERIC({precision}, {_SCALE})"
    with localcontext() as context:
        context.prec = _CHECK_PRECISION
        try:
            exact = value.quantize(_QUANTUM)
            limit = Decimal(1).scaleb(precision - _SCALE)
        except InvalidOperation as exc:
            _log.warning("rejecting %s=%s: does not fit %s", field_name, value, numeric)
            raise InvalidArgumentError(
                f"{field_name} does not fit {numeric}",
                details={"field": field_name, "value": str(value), "numeric": numeric},
            ) from exc

    if not value.is_finite() or exact != value:
        _log.warning(
            "rejecting %s=%s: more than %d decimal places for %s",
            field_name,
            value,
            _SCALE,
            numeric,
        )
        raise InvalidArgumentError(
            f"{field_name} has more than {_SCALE} decimal places",
            details={"field": field_name, "value": str(value), "numeric": numeric},
        )
    if abs(exact) >= limit:
        _log.warning("rejecting %s=%s: integer digits exceed %s", field_name, value, numeric)
        raise InvalidArgumentError(
            f"{field_name} does not fit {numeric}",
            details={"field": field_name, "value": str(value), "numeric": numeric},
        )
    return value


def _content_hash(content: AnnouncementContent) -> str:
    """sha256 of the cleaned body (``raw_html`` when no body was cleaned)."""

    material = content.clean_content if content.clean_content is not None else content.raw_html
    if material is None:
        raise InvalidArgumentError(
            "announcement requires raw_html or clean_content",
            details={"field": "content.clean_content"},
        )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _announcement_date(structured: Mapping[str, Any]) -> date:
    value = structured.get("publish_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise InvalidArgumentError(
        "content.structured_fields.publish_date must be an ISO date",
        details={"field": "content.structured_fields.publish_date", "value": value},
    )


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidArgumentError(
            f"{field_name} must be a string",
            details={"field": field_name, "type": type(value).__name__},
        )
    return value


__all__ = ["WriteOutcome", "write_event"]
