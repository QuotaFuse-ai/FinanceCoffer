"""ETL consumer behaviour against an in-memory Redis stand-in.

The consumer talks to ``XREADGROUP`` / ``XACK`` / ``XAUTOCLAIM`` / ``XADD`` and
to ``Database.session_scope``. This module replaces both: ``FakeRedis`` models
the stream, the group and its pending list closely enough to check the
delivery guarantees, and ``process_payload`` is patched so the loop can be
driven through every outcome (written, duplicate, dead letter, retry, deferral)
without PostgreSQL. The real storage path is covered in
``tests/integration/test_etl_integration.py``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
import redis
from sqlalchemy.exc import SQLAlchemyError

from financecoffer.core.config import Config
from financecoffer.core.errors import (
    DependencyError,
    InvalidArgumentError,
    NotFoundError,
    NotImplementedYetError,
    StorageError,
)
from financecoffer.etl import consumer as consumer_module
from financecoffer.etl import status as status_module
from financecoffer.etl.consumer import Consumer
from financecoffer.etl.control import control_state, is_paused, pause, resume
from financecoffer.etl.dlq import DlqRecord, entry_fields, retry_dlq
from financecoffer.etl.metrics import (
    DUPLICATE_EVENTS_TOTAL,
    EVENTS_TOTAL,
    METRICS,
    MetricsRegistry,
)
from financecoffer.etl.processor import (
    FailureInfo,
    ProcessResult,
    classify_failure,
    parse_entry_fields,
)
from financecoffer.etl.status import collect_status

# -- fakes -------------------------------------------------------------------


class FakeRedis:
    """A small, string-only model of the Redis stream commands we use."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], dict[str, Any]] = {}
        self.pending: dict[tuple[str, str], dict[str, str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.claimable: list[tuple[str, dict[str, str]]] = []
        self.acked: list[str] = []
        self.deleted: list[str] = []
        self.group_creates = 0
        self.fail_next: dict[str, BaseException] = {}
        self._seq = 0

    # -- lifecycle -----------------------------------------------------
    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None

    # -- streams -------------------------------------------------------
    def _next_id(self) -> str:
        self._seq += 1
        return f"{1_700_000_000_000 + self._seq}-0"

    def xadd(self, stream: str, fields: Mapping[str, Any]) -> str:
        entry_id = self._next_id()
        record = {str(key): str(value) for key, value in fields.items()}
        self.streams.setdefault(stream, []).append((entry_id, record))
        return entry_id

    def xlen(self, stream: str) -> int:
        return len(self.streams.get(stream, []))

    def xdel(self, stream: str, *entry_ids: str) -> int:
        self.deleted.extend(entry_ids)
        before = len(self.streams.get(stream, []))
        self.streams[stream] = [
            (entry_id, fields)
            for entry_id, fields in self.streams.get(stream, [])
            if entry_id not in entry_ids
        ]
        return before - len(self.streams[stream])

    def xgroup_create(
        self, stream: str, group: str, id: str = "0", mkstream: bool = False
    ) -> bool:
        key = (stream, group)
        if key in self.groups:
            raise redis.exceptions.ResponseError(
                "BUSYGROUP Consumer Group name already exists"
            )
        if mkstream:
            self.streams.setdefault(stream, [])
        self.groups[key] = {"last": id, "delivered": set()}
        self.group_creates += 1
        return True

    def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: Mapping[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        stream, start = next(iter(streams.items()))
        key = (stream, group)
        if key not in self.groups:
            raise RuntimeError("NOGROUP No such key or consumer group")
        failure = self.fail_next.pop("xreadgroup", None)
        if failure is not None:
            self.groups.pop(key, None)
            raise failure

        if start == ">":
            delivered: set[str] = self.groups[key]["delivered"]
            entries = [
                (entry_id, dict(fields))
                for entry_id, fields in self.streams.get(stream, [])
                if entry_id not in delivered
            ]
            delivered.update(entry_id for entry_id, _ in entries)
        else:
            own = self.pending.get(key, {})
            entries = [
                (entry_id, dict(fields))
                for entry_id, fields in self.streams.get(stream, [])
                if own.get(entry_id) == consumer
            ]

        take = entries[: count if count is not None else len(entries)]
        bucket = self.pending.setdefault(key, {})
        for entry_id, _ in take:
            bucket[entry_id] = consumer
        return [(stream, take)] if take else []

    def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.acked.append(entry_id)
        bucket = self.pending.get((stream, group), {})
        return 1 if bucket.pop(entry_id, None) is not None else 0

    def xpending(self, stream: str, group: str) -> dict[str, Any]:
        bucket = self.pending.get((stream, group), {})
        owners = Counter(bucket.values())
        return {
            "pending": len(bucket),
            "min": None,
            "max": None,
            "consumers": [{"name": name, "pending": count} for name, count in owners.items()],
        }

    def xautoclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_time: int,
        start_id: str = "0-0",
        count: int | None = None,
    ) -> list[Any]:
        take = self.claimable[: count if count is not None else len(self.claimable)]
        self.claimable = self.claimable[len(take) :]
        bucket = self.pending.setdefault((stream, group), {})
        for entry_id, _ in take:
            bucket[entry_id] = consumer
        return ["0-0", take, []]

    def xinfo_groups(self, stream: str) -> list[dict[str, Any]]:
        length = len(self.streams.get(stream, []))
        return [
            {
                "name": group,
                "last-delivered-id": info["last"],
                "lag": 0,
                "entries-read": length,
            }
            for (stream_name, group), info in self.groups.items()
            if stream_name == stream
        ]

    def xinfo_consumers(self, stream: str, group: str) -> list[dict[str, Any]]:
        bucket = self.pending.get((stream, group), {})
        owners = Counter(bucket.values())
        return [
            {"name": name, "pending": count, "idle": 0} for name, count in owners.items()
        ]

    # -- hashes --------------------------------------------------------
    def hset(self, key: str, mapping: Mapping[str, Any] | None = None, **kwargs: Any) -> int:
        values = dict(mapping or {})
        values.update(kwargs)
        bucket = self.hashes.setdefault(key, {})
        fresh = sum(1 for field in values if field not in bucket)
        bucket.update({str(field): str(value) for field, value in values.items()})
        return fresh

    def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))


