"""Ops models: heartbeats, idempotency ledger, offsets, DLQ, archive jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from financecoffer.db.models.base import Base


class SourceHeartbeat(Base):
    __tablename__ = "source_heartbeat"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'partial', 'failed')",
            name="ck_source_heartbeat_status",
        ),
        CheckConstraint("items >= 0", name="ck_source_heartbeat_items"),
        Index("idx_source_heartbeat_last_seen", desc("last_seen")),
        {"schema": "ops"},
    )

    source: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20))
    items: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    run_id: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class EtlEventLog(Base):
    __tablename__ = "etl_event_log"
    __table_args__ = (
        CheckConstraint(
            "status IN ('received', 'processing', 'done', 'failed', 'dlq', 'duplicate')",
            name="ck_etl_event_log_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_etl_event_log_attempts"),
        UniqueConstraint(
            "provider",
            "data_type",
            "symbol",
            "biz_key",
            name="uq_etl_event_identity",
            postgresql_nulls_not_distinct=True,
        ),
        Index("idx_etl_event_log_status", "status", "received_at"),
        Index(
            "idx_etl_event_log_type",
            "provider",
            "data_type",
            desc("received_at"),
        ),
        {"schema": "ops"},
    )

    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    data_type: Mapped[str] = mapped_column(String(50))
    symbol: Mapped[str | None] = mapped_column(String(20))
    biz_key: Mapped[str] = mapped_column(String(200))
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'received'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EtlConsumerOffset(Base):
    __tablename__ = "etl_consumer_offset"
    __table_args__ = {"schema": "ops"}

    consumer_group: Mapped[str] = mapped_column(String(100), primary_key=True)
    stream: Mapped[str] = mapped_column(String(100), primary_key=True)
    shard: Mapped[str] = mapped_column(
        String(100), primary_key=True, server_default=text("'0'")
    )
    last_event_id: Mapped[str | None] = mapped_column(String(100))
    last_event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class EtlDlq(Base):
    __tablename__ = "etl_dlq"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'retried', 'purged')",
            name="ck_etl_dlq_status",
        ),
        CheckConstraint("retry_count >= 0", name="ck_etl_dlq_retry_count"),
        Index("idx_etl_dlq_status_created", "status", "created_at"),
        Index("idx_etl_dlq_event_id", "event_id"),
        {"schema": "ops"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str | None] = mapped_column(String(100))
    stream: Mapped[str] = mapped_column(String(100))
    entry_id: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(100))
    raw_event: Mapped[dict[str, Any]] = mapped_column(JSONB)
    error_detail: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ArchiveJob(Base):
    __tablename__ = "archive_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="ck_archive_job_status",
        ),
        CheckConstraint(
            "rows_archived >= 0 AND bytes_archived >= 0",
            name="ck_archive_job_rows",
        ),
        Index("idx_archive_job_status", "status", "created_at"),
        Index("idx_archive_job_symbol", "symbol", desc("created_at")),
        {"schema": "ops"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str | None] = mapped_column(String(20))
    object_key: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    rows_archived: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    bytes_archived: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class SchemaVersion(Base):
    """Migration ledger owned by :mod:`financecoffer.db.migrations`."""

    __tablename__ = "schema_version"
    __table_args__ = {"schema": "ops"}

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(CHAR(64))
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    duration_ms: Mapped[int] = mapped_column(Integer)
    applied_by: Mapped[str] = mapped_column(Text, server_default=text("current_user"))
