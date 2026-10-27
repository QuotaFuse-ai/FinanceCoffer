"""DWS repository: persist pre-computed wide rows (no aggregation here)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from financecoffer.db.models.dws import StockAgentAnalysisWide
from financecoffer.db.repositories.base import BaseRepository


class AgentAnalysisWideRepository(BaseRepository[StockAgentAnalysisWide]):
    model = StockAgentAnalysisWide

    def upsert(self, **values: Any) -> StockAgentAnalysisWide:
        """Replace the wide row for one symbol with the caller-built payload."""

        values.setdefault("built_at", func.now())
        updates = {key: value for key, value in values.items() if key != "symbol"}
        statement = (
            pg_insert(self.model)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[self.model.symbol],
                set_=updates,
            )
            .returning(self.model)
        )
        return self._one(statement)