class FakeDatabase:
    """A session factory that hands out ``None``; patched payloads never use it."""

    def __init__(self) -> None:
        self.sessions = 0

    @contextmanager
    def session_scope(self) -> Iterator[None]:
        self.sessions += 1
        yield None


class FailingDatabase:
    """Every session attempt fails, as it would with PostgreSQL unreachable."""

    @contextmanager
    def session_scope(self) -> Iterator[None]:
        raise RuntimeError("database is down")
        yield None  # pragma: no cover - keeps this a generator


class StubDlqRepository:
    def __init__(self, session: Any) -> None:
        self.session = session

    def depth(self) -> int:
        return 0

    def latest(self, *, limit: int = 1) -> list[Any]:
        return []


# -- helpers -----------------------------------------------------------------


def make_config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "event_stream": "unit:events",
        "event_group": "unit-group",
        "etl_dlq_stream": "unit:dlq",
        "etl_control_key": "unit:control",
        "etl_batch_size": 10,
        "etl_block_ms": 0,
        "etl_backoff_ms": 0,
        "etl_claim_idle_ms": 0,
        "etl_max_attempts": 3,
    }
    base.update(overrides)
    return Config(**base)


def written(event_id: str = "evt_unit") -> ProcessResult:
    return ProcessResult(
        status="written",
        event_id=event_id,
        reason="new",
        event_log_status="done",
        ods_id=1,
        dwd_table="dwd.stock_quote_snapshot",
        dwd_version=1,
        dwd_id=1,
    )


