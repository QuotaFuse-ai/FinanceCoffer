"""Session factory tests; constructing a :class:`Database` never connects."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from financecoffer.db.session import Database, sqlalchemy_url


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("  postgresql://u:p@h/db  ", "postgresql+psycopg://u:p@h/db"),
    ],
)
def test_sqlalchemy_url_selects_psycopg3(dsn: str, expected: str) -> None:
    assert sqlalchemy_url(dsn) == expected


def test_database_builds_a_lazy_engine_and_sessions() -> None:
    database = Database("postgresql://u:p@127.0.0.1:1/db")
    try:
        assert database.engine.url.drivername == "postgresql+psycopg"
        with database.session() as session:
            assert isinstance(session, Session)
            assert session.bind is database.engine
    finally:
        database.dispose()
