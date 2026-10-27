"""DWS models: pre-computed wide rows for agent queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from financecoffer.db.models.base import Base


class StockAgentAnalysisWide(Base):
    __tablename__ = "stock_agent_analysis_wide"
    __table_args__ = (
        CheckConstraint(
            "fiscal_years BETWEEN 1 AND 10",
            name="ck_dws_wide_fiscal_years",
        ),
        CheckConstraint(
            "jsonb_typeof(metrics_json) = 'object'",
            name="ck_dws_wide_metrics",
        ),
        CheckConstraint(
            "compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X')",
            name="ck_dws_wide_compliance_tier",
        ),
        Index("idx_dws_wide_as_of", desc("as_of")),
        {"schema": "dws"},
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    risk_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    latest_announcements: Mapped[list[Any] | None] = mapped_column(JSONB)
    fiscal_years: Mapped[int] = mapped_column(SmallInteger, server_default=text("5"))
    source_version: Mapped[str | None] = mapped_column(String(50))
    built_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    compliance_tier: Mapped[str | None] = mapped_column(String(1))
    commercial_use: Mapped[bool | None]
