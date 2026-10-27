"""DWD models: versioned facts and append-only detail rows."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from financecoffer.db.models.base import Base

_QUALITY_CHECK = "quality_status IN ('normal', 'suspicious', 'bad')"
_COMPLIANCE_CHECK = "compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X')"


class StockFinancialSnapshot(Base):
    __tablename__ = "stock_financial_snapshot"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_fin_snapshot_event"),
        CheckConstraint("version >= 1", name="ck_fin_snapshot_version"),
        CheckConstraint(_QUALITY_CHECK, name="ck_fin_snapshot_quality"),
        CheckConstraint(_COMPLIANCE_CHECK, name="ck_fin_snapshot_compliance_tier"),
        Index("idx_fin_snapshot_symbol_date", "symbol", desc("report_date")),
        Index(
            "idx_fin_snapshot_symbol_type_version",
            "symbol",
            "report_type",
            desc("version"),
        ),
        {"schema": "dwd"},
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, primary_key=True)
    report_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    amount_revenue: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    amount_net_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    quality_status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str | None] = mapped_column(String(20))
    balance_check_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_cold: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    minio_url: Mapped[str | None] = mapped_column(String(500))
    event_id: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    compliance_tier: Mapped[str | None] = mapped_column(String(1))
    commercial_use: Mapped[bool | None]


class StockAnnouncement(Base):
    __tablename__ = "stock_announcement"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_ann_content_hash"),
        UniqueConstraint("event_id", name="uq_ann_event"),
        CheckConstraint(_QUALITY_CHECK, name="ck_ann_quality"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_ann_content_hash"),
        CheckConstraint(_COMPLIANCE_CHECK, name="ck_ann_compliance_tier"),
        Index("idx_ann_symbol_date", "symbol", desc("ann_date")),
        Index("idx_ann_type_date", "ann_type", desc("ann_date")),
        {"schema": "dwd"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    ann_date: Mapped[date] = mapped_column(Date)
    ann_type: Mapped[str | None] = mapped_column(String(50))
    title: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    clean_content: Mapped[str | None] = mapped_column(Text)
    minio_url: Mapped[str | None] = mapped_column(String(500))
    quality_status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str | None] = mapped_column(String(20))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_cold: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    event_id: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    compliance_tier: Mapped[str | None] = mapped_column(String(1))
    commercial_use: Mapped[bool | None]


class StockKline(Base):
    __tablename__ = "stock_kline"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_kline_event"),
        CheckConstraint("version >= 1", name="ck_kline_version"),
        CheckConstraint(_QUALITY_CHECK, name="ck_kline_quality"),
        CheckConstraint(
            "high IS NULL OR low IS NULL OR high >= low",
            name="ck_kline_high_low",
        ),
        CheckConstraint(_COMPLIANCE_CHECK, name="ck_kline_compliance_tier"),
        Index("idx_kline_symbol_date", "symbol", desc("trade_date")),
        {"schema": "dwd"},
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    adjust: Mapped[str] = mapped_column(
        String(20), primary_key=True, server_default=text("'none'")
    )
    version: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, server_default=text("1")
    )
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    close: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    quality_status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str | None] = mapped_column(String(20))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    compliance_tier: Mapped[str | None] = mapped_column(String(1))
    commercial_use: Mapped[bool | None]


class StockRiskEvent(Base):
    __tablename__ = "stock_risk_event"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_risk_event"),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_risk_severity",
        ),
        CheckConstraint(_QUALITY_CHECK, name="ck_risk_quality"),
        CheckConstraint(_COMPLIANCE_CHECK, name="ck_risk_compliance_tier"),
        Index("idx_risk_symbol_date", "symbol", desc("occur_date")),
        Index("idx_risk_severity_date", "severity", desc("occur_date")),
        {"schema": "dwd"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    occur_date: Mapped[date] = mapped_column(Date)
    risk_type: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(20))
    summary: Mapped[str | None] = mapped_column(Text)
    source_announcement_ref: Mapped[str | None] = mapped_column(String(200))
    quality_status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str | None] = mapped_column(String(20))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    compliance_tier: Mapped[str | None] = mapped_column(String(1))
    commercial_use: Mapped[bool | None]


class StockQuoteSnapshot(Base):
    __tablename__ = "stock_quote_snapshot"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_quote_event"),
        CheckConstraint("version >= 1", name="ck_quote_version"),
        CheckConstraint(_QUALITY_CHECK, name="ck_quote_quality"),
        CheckConstraint(_COMPLIANCE_CHECK, name="ck_quote_compliance_tier"),
        Index("idx_quote_symbol_date", "symbol", desc("trade_date")),
        {"schema": "dwd"},
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    version: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, server_default=text("1")
    )
    close: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    total_market_cap: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    pe_ttm: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    quality_status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str | None] = mapped_column(String(20))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    compliance_tier: Mapped[str | None] = mapped_column(String(1))
    commercial_use: Mapped[bool | None]


class StockResearchReport(Base):
    __tablename__ = "stock_research_report"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_research_event"),
        CheckConstraint(_QUALITY_CHECK, name="ck_research_quality"),
        CheckConstraint(_COMPLIANCE_CHECK, name="ck_report_compliance_tier"),
        Index("idx_research_symbol_date", "symbol", desc("publish_date")),
        {"schema": "dwd"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    report_id: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(Text)
    institution: Mapped[str | None] = mapped_column(String(100))
    analyst: Mapped[str | None] = mapped_column(String(100))
    rating: Mapped[str | None] = mapped_column(String(50))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    publish_date: Mapped[date] = mapped_column(Date)
    pdf_url: Mapped[str | None] = mapped_column(String(500))
    quality_status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str | None] = mapped_column(String(20))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    compliance_tier: Mapped[str | None] = mapped_column(String(1))
    commercial_use: Mapped[bool | None]


class NorthboundFlow(Base):
    __tablename__ = "northbound_flow"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_northbound_event"),
        CheckConstraint(_QUALITY_CHECK, name="ck_northbound_quality"),
        CheckConstraint(_COMPLIANCE_CHECK, name="ck_northbound_compliance_tier"),
        Index("idx_northbound_date", desc("trade_date")),
        {"schema": "dwd"},
    )

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    channel: Mapped[str] = mapped_column(String(50), primary_key=True)
    market: Mapped[str] = mapped_column(String(10), server_default=text("'CN'"))
    net_buy_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    buy_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    sell_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'CNY'"))
    quality_status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str | None] = mapped_column(String(20))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    compliance_tier: Mapped[str | None] = mapped_column(String(1))
    commercial_use: Mapped[bool | None]
