"""``ops.etl_event_log`` idempotency against a migrated PostgreSQL database (T-004)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from financecoffer.db.models import EtlEventLog
from financecoffer.db.session import Database
from financecoffer.events import ValidatedEvent, compute_event_id, register_event, validate_event

#: A provider of its own so these rows never disturb other modules' totals
#: (``migrated_warehouse`` is shared by the whole session).
PROVIDER = "t004-etl"


@pytest.fixture()
def db(migrated_warehouse: str) -> Iterator[Database]:
    database = Database(migrated_warehouse)
    try:
        yield database
    finally:
        try:
            with database.session_scope() as session:
                session.execute(
                    delete(EtlEventLog).where(EtlEventLog.provider == PROVIDER)
                )
        finally:
            database.dispose()


def event() -> ValidatedEvent:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider=PROVIDER, data_type="quote", symbol="600000", biz_key="2026-10-20"
        ),
        "biz_key": "2026-10-20",
        "event_time": "2026-10-27T02:00:00Z",
        "data_type": "quote",
        "symbol": "600000",
        "market": "CN",
        "source_info": {"provider": PROVIDER},
        "content": {
            "quality_metrics": {"quality_status": "normal"},
            "structured_fields": {"trade_date": "2026-10-20", "close": "11.11"},
        },
    }
    return validate_event(payload)


def count_rows(database: Database) -> int:
    with database.session_scope() as session:
        statement = (
            select(func.count())
            .select_from(EtlEventLog)
            .where(EtlEventLog.provider == PROVIDER)
        )
        return int(session.scalar(statement) or 0)


def test_redelivery_registers_once_and_counts_the_attempt(db: Database) -> None:
    first_event = event()

    with db.session_scope() as session:
        first = register_event(session, first_event)

    with db.session_scope() as session:
        second = register_event(session, first_event)

    assert first.duplicate is False
    assert first.reason == "new"
    assert second.duplicate is True
    assert second.reason == "event_id"
    assert second.event_id == first_event.event_id
    assert second.attempts == 1
    assert count_rows(db) == 1

    with db.session_scope() as session:
        row = session.get(EtlEventLog, first_event.event_id)
        assert row is not None
        assert row.attempts == 1
        assert row.status == "received"


def test_a_new_event_id_for_a_known_identity_is_rejected_as_biz_key(db: Database) -> None:
    first_event = event()
    with db.session_scope() as session:
        register_event(session, first_event)

    impostor = replace(first_event, event_id="evt_deliberately_different")
    with db.session_scope() as session:
        result = register_event(session, impostor)

    assert result.duplicate is True
    assert result.reason == "biz_key"
    assert result.event_id == first_event.event_id
    assert result.attempts == 1
    assert count_rows(db) == 1
