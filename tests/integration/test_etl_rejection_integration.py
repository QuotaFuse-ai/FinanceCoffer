"""A rejected payload is booked as ``failed`` in ``ops.etl_event_log`` (QUOT-73).

Validation runs before registration, so an invalid payload has no ledger row to
transition -- and the caller's rollback would erase any insert made on its
session. These tests drive the real ``process_payload`` against the migrated
scratch database and read the row back on a fresh session.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import delete, func, or_, select

from financecoffer.core.errors import InvalidArgumentError
from financecoffer.db.models import EtlEventLog
from financecoffer.db.session import Database
from financecoffer.etl.processor import process_payload

PROVIDER = "t073-etl-reject"
SYNTHETIC_EVENT_ID_PREFIX = "invalid_"
NO_PROVIDER_EVENT_ID = "evt_t073_no_provider"


def _clean(database: Database) -> None:
    with database.session_scope() as session:
        # A rejected payload missing ``source_info`` books under the "unknown"
        # provider with its own biz key, so provider alone does not match it.
        session.execute(
            delete(EtlEventLog).where(
                or_(
                    EtlEventLog.provider == PROVIDER,
                    EtlEventLog.event_id.startswith(SYNTHETIC_EVENT_ID_PREFIX),
                    EtlEventLog.event_id == NO_PROVIDER_EVENT_ID,
                )
            )
        )


@pytest.fixture()
def db(migrated_warehouse: str) -> Iterator[Database]:
    database = Database(migrated_warehouse)
    _clean(database)
    try:
        yield database
    finally:
        try:
            _clean(database)
        finally:
            database.dispose()


def rejected_payload(*, event_id: str | None = "evt_t073_rejected") -> dict[str, Any]:
    """A quote event with no ``quality_metrics``, so validation rejects it."""

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "biz_key": "2026-10-20",
        "event_time": "2026-10-27T02:00:00Z",
        "data_type": "quote",
        "symbol": "600000",
        "market": "CN",
        "source_info": {"provider": PROVIDER},
        "content": {"structured_fields": {"trade_date": "2026-10-20", "close": "11.1100"}},
    }
    if event_id is not None:
        payload["event_id"] = event_id
    return payload


def reject(db: Database, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False)
    with pytest.raises(InvalidArgumentError):
        with db.session_scope() as session:
            process_payload(
                session,
                payload=payload,
                raw_text=raw,
                compliance_enabled=False,
            )


def rows(db: Database) -> list[EtlEventLog]:
    with db.session_scope() as session:
        statement = select(EtlEventLog).where(EtlEventLog.provider == PROVIDER)
        return list(session.scalars(statement))


def test_rejected_payload_leaves_a_failed_row_behind(db: Database) -> None:
    reject(db, rejected_payload())

    [row] = rows(db)
    assert row.event_id == "evt_t073_rejected"
    assert row.status == "failed"
    assert row.attempts == 1
    assert row.data_type == "quote"
    assert row.symbol == "600000"
    assert row.biz_key == "2026-10-20"
    assert row.error is not None
    assert row.error.startswith("InvalidArgumentError: schema: ")


def test_redelivering_the_same_invalid_payload_keeps_one_row(db: Database) -> None:
    payload = rejected_payload()

    reject(db, payload)
    reject(db, payload)

    with db.session_scope() as session:
        total = session.scalar(
            select(func.count()).select_from(EtlEventLog).where(EtlEventLog.provider == PROVIDER)
        )
    assert total == 1


def test_payload_without_event_id_gets_a_stable_synthetic_id(db: Database) -> None:
    payload = rejected_payload(event_id=None)
    payload.pop("biz_key")

    reject(db, payload)
    reject(db, payload)

    [row] = rows(db)
    assert row.event_id.startswith("invalid_")
    assert len(row.event_id) == len("invalid_") + 32
    assert row.biz_key == f"unvalidated:{row.event_id}"


def test_payload_without_provider_is_booked_under_unknown(db: Database) -> None:
    payload = rejected_payload(event_id=NO_PROVIDER_EVENT_ID)
    payload.pop("source_info")

    reject(db, payload)

    with db.session_scope() as session:
        row = session.get(EtlEventLog, NO_PROVIDER_EVENT_ID)
    assert row is not None
    assert row.provider == "unknown"
    assert row.data_type == "quote"
    assert row.biz_key == "2026-10-20"
