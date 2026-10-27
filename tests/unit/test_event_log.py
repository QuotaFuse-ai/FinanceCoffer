"""Idempotent registration decision logic (T-004).

The database constraint is what decides duplicates; these tests pin the three
reported outcomes (``new`` / ``event_id`` / ``biz_key``) without a server. The
real constraint behaviour lives in
``tests/integration/test_event_log_integration.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from financecoffer.core.errors import StorageError
from financecoffer.events import (
    ValidatedEvent,
    compute_event_id,
    register_event,
    registration,
    validate_event,
)


def event() -> ValidatedEvent:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider="eastmoney", data_type="financial", symbol="600000", biz_key="2026Q3"
        ),
        "biz_key": "2026Q3",
        "event_time": "2026-10-27T02:00:00Z",
        "data_type": "financial",
        "symbol": "600000",
        "market": "CN",
        "source_info": {"provider": "eastmoney"},
        "content": {
            "quality_metrics": {"quality_status": "normal"},
            "structured_fields": {"report_date": "2026-09-30", "report_type": "Q3"},
        },
    }
    return validate_event(payload)


@dataclass
class Row:
    event_id: str
    status: str = "received"
    attempts: int = 0


class FakeRepository:
    """Just enough of ``EventLogRepository`` to drive ``register_event``."""

    def __init__(
        self,
        *,
        claim: Row | None = None,
        get: Row | None = None,
        identity: Row | None = None,
    ) -> None:
        self.claim_row = claim
        self.get_row = get
        self.identity_row = identity
        self.calls: list[str] = []

    def claim(self, event_id: str, **_: object) -> Row | None:
        self.calls.append("claim")
        return self.claim_row

    def get(self, event_id: str) -> Row | None:
        self.calls.append("get")
        return self.get_row

    def find_by_identity(self, **_: object) -> Row | None:
        self.calls.append("find_by_identity")
        return self.identity_row

    def record_duplicate_delivery(self, event_id: str) -> Row | None:
        self.calls.append("record_duplicate_delivery")
        row = self.claim_row or self.get_row or self.identity_row
        return None if row is None else replace(row, attempts=row.attempts + 1)


def install(monkeypatch: pytest.MonkeyPatch, repository: FakeRepository) -> None:
    monkeypatch.setattr(registration, "EventLogRepository", lambda session: repository)


def test_a_first_delivery_is_new(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository(claim=Row(event().event_id))
    install(monkeypatch, repository)

    result = register_event(None, event())  # type: ignore[arg-type]

    assert result.as_dict() == {
        "event_id": event().event_id,
        "duplicate": False,
        "reason": "new",
        "status": "received",
        "attempts": 0,
    }
    assert repository.calls == ["claim"]


def test_the_same_event_id_is_a_duplicate_and_bumps_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(get=Row(event().event_id))
    install(monkeypatch, repository)

    result = register_event(None, event())  # type: ignore[arg-type]

    assert result.duplicate is True
    assert result.reason == "event_id"
    assert result.event_id == event().event_id
    assert result.attempts == 1
    assert repository.calls == ["claim", "get", "record_duplicate_delivery"]


def test_a_different_id_for_the_same_identity_is_reported_as_biz_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(identity=Row("evt_original", attempts=2))
    install(monkeypatch, repository)

    result = register_event(None, event())  # type: ignore[arg-type]

    assert result.duplicate is True
    assert result.reason == "biz_key"
    assert result.event_id == "evt_original"
    assert result.attempts == 3
    assert "find_by_identity" in repository.calls


def test_a_skipped_insert_without_a_matching_row_is_a_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, FakeRepository())

    with pytest.raises(StorageError):
        register_event(None, event())  # type: ignore[arg-type]
