"""Pydantic models for the upstream event contract (v0.3).

The models mirror ``docs/specs/event-contract.md``:

* amounts, prices and volumes are :class:`~decimal.Decimal` parsed from decimal
  strings; a JSON number is rejected instead of being coerced, because a float
  has already lost precision before validation starts;
* ``quality_status`` is required and never defaulted -- FinanceCoffer does not
  invent upstream judgements;
* compliance fields stay unmodelled. ``source_info`` tolerates them as extras
  and the models never look at them; only
  :func:`financecoffer.core.compliance.extract_mark` reads them, and only when
  ``FC_COMPLIANCE_ENABLED`` is on (DEC-0010).

Unknown fields are allowed everywhere on purpose: the contract says a new
optional field does not bump ``schema_version`` (section 6), so a consumer that
forbids extras would break on a forward-compatible publisher change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

SUPPORTED_SCHEMA_VERSION = "1.0"

#: Contract section 1: one event is at most 8 MB unless it carries a MinIO reference.
MAX_EVENT_BYTES = 8 * 1024 * 1024


class EventDataType(StrEnum):
    FINANCIAL = "financial"
    ANNOUNCEMENT = "announcement"
    KLINE = "kline"
    QUOTE = "quote"
    RISK = "risk"
    RESEARCH = "research"
    NORTHBOUND = "northbound"
    HEARTBEAT = "heartbeat"


class Market(StrEnum):
    CN = "CN"
    US = "US"
    HK = "HK"


class QualityStatus(StrEnum):
    NORMAL = "normal"
    SUSPICIOUS = "suspicious"
    BAD = "bad"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def _decimal_from_string(value: Any) -> Decimal:
    """Parse a decimal string; reject JSON numbers so precision is not lost."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "must be a decimal string, JSON numbers are rejected to avoid precision loss"
        )
    try:
        number = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError("must be a decimal string") from exc
    if not number.is_finite():
        raise ValueError("must be a finite decimal string")
    return number


DecimalString = Annotated[Decimal, BeforeValidator(_decimal_from_string)]


class QualityMetrics(BaseModel):
    """``quality_metrics`` shared by every payload; ``quality_status`` is required."""

    model_config = ConfigDict(extra="allow")

    confidence: Confidence | None = None
    quality_status: QualityStatus


class SourceInfo(BaseModel):
    """``source_info`` envelope block.

    Compliance fields are deliberately absent so nothing here reads or rewrites
    them; they remain available untouched in the caller's raw payload.
    ``hub`` is the publishing hub (``tickhub-cn`` / ``tickhub-us``) and is what
    the ODS ``source`` label is taken from; ``provider`` names the upstream
    vendor (``cninfo``, ``tencent``, ...), not the hub.
    """

    model_config = ConfigDict(extra="allow")

    provider: str = Field(min_length=1)
    hub: str | None = None
    crawl_url: str | None = None
    crawl_time: datetime | None = None


class EventEnvelope(BaseModel):
    """Envelope fields shared by every ``data_type``."""

    model_config = ConfigDict(extra="allow")

    schema_version: str
    event_id: str = Field(min_length=1)
    biz_key: str = Field(min_length=1)
    event_time: datetime
    data_type: EventDataType
    symbol: str | None = None
    market: Market
    source_info: SourceInfo
    content: dict[str, Any]
    trace_id: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _known_schema_version(cls, value: str) -> str:
        if value != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value!r}; "
                f"only {SUPPORTED_SCHEMA_VERSION!r} is accepted"
            )
        return value

    @field_validator("event_time")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("event_time must be RFC3339 with a timezone")
        return value.astimezone(UTC)


class BaseContent(BaseModel):
    """Payload shared block: ``quality_metrics`` is mandatory everywhere."""

    model_config = ConfigDict(extra="allow")

    quality_metrics: QualityMetrics


