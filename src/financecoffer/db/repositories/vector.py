"""Vector repository: append announcement chunks and their embeddings."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from financecoffer.db.models.vector import AnnouncementChunk
from financecoffer.db.repositories.base import BaseRepository


class AnnouncementChunkRepository(BaseRepository[AnnouncementChunk]):
    model = AnnouncementChunk

    def append(self, **values: Any) -> AnnouncementChunk:
        return self.add(**values)

    def append_many(
        self, rows: Iterable[Mapping[str, Any]]
    ) -> list[AnnouncementChunk]:
        objects = [self.model(**row) for row in rows]
        self.session.add_all(objects)
        self.session.flush()
        return objects

    def insert_once(self, **values: Any) -> AnnouncementChunk | None:
        """Insert unless ``(dwd_ref_id, chunk_index)`` is already stored."""

        return self.insert_ignore_conflict(**values)
