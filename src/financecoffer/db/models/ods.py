"""ODS models: immutable raw events partitioned by ``fetch_time``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from financecoffer.db.models.base import Base


class StockRawData(Base):
    __tablename__ = "stock_raw_data"
    __table_args__ = (
        CheckConstraint(
            "etl_status IN ('pending', 'done', 'failed')",
            name="ck_ods_etl_status",
        ),
        CheckConstraint(
            "raw_json IS NOT NULL OR raw_file_path IS NOT NULL",
            name="ck_ods_raw_payload",
        ),
        CheckConstraint(
            "compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X')",
            name="ck_ods_raw_compliance_tier",
        ),
        Index("idx_ods_raw_event_id", "event_id"),
        Index("idx_ods_raw_symbol_time", "symbol", desc("fetch_time")),
        Index("idx_ods_raw_type_time", "data_type", desc("fetch_time")),
        Index("idx_ods_raw_status", "etl_status", "fetch_time"),
        {
            "schema": "ods",
            "postgresql_partition_by": "RANGE (fetch_time)",
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(100))
    data_type: Mapped[str] = mapped_column(String(50))
    #: ``None`` for contract payloads that describe a market rather than a
    #: stock (``northbound``); a missing symbol is never replaced by a fake one.
    symbol: Mapped[str | None] = mapped_column(String(20))
    market: Mapped[str | None] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(50))
    biz_key: Mapped[str] = mapped_column(String(200))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    request_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_text: Mapped[str | None] = mapped_column(Text)
    raw_file_path: Mapped[str | None] = mapped_column(String(500))
    etl_status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    compliance_tier: Mapped[str | None] = mapped_column(String(1))
    commercial_use: Mapped[bool | None]