class AnnouncementContent(BaseContent):
    raw_html: str | None = None
    clean_content: str | None = None
    structured_fields: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_body(self) -> AnnouncementContent:
        if not self.raw_html and not self.clean_content:
            raise ValueError("announcement requires raw_html or clean_content")
        return self


class FinancialFields(BaseModel):
    model_config = ConfigDict(extra="allow")

    report_date: date
    report_type: str = Field(min_length=1)
    amount_revenue: DecimalString | None = None
    amount_net_profit: DecimalString | None = None
    #: 扣非净利润 (year-to-date). Optional: providers that only publish the
    #: parent figure send nothing and the deducted PE stays ``null``.
    amount_net_profit_deducted: DecimalString | None = None
    #: ``balance`` / ``income`` / ``cashflow``; several statements share one
    #: report date, so the warehouse needs it to tell them apart.
    statement: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _extra_amounts_are_strings(self) -> FinancialFields:
        for key, value in (self.model_extra or {}).items():
            if key.startswith("amount_") and not isinstance(value, str):
                raise ValueError(
                    f"structured_fields.{key} must be a decimal string, "
                    "JSON numbers are rejected to avoid precision loss"
                )
        return self


class FinancialContent(BaseContent):
    raw_html: str | None = None
    structured_fields: FinancialFields


class KlineFields(BaseModel):
    model_config = ConfigDict(extra="allow")

    trade_date: date
    open: DecimalString
    high: DecimalString
    low: DecimalString
    close: DecimalString
    volume: DecimalString
    amount: DecimalString | None = None
    adjust: str = Field(min_length=1)


class KlineContent(BaseContent):
    structured_fields: KlineFields


class QuoteFields(BaseModel):
    model_config = ConfigDict(extra="allow")

    trade_date: date
    close: DecimalString
    total_market_cap: DecimalString | None = None
    pe_ttm: DecimalString | None = None
    pb_mrq: DecimalString | None = None


class QuoteContent(BaseContent):
    structured_fields: QuoteFields


class RiskFields(BaseModel):
    model_config = ConfigDict(extra="allow")

    risk_type: str = Field(min_length=1)
    occur_date: date
    severity: str = Field(min_length=1)
    summary: str | None = None


class RiskContent(BaseContent):
    structured_fields: RiskFields
    source_announcement_ref: str | None = None


class ResearchFields(BaseModel):
    """Contract 3.6: one research report; ``pdf_url`` is a reference only."""

    model_config = ConfigDict(extra="allow")

    report_id: str | None = None
    title: str | None = None
    institution: str | None = None
    analyst: str | None = None
    rating: str | None = None
    target_price: DecimalString | None = None
    #: ``dwd.stock_research_report.publish_date`` is NOT NULL, so a report
    #: without a publish date can never be stored and is rejected here instead.
    publish_date: date
    pdf_url: str | None = None


class ResearchContent(BaseContent):
    structured_fields: ResearchFields


class NorthboundFields(BaseModel):
    """Contract 3.7: market-level flow; the envelope carries ``symbol=null``."""

    model_config = ConfigDict(extra="allow")

    trade_date: date
    channel: str = Field(min_length=1)
    net_buy_amount: DecimalString | None = None
    buy_amount: DecimalString | None = None
    sell_amount: DecimalString | None = None
    currency: str = Field(default="CNY", min_length=1, max_length=3)


class NorthboundContent(BaseContent):
    structured_fields: NorthboundFields


class HeartbeatStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class HeartbeatFields(BaseModel):
    """Contract 3.8: one crawler round; ``run_id`` is the ``biz_key``.

    A ``failed`` round must still be published -- otherwise "not running" and
    "ran but collected nothing" look the same to the consumer.
    """

    model_config = ConfigDict(extra="allow")

    run_id: str = Field(min_length=1)
    status: HeartbeatStatus
    items_collected: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_run_at: datetime | None = None

    @field_validator("started_at", "finished_at", "next_run_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


class HeartbeatContent(BaseContent):
    structured_fields: HeartbeatFields