def duplicate(event_id: str = "evt_unit") -> ProcessResult:
    return ProcessResult(
        status="duplicate",
        event_id=event_id,
        reason="event_id",
        event_log_status="done",
    )


@dataclass
class Harness:
    client: FakeRedis
    database: FakeDatabase
    config: Config
    consumer: Consumer
    processed: list[Mapping[str, Any]]
    dead_letters: list[dict[str, Any]]

    def publish(self, payload: str = "{}") -> str:
        return self.client.xadd(self.config.event_stream, {"payload": payload})


def build_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: Config | None = None,
    process: Callable[..., ProcessResult] | None = None,
    name: str = "unit-consumer",
) -> Harness:
    client = FakeRedis()
    database = FakeDatabase()
    effective = config or make_config()
    instance = Consumer(
        client=client,
        database=database,
        config=effective,
        consumer_name=name,
    )
    processed: list[Mapping[str, Any]] = []
    dead_letters: list[dict[str, Any]] = []

    def default_process(session: Any, *, payload: Mapping[str, Any], **_: Any) -> ProcessResult:
        processed.append(payload)
        return written()

    handler = process or default_process

    def record(session: Any, *, payload: Mapping[str, Any], **kwargs: Any) -> ProcessResult:
        return handler(session, payload=payload, **kwargs)

    def enqueue(**kwargs: Any) -> DlqRecord:
        dead_letters.append(kwargs)
        return DlqRecord(
            dlq_id=len(dead_letters),
            event_id=kwargs.get("event_id"),
            reason=kwargs["failure"].reason,
            attempts=kwargs["attempts"],
            stream_entry_id="dlq-1",
            dirty=True,
        )

    saved: list[str | None] = []

    def save_offset() -> None:
        saved.append(instance._last_entry_id)

    monkeypatch.setattr(consumer_module, "process_payload", record)
    monkeypatch.setattr(consumer_module, "enqueue_failure", enqueue)
    monkeypatch.setattr(instance, "_save_offset", save_offset)
    instance.saved_offsets = saved  # type: ignore[attr-defined]

    return Harness(
        client=client,
        database=database,
        config=effective,
        consumer=instance,
        processed=processed,
        dead_letters=dead_letters,
    )


@pytest.fixture(autouse=True)
def reset_metrics() -> Iterator[None]:
    METRICS.reset()
    yield
    METRICS.reset()


# -- parsing and classification ---------------------------------------------


def test_parse_entry_fields_returns_the_exact_upstream_text() -> None:
    text = '{"event_id": "evt_1", "content": {"close": "1.00"}}'

    payload, raw = parse_entry_fields({"payload": text, "extra": "ignored"})

    assert raw == text
    assert payload["event_id"] == "evt_1"


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({}, "payload"),
        ({"payload": ""}, "payload"),
        ({"payload": "not json"}, "payload"),
        ({"payload": "[1, 2]"}, "payload"),
    ],
)
def test_parse_entry_fields_rejects_bad_entries(
    fields: dict[str, str], reason: str
) -> None:
    with pytest.raises(InvalidArgumentError) as caught:
        parse_entry_fields(fields)

    assert caught.value.details is not None
    assert caught.value.details["reason"] == reason


@pytest.mark.parametrize(
    ("error", "reason", "retryable"),
    [
        (InvalidArgumentError("bad", details={"reason": "schema"}), "schema", False),
        (InvalidArgumentError("bad"), "validation", False),
        (NotImplementedYetError("later"), "unsupported", False),
        (StorageError("disk full"), "storage", True),
        (DependencyError("redis down"), "dependency", True),
        (SQLAlchemyError("boom"), "storage", True),
        (RuntimeError("boom"), "internal", True),
    ],
)
def test_classify_failure_marks_deterministic_errors_as_final(
    error: BaseException, reason: str, retryable: bool
) -> None:
    failure = classify_failure(error)

    assert failure.reason == reason
    assert failure.retryable is retryable


