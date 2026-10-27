"""DWD repositories: append-only detail rows and versioned facts.

Versioned facts never UPDATE a stored row. A new business-key version is
inserted with ``version = MAX(version) + 1``; replayed events are skipped by the
unique ``event_id`` constraint, so an event can only create one version.
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from financecoffer.db.models.dwd import (
    NorthboundFlow,
    StockAnnouncement,
    StockFinancialSnapshot,
    StockKline,
    StockQuoteSnapshot,
    StockResearchReport,
    StockRiskEvent,
)
from financecoffer.db.repositories.base import BaseRepository, ModelT


class VersionedFactRepository(BaseRepository[ModelT]):
    """Insert a new version for a business key; history stays untouched."""

    key_columns: ClassVar[tuple[str, ...]]

    @classmethod
    def _version_column(cls) -> Any:
        return cast(Any, cls.model).version

    def latest(self, **business_key: Any) -> ModelT | None:
        statement = (
            select(self.model)
            .where(
                *(
                    getattr(self.model, name) == value
                    for name, value in business_key.items()
                )
            )
            .order_by(self._version_column().desc())
            .limit(1)
        )
        return self.session.scalars(statement).first()

    def insert_version(self, **values: Any) -> ModelT | None:
        """Append the next version; return ``None`` when the event was seen."""

        key = {name: values[name] for name in self.key_columns}
        next_version = (
            select(func.coalesce(func.max(self._version_column()), 0) + 1)
            .where(
                *(getattr(self.model, name) == value for name, value in key.items())
            )
            .scalar_subquery()
        )
        values.pop("version", None)
        statement = (
            pg_insert(self.model)
            .values(**values, version=next_version)
            .on_conflict_do_nothing()
            .returning(self.model)
        )
        return self._one_or_none(statement)


class FinancialSnapshotRepository(VersionedFactRepository[StockFinancialSnapshot]):
    model = StockFinancialSnapshot
    key_columns = ("symbol", "report_date", "report_type")


class KlineRepository(VersionedFactRepository[StockKline]):
    model = StockKline
    key_columns = ("symbol", "trade_date", "adjust")


class QuoteSnapshotRepository(VersionedFactRepository[StockQuoteSnapshot]):
    model = StockQuoteSnapshot
    key_columns = ("symbol", "trade_date")


class AnnouncementRepository(BaseRepository[StockAnnouncement]):
    model = StockAnnouncement

    def insert_once(self, **values: Any) -> StockAnnouncement | None:
        """Insert unless ``event_id`` or ``content_hash`` was already stored."""

        return self.insert_ignore_conflict(**values)


class RiskEventRepository(BaseRepository[StockRiskEvent]):
    model = StockRiskEvent

    def insert_once(self, **values: Any) -> StockRiskEvent | None:
        return self.insert_ignore_conflict(**values)


class ResearchReportRepository(BaseRepository[StockResearchReport]):
    model = StockResearchReport

    def insert_once(self, **values: Any) -> StockResearchReport | None:
        return self.insert_ignore_conflict(**values)


class NorthboundFlowRepository(BaseRepository[NorthboundFlow]):
    model = NorthboundFlow

    def insert_once(self, **values: Any) -> NorthboundFlow | None:
        """Daily per-channel flow is immutable; a repeat is a no-op."""

        return self.insert_ignore_conflict(**values)
