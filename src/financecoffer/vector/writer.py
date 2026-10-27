"""Idempotent writes into ``vector.announcement_chunk``.

One announcement owns one contiguous block of ``chunk_index`` values. Writing
uses ``INSERT ... ON CONFLICT (dwd_ref_id, chunk_index) DO UPDATE`` so replaying
the same pipeline converges on the same rows instead of appending duplicates,
and a shorter document removes the rows that are no longer produced.

Gaps left by a failed embedding are kept on purpose: the last successfully
embedded text is better than dropping searchable evidence, and the failure is
already counted in ``fc_missing_chunk_total``. Callers own the transaction
(``Database.session_scope``); the writer only executes statements.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from financecoffer.db.models.vector import AnnouncementChunk

_UPDATABLE = (
    "symbol",
    "ann_date",
    "ann_type",
    "chunk_text",
    "token_count",
    "embedding",
    "embedding_model",
    "embedded_at",
)


@dataclass(frozen=True)
class EmbeddedChunk:
    """One chunk together with the vector produced for it."""

    chunk_index: int
    chunk_text: str
    token_count: int
    embedding: Sequence[float]


@dataclass(frozen=True)
class UpsertCounts:
    """Rows touched by one :meth:`ChunkWriter.replace` call."""

    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    embedded: int = field(default=0)

    @property
    def written(self) -> int:
        return self.inserted + self.updated


class ChunkWriter:
    """Upsert and prune the chunks of a single DWD announcement."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def replace(
        self,
        *,
        dwd_ref_id: int,
        symbol: str,
        ann_date: date,
        ann_type: str | None,
        chunk_total: int,
        chunks: Sequence[EmbeddedChunk],
        embedding_model: str,
        embedded_at: datetime,
    ) -> UpsertCounts:
        table = AnnouncementChunk.__table__
        inserted = 0

        if chunks:
            values: list[dict[str, Any]] = [
                {
                    "dwd_ref_id": dwd_ref_id,
                    "symbol": symbol,
                    "ann_date": ann_date,
                    "ann_type": ann_type,
                    "chunk_index": chunk.chunk_index,
                    "chunk_text": chunk.chunk_text,
                    "token_count": chunk.token_count,
                    "embedding": list(chunk.embedding),
                    "embedding_model": embedding_model,
                    "embedded_at": embedded_at,
                }
                for chunk in chunks
            ]
            statement = pg_insert(table).values(values)
            statement = statement.on_conflict_do_update(
                constraint="uq_ann_chunk_position",
                set_={name: statement.excluded[name] for name in _UPDATABLE},
            ).returning(table.c.id, literal_column("(xmax = 0)").label("inserted"))
            inserted = sum(1 for row in self.session.execute(statement) if row.inserted)

        pruned = self.session.execute(
            delete(table).where(
                table.c.dwd_ref_id == dwd_ref_id,
                table.c.chunk_index >= max(int(chunk_total), 0),
            )
        ).rowcount

        return UpsertCounts(
            inserted=inserted,
            updated=len(chunks) - inserted,
            deleted=max(int(pruned or 0), 0),
            embedded=len(chunks),
        )


__all__ = ["ChunkWriter", "EmbeddedChunk", "UpsertCounts"]