# -- control plane and metrics ----------------------------------------------


def test_control_key_round_trip_keeps_reason_and_state() -> None:
    client = FakeRedis()

    assert is_paused(client, "kc") is False
    paused = pause(client, "kc", reason="nightly load")
    assert paused == {
        "paused": True,
        "changed_at": paused["changed_at"],
        "reason": "nightly load",
    }
    assert is_paused(client, "kc") is True

    resumed = resume(client, "kc")
    assert resumed["paused"] is False
    assert control_state(client, "kc")["paused"] is False


def test_metrics_registry_counts_gauges_and_renders_text() -> None:
    registry = MetricsRegistry()
    registry.inc(EVENTS_TOTAL, labels={"status": "written"})
    registry.inc(EVENTS_TOTAL, labels={"status": "written"})
    registry.set(DUPLICATE_EVENTS_TOTAL, 3)

    assert registry.get(EVENTS_TOTAL, labels={"status": "written"}) == 2
    assert registry.snapshot() == {
        f'{EVENTS_TOTAL}{{status="written"}}': 2.0,
        DUPLICATE_EVENTS_TOTAL: 3.0,
    }
    text = registry.render()
    assert f"# TYPE {EVENTS_TOTAL} counter" in text
    assert f'{EVENTS_TOTAL}{{status="written"}} 2.0' in text


def test_entry_fields_stringifies_values_and_records_the_source_entry() -> None:
    fields = entry_fields("1-0", {"payload": "{}", "attempts": 2})

    assert fields == {"payload": "{}", "attempts": "2", "source_entry_id": "1-0"}


# -- consume loop ------------------------------------------------------------


def test_consumer_name_prefers_the_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = build_harness(monkeypatch, name="worker-a")

    assert harness.consumer.name == "worker-a"

    configured = Consumer(
        client=harness.client,
        database=harness.database,
        config=make_config(etl_consumer_name="from-config"),
    )
    assert configured.name == "from-config"

    default = Consumer(
        client=harness.client, database=harness.database, config=make_config()
    )
    assert default.name


def test_ensure_group_creates_once_then_reports_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(monkeypatch)

    assert harness.consumer.ensure_group() == "created"
    monkeypatch.setattr(consumer_module, "is_busy_group", lambda exc: "BUSYGROUP" in str(exc))
    assert harness.consumer.ensure_group() == "exists"


def test_a_valid_event_is_written_and_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(monkeypatch)
    entry_id = harness.publish('{"event_id": "evt_unit"}')

    report = harness.consumer.run(once=True, block_ms=0)

    assert report.written == 1
    assert report.duplicate == 0
    assert report.dlq == 0
    assert report.pending_after == 0
    assert harness.client.acked == [entry_id]
    assert harness.consumer.saved_offsets == [entry_id]  # type: ignore[attr-defined]
    assert METRICS.get(EVENTS_TOTAL, labels={"status": "written"}) == 1


def test_a_duplicate_event_is_acknowledged_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(monkeypatch, process=lambda *a, **k: duplicate())
    harness.publish('{"event_id": "evt_unit"}')

    report = harness.consumer.run(once=True, block_ms=0)

    assert report.written == 0
    assert report.duplicate == 1
    assert report.pending_after == 0
    assert len(harness.client.acked) == 1
    assert METRICS.get(DUPLICATE_EVENTS_TOTAL) == 1
    assert METRICS.get(EVENTS_TOTAL, labels={"status": "duplicate"}) == 1


