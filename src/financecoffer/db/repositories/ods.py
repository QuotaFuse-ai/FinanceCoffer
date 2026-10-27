"""ODS repository: append-only writes and replay reads, never updates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select

from financecoffer.db.models.ods import StockRawData
from financecoffer.db.repositories.base import BaseRepository


class RawDataRepository(BaseRepository[StockRawData]):
    model = StockRawData

    def append(self, **values: Any) -> StockRawData:
        return self.add(**values)

    def append_many(self, rows: Iterable[Mapping[str, Any]]) -> list[StockRawData]:
        objects = [self.model(**row) for row in rows]
        self.session.add_all(objects)
        self.session.flush()
        return objects

    def by_event_id(self, event_id: str, *, limit: int = 100) -> Sequence[StockRawData]:
        statement = (
            select(self.model)
            .where(self.model.event_id == event_id)
            .order_by(self.model.fetch_time, self.model.id)
            .limit(limit)
        )
        return self.session.scalars(statement).all()

    def iter_range(
        self,
        start: datetime,
        end: datetime,
        *,
        data_type: str | None = None,
        symbol: str | None = None,
        etl_status: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[StockRawData]:
        """Replay ``[start, end)`` by fetch time; reads only."""

        statement = select(self.model).where(
            self.model.fetch_time >= start,
            self.model.fetch_time < end,
        )
        if data_type is not None:
            statement = statement.where(self.model.data_type == data_type)
        if symbol is not None:
            statement = statement.where(self.model.symbol == symbol)
        if etl_status is not None:
            statement = statement.where(self.model.etl_status == etl_status)
        statement = (
            statement.order_by(self.model.fetch_time, self.model.id)
            .limit(limit)
            .offset(offset)
        )
        return self.session.scalars(statement).all()
