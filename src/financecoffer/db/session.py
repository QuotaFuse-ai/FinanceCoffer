"""Engine and session lifecycle.

Repositories receive a ``Session`` and only ``flush``; commit and rollback belong
to the service layer through :class:`Database`. Importing this module does not
connect to PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from financecoffer.db.connection import resolve_dsn


def sqlalchemy_url(dsn: str) -> str:
    """Return a SQLAlchemy URL; plain PostgreSQL DSNs use the psycopg 3 driver."""

    cleaned = dsn.strip()
    if cleaned.startswith(("postgresql+", "postgres+")):
        return cleaned
    if cleaned.startswith("postgres://"):
        return "postgresql+psycopg://" + cleaned.removeprefix("postgres://")
    if cleaned.startswith("postgresql://"):
        return "postgresql+psycopg://" + cleaned.removeprefix("postgresql://")
    return cleaned


class Database:
    """Owns one engine and its ``sessionmaker``; one instance per DSN/process."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        echo: bool = False,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_pre_ping: bool = True,
    ) -> None:
        self.dsn = dsn or resolve_dsn()
        self.engine: Engine = create_engine(
            sqlalchemy_url(self.dsn),
            echo=echo,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=pool_pre_ping,
        )
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=False,
        )

    def session(self) -> Session:
        """Open a session; the caller owns commit/rollback/close."""

        return self._session_factory()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """Commit on success, roll back on error, always close."""

        session = self.session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