def test_a_deterministic_failure_goes_straight_to_the_dlq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*args: Any, **kwargs: Any) -> ProcessResult:
        raise InvalidArgumentError("bad contract", details={"reason": "schema"})

    harness = build_harness(monkeypatch, process=reject)
    entry_id = harness.publish('{"event_id": "evt_bad"}')

    report = harness.consumer.run(once=True, block_ms=0)

    assert report.dlq == 1
    assert report.retries == 0
    assert report.written == 0
    assert harness.client.acked == [entry_id]
    assert harness.dead_letters[0]["attempts"] == 1
    assert harness.dead_letters[0]["failure"].reason == "schema"
    assert harness.dead_letters[0]["event_id"] == "evt_bad"


def test_a_transient_failure_retries_up_to_the_limit_then_dead_letters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def flaky(*args: Any, **kwargs: Any) -> ProcessResult:
        calls.append(len(calls) + 1)
        raise StorageError("primary is read-only")

    harness = build_harness(
        monkeypatch,
        config=make_config(etl_max_attempts=3),
        process=flaky,
    )
    harness.publish('{"event_id": "evt_flaky"}')

    report = harness.consumer.run(once=True, block_ms=0)

    assert len(calls) == 3
    assert report.retries == 2
    assert report.dlq == 1
    assert harness.dead_letters[0]["attempts"] == 3
    assert len(harness.client.acked) == 1


def test_a_stop_request_during_backoff_defers_the_entry_and_keeps_it_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness: dict[str, Harness] = {}

    def stop_then_fail(*args: Any, **kwargs: Any) -> ProcessResult:
        harness["h"].consumer.request_stop()
        raise StorageError("temporary")

    harness["h"] = build_harness(
        monkeypatch,
        config=make_config(etl_max_attempts=3),
        process=stop_then_fail,
    )
    entry_id = harness["h"].publish('{"event_id": "evt_defer"}')

    report = harness["h"].consumer.run(once=True, block_ms=0)

    assert report.deferred == 1
    assert report.dlq == 0
    assert report.retries == 0
    assert harness["h"].client.acked == []
    assert harness["h"].client.xpending("unit:events", "unit-group")["pending"] == 1
    assert report.pending_after == 1
    assert entry_id


def test_a_stop_request_mid_batch_still_finishes_every_claimed_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness: dict[str, Harness] = {}
    seen: list[str] = []

    def stop_on_first(session: Any, *, payload: Mapping[str, Any], **kwargs: Any) -> ProcessResult:
        seen.append(str(payload["event_id"]))
        if len(seen) == 1:
            harness["h"].consumer.request_stop()
        return written(str(payload["event_id"]))

    harness["h"] = build_harness(monkeypatch, process=stop_on_first)
    first = harness["h"].publish('{"event_id": "evt_a"}')
    second = harness["h"].publish('{"event_id": "evt_b"}')

    report = harness["h"].consumer.run(block_ms=0)

    assert seen == ["evt_a", "evt_b"]
    assert report.written == 2
    assert report.stopped is True
    assert harness["h"].client.acked == [first, second]
    assert report.pending_after == 0


def test_a_paused_consumer_does_not_read_new_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(monkeypatch)
    pause(harness.client, "unit:control", reason="maintenance")
    harness.publish('{"event_id": "evt_waiting"}')

    report = harness.consumer.run(once=True, block_ms=0)

    assert report.paused is True
    assert report.written == 0
    assert harness.processed == []
    assert harness.client.xpending("unit:events", "unit-group")["pending"] == 0
    assert harness.client.xlen("unit:events") == 1


def test_a_stale_entry_is_claimed_processed_and_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(
        monkeypatch,
        config=make_config(etl_claim_idle_ms=1),
    )
    entry_id = harness.client.xadd("unit:events", {"payload": '{"event_id": "evt_stale"}'})
    harness.client.claimable.append((entry_id, {"payload": '{"event_id": "evt_stale"}'}))

    report = harness.consumer.run(once=True, block_ms=0)

    assert report.claimed == 1
    assert report.written == 1
    assert harness.client.acked == [entry_id]


