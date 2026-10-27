"""Shared repository behaviour: a session, reads, inserts and conflict-skip.

Repositories only ``flush``; the transaction boundary belongs to
:class:`financecoffer.db.session.Database`.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from financecoffer.db.models.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *primary_key: Any) -> ModelT | None:
        key: Any = primary_key[0] if len(primary_key) == 1 else primary_key
        return self.session.get(self.model, key)

    def add(self, **values: Any) -> ModelT:
        row = self.model(**values)
        self.session.add(row)
        self.session.flush()
        return row

    def insert_ignore_conflict(self, **values: Any) -> ModelT | None:
        """Insert unless any unique constraint matches; return ``None`` on skip."""

        statement = (
            pg_insert(self.model)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(self.model)
        )
        return self._one_or_none(statement)

    def select_all(self) -> list[ModelT]:
        return list(self.session.scalars(select(self.model)))

    def _one(self, statement: Any) -> ModelT:
        return cast(ModelT, self.session.scalars(statement).one())

    def _one_or_none(self, statement: Any) -> ModelT | None:
        return cast("ModelT | None", self.session.scalars(statement).one_or_none())
