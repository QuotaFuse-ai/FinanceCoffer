"""ODS repository: append-only writes and replay reads, never updates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select

from financecoffer.db.models.ods import StockRawData
from financecoffer.db.models.ops import EtlEventLog
from financecoffer.db.repositories.base import BaseRepository

#: Event-log states that mean "this raw row has not reached DWD yet". The ODS
#: row itself is append-only, so a failure is recorded on the event log instead
#: of being written back into ``stock_raw_data.etl_status`` (DEC-0021).
FAILED_EVENT_STATUSES: tuple[str, ...] = ("failed", "dlq")


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

    def by_id(self, record_id: int) -> StockRawData | None:
        """Look one record up by ``id`` (globally unique despite the composite key)."""

        statement = select(self.model).where(self.model.id == record_id).limit(1)
        return self._one_or_none(statement)

    def search(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        data_type: str | None = None,
        symbol: str | None = None,
        etl_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StockRawData]:
        """Newest-first listing; ``until`` is exclusive."""

        statement = select(self.model)
        if since is not None:
            statement = statement.where(self.model.fetch_time >= since)
        if until is not None:
            statement = statement.where(self.model.fetch_time < until)
        if data_type is not None:
            statement = statement.where(self.model.data_type == data_type)
        if symbol is not None:
            statement = statement.where(self.model.symbol == symbol)
        if etl_status is not None:
            statement = statement.where(self.model.etl_status == etl_status)
        statement = (
            statement.order_by(desc(self.model.fetch_time), desc(self.model.id))
            .limit(limit)
            .offset(offset)
        )
        return self.session.scalars(statement).all()

    def failed(self, *, limit: int = 1000) -> Sequence[StockRawData]:
        """Rows awaiting replay, oldest first so redelivery keeps upstream order.

        The failure state lives on ``ops.etl_event_log``: ODS is append-only, so
        ``etl_status`` is written once as ``pending`` and never revisited
        (DEC-0021). Selecting on ``etl_status == 'failed'`` would therefore
        never match a single row, which made ``ods replay --all-failed`` a
        no-op; the join below is the real selector.
        """

        statement = (
            select(self.model)
            .join(EtlEventLog, EtlEventLog.event_id == self.model.event_id)
            .where(EtlEventLog.status.in_(FAILED_EVENT_STATUSES))
            .order_by(self.model.fetch_time, self.model.id)
            .limit(limit)
        )
        return self.session.scalars(statement).all()

    def statistics(self, group_by: str) -> list[tuple[Any, int]]:
        """Counts per distinct value of ``group_by``; the caller validates the column."""

        column = getattr(self.model, group_by)
        statement = (
            select(column, func.count())
            .group_by(column)
            .order_by(func.count().desc(), column)
        )
        return [(value, int(count)) for value, count in self.session.execute(statement).all()]

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