def test_a_disappeared_group_is_recreated_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(monkeypatch)
    harness.client.fail_next["xreadgroup"] = RuntimeError("NOGROUP gone")

    report = harness.consumer.run(once=True, block_ms=0)

    assert report.group_state == "created"
    assert harness.client.group_creates == 2


def test_reading_while_paused_is_translated_to_a_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(monkeypatch)

    def broken_hget(key: str, field: str) -> str | None:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(harness.client, "hget", broken_hget)

    with pytest.raises(DependencyError) as caught:
        harness.consumer.run(once=True, block_ms=0)

    assert "control key" in caught.value.message


# -- status ------------------------------------------------------------------


def test_entries_from_read_and_claim_accept_both_redis_shapes() -> None:
    from financecoffer.etl.consumer import _entries_from_claim, _entries_from_read

    assert _entries_from_read(None) == []
    assert _entries_from_read([("unit:events", [("1-0", {"payload": "{}"})])]) == [
        ("1-0", {"payload": "{}"})
    ]
    assert _entries_from_claim(["9-0", [("2-0", {"payload": "{}"})], []]) == [
        ("2-0", {"payload": "{}"})
    ]
    assert _entries_from_claim(["9-0", [("3-0", {"payload": "{}"})]]) == [
        ("3-0", {"payload": "{}"})
    ]
    assert _entries_from_claim(None) == []


def test_status_reports_a_missing_group_without_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedis()
    monkeypatch.setattr(status_module, "DlqRepository", StubDlqRepository)

    snapshot = collect_status(
        client=client, database=FakeDatabase(), config=make_config()
    )

    assert snapshot["lag"] is None
    assert snapshot["pending"] == 0
    assert snapshot["healthy"] is False
    assert snapshot["errors"] == [
        {
            "area": "redis",
            "reason": "group_missing",
            "message": "consumer group 'unit-group' does not exist yet",
        }
    ]


def test_status_numbers_come_from_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(monkeypatch)
    monkeypatch.setattr(status_module, "DlqRepository", StubDlqRepository)
    harness.publish('{"event_id": "evt_unit"}')
    harness.consumer.run(once=True, block_ms=0)

    snapshot = collect_status(
        client=harness.client,
        database=harness.database,
        config=harness.config,
        consumer_name="unit-consumer",
    )

    assert snapshot["healthy"] is True
    assert snapshot["pending"] == harness.client.xpending("unit:events", "unit-group")["pending"]
    assert snapshot["pending"] == 0
    assert snapshot["lag"] == 0
    assert snapshot["dlq"] == {"stream": "unit:dlq", "stream_depth": 0, "database_pending": 0}
    assert snapshot["last_error"] is None


def test_status_survives_redis_and_database_outages() -> None:
    class BrokenRedis:
        def hgetall(self, key: str) -> dict[str, str]:
            raise RuntimeError("redis down")

    snapshot = collect_status(
        client=BrokenRedis(), database=FailingDatabase(), config=make_config()
    )

    areas = [item["area"] for item in snapshot["errors"]]
    assert areas == ["redis", "database"]
    assert snapshot["healthy"] is False
    assert snapshot["pending"] == 0


# -- DLQ argument handling ---------------------------------------------------


def test_retry_requires_exactly_one_target_for_a_corrected_payload() -> None:
    with pytest.raises(NotFoundError):
        retry_dlq(
            client=None,
            database=None,  # type: ignore[arg-type]
            config=make_config(),
            payload={"event_id": "evt_fixed"},
        )


def test_retry_rejects_a_corrected_payload_for_every_entry() -> None:
    with pytest.raises(NotFoundError):
        retry_dlq(
            client=None,
            database=None,  # type: ignore[arg-type]
            config=make_config(),
            payload={"event_id": "evt_fixed"},
            all_pending=True,
        )


def test_failure_info_as_dict_is_stable() -> None:
    info = FailureInfo(reason="schema", message="bad", retryable=False)

    assert info.as_dict() == {"reason": "schema", "message": "bad", "retryable": False}
