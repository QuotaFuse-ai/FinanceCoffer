"""Vector models: announcement chunks and their embeddings."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from financecoffer.db.models.base import Base
from financecoffer.db.models.types import Vector


class AnnouncementChunk(Base):
    __tablename__ = "announcement_chunk"
    __table_args__ = (
        UniqueConstraint("dwd_ref_id", "chunk_index", name="uq_ann_chunk_position"),
        CheckConstraint("chunk_index >= 0", name="ck_ann_chunk_index"),
        CheckConstraint(
            "token_count IS NULL OR token_count > 0",
            name="ck_ann_chunk_tokens",
        ),
        Index("idx_ann_chunk_filter", "symbol", "ann_date"),
        Index("idx_ann_chunk_ref", "dwd_ref_id"),
        Index(
            "idx_ann_chunk_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        {"schema": "vector"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    ann_date: Mapped[date] = mapped_column(Date)
    ann_type: Mapped[str | None] = mapped_column(String(50))
    chunk_index: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    chunk_text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    embedding_model: Mapped[str] = mapped_column(
        String(100), server_default=text("'BAAI/bge-m3'")
    )
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    dwd_ref_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "dwd.stock_announcement.id",
            name="announcement_chunk_dwd_ref_id_fkey",
            ondelete="CASCADE",
        ),
    )
