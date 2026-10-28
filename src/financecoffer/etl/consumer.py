"""The consume loop: group setup, recovery, batching, retry and shutdown.

Guarantees the operator depends on:

* **at-least-once** -- a message is acknowledged only after its transaction
  committed. A crash leaves it in the group's pending list, and the next start
  reads its own pending entries before touching new ones;
* **stable name** -- the consumer name defaults to the hostname, so a restart on
  the same host reclaims its own pending entries instead of leaving them idle
  until ``XAUTOCLAIM``;
* **stale takeover** -- entries owned by a dead consumer are claimed after
  ``etl_claim_idle_ms``;
* **graceful stop** -- SIGINT/SIGTERM set a flag; the batch in flight is
  finished and acknowledged, the next read is never started;
* **bounded retry** -- transient failures retry with exponential backoff up to
  ``etl_max_attempts``, then the entry is dead-lettered and acknowledged;
  deterministic failures go to the DLQ immediately;
* **observable liveness** -- every loop iteration refreshes a short-lived Redis
  key. A reachable Redis says nothing about a consumer whose loop has stalled,
  so the container healthcheck reads that key instead of pinging Redis.

The consumer does no calculation: it validates the contract, lands the event
and writes the DWD fact. Quality fields pass through untouched.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from financecoffer.core.config import Config
from financecoffer.core.errors import DependencyError
from financecoffer.db.repositories.ops import ConsumerOffsetRepository
from financecoffer.db.session import Database
from financecoffer.etl.client import is_busy_group
from financecoffer.etl.control import is_paused
from financecoffer.etl.dlq import enqueue_failure
from financecoffer.etl.metrics import (
    METRICS,
    PENDING_MESSAGES,
    observe_lag,
    record_dirty,
    record_duplicate,
    record_event,
)
from financecoffer.etl.processor import (
    FailureInfo,
    classify_failure,
    event_id_hint,
    parse_entry_fields,
    process_payload,
)

_log = logging.getLogger(__name__)

Entry = tuple[str, dict[str, Any]]

#: Liveness key refreshed by the consume loop and read by the container
#: healthcheck. It expires on its own, so a worker that stops refreshing
#: (crashed thread, stalled loop, hard-killed process) goes red within one TTL
#: instead of hiding behind a healthy Redis.
LIVENESS_KEY_PREFIX = "fc:etl:live"

#: Floor for the key's TTL. The effective TTL also covers three blocked reads,
#: so a long ``etl_block_ms`` cannot expire the key between two iterations.
LIVENESS_TTL_SECONDS = 30


def liveness_key(stream: str) -> str:
    """Key holding the epoch seconds of the last loop iteration."""

    return f"{LIVENESS_KEY_PREFIX}:{stream}"


@dataclass(frozen=True)
class ConsumeReport:
    """What one ``etl consume`` run did, as printed in the envelope."""

    stream: str
    group: str
    consumer: str
    group_state: str
    started_at: datetime
    finished_at: datetime
    batches: int
    written: int
    duplicate: int
    dlq: int
    retries: int
    recovered: int
    claimed: int
    deferred: int
    paused: bool
    stopped: bool
    pending_after: int | None
    last_entry_id: str | None
    counts: Mapping[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "group": self.group,
            "consumer": self.consumer,
            "group_state": self.group_state,
            "started_at": _rfc3339(self.started_at),
            "finished_at": _rfc3339(self.finished_at),
            "batches": self.batches,
            "written": self.written,
            "duplicate": self.duplicate,
            "dlq": self.dlq,
            "retries": self.retries,
            "recovered": self.recovered,
            "claimed": self.claimed,
            "deferred": self.deferred,
            "paused": self.paused,
            "stopped": self.stopped,
            "pending_after": self.pending_after,
            "last_entry_id": self.last_entry_id,
        }


class Consumer:
    """One consumer instance against one stream and group."""

    def __init__(
        self,
        *,
        client: Any,
        database: Database,
        config: Config,
        consumer_name: str | None = None,
        stream: str | None = None,
        group: str | None = None,
    ) -> None:
        self._client = client
        self._database = database
        self._config = config
        self.stream = stream or config.event_stream
        self.group = group or config.event_group
        self.name = consumer_name or config.etl_consumer_name or _default_name()
        self._stop = threading.Event()
        self._counts: Counter[str] = Counter()
        self._last_entry_id: str | None = None
        self._paused = False

    def request_stop(self) -> None:
        """Ask the loop to stop after the batch in flight."""

        self._stop.set()

    def ensure_group(self) -> str:
        """Create the group if missing; ``created`` / ``exists``.

        The group starts at id ``0`` so events published before the very first
        consumer started are still consumed. On an existing group ``XGROUP
        CREATE`` answers ``BUSYGROUP``, which is the expected "already there".
        """

        try:
            self._client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - translated below
            if is_busy_group(exc):
                return "exists"
            raise DependencyError(
                "cannot create the Redis consumer group",
                details={
                    "stream": self.stream,
                    "group": self.group,
                    "hint": "check FC_REDIS_URL and that Redis is reachable",
                },
            ) from exc
        _log.info("created consumer group %s on %s", self.group, self.stream)
        return "created"

    def run(
        self,
        *,
        once: bool = False,
        batch_size: int | None = None,
        block_ms: int | None = None,
        claim_idle_ms: int | None = None,
    ) -> ConsumeReport:
        """Consume until stopped (or one batch when ``once``).

        ``block_ms=0`` means "do not block", which is what ``--once`` needs.
        """

        batch = max(1, int(batch_size or self._config.etl_batch_size))
        block = self._config.etl_block_ms if block_ms is None else max(0, int(block_ms))
        claim_idle = (
            self._config.etl_claim_idle_ms
            if claim_idle_ms is None
            else max(0, int(claim_idle_ms))
        )
        started = datetime.now(UTC)
        group_state = self.ensure_group()
        liveness_ttl = max(LIVENESS_TTL_SECONDS, int(block / 1000) * 3)

        # Restart recovery: our own pending entries are already claimed to this
        # consumer, so drain them before reading anything new.
        self._counts["recovered"] = self._drain_own_pending(batch)
        self._touch_liveness(liveness_ttl)

        while not self._stop.is_set():
            if self._control_paused():
                self._paused = True
                self._counts["paused"] += 1
                # A paused worker is still a live worker: keep the liveness key
                # fresh so pausing does not look like a stalled loop.
                self._touch_liveness(liveness_ttl)
                if once:
                    break
                self._idle_sleep(block)
                continue
            self._paused = False

            entries = self._claim_stale(claim_idle, batch)
            if entries:
                self._counts["claimed"] += len(entries)
            else:
                entries = self._read_new(batch, block)
            if not entries:
                if once:
                    break
                self._touch_liveness(liveness_ttl)
                continue

            self._counts["batches"] += 1
            self._handle_batch(entries)
            if once:
                break
            self._touch_liveness(liveness_ttl)

        finished = datetime.now(UTC)
        pending_after = self._pending_total()
        if pending_after is not None:
            METRICS.set(PENDING_MESSAGES, pending_after)
        return ConsumeReport(
            stream=self.stream,
            group=self.group,
            consumer=self.name,
            group_state=group_state,
            started_at=started,
            finished_at=finished,
            batches=self._counts["batches"],
            written=self._counts["written"],
            duplicate=self._counts["duplicate"],
            dlq=self._counts["dlq"],
            retries=self._counts["retries"],
            recovered=self._counts["recovered"],
            claimed=self._counts["claimed"],
            deferred=self._counts["deferred"],
            paused=self._paused,
            stopped=self._stop.is_set(),
            pending_after=pending_after,
            last_entry_id=self._last_entry_id,
            counts=dict(self._counts),
        )

    # -- reads ------------------------------------------------------------

    def _control_paused(self) -> bool:
        try:
            return is_paused(self._client, self._config.etl_control_key)
        except Exception as exc:  # noqa: BLE001 - translated to a CLI error
            raise DependencyError(
                "cannot read the ETL control key",
                details={
                    "key": self._config.etl_control_key,
                    "hint": "check FC_REDIS_URL and that Redis is reachable",
                },
            ) from exc

    def _drain_own_pending(self, batch: int) -> int:
        total = 0
        for _ in range(1000):
            if self._stop.is_set():
                break
            entries = self._read_pending(batch)
            if not entries:
                break
            total += len(entries)
            self._handle_batch(entries)
        return total

    def _read_pending(self, batch: int) -> list[Entry]:
        response = self._call(
            lambda: self._client.xreadgroup(
                self.group, self.name, {self.stream: "0"}, count=batch
            )
        )
        return _entries_from_read(response)

    def _read_new(self, batch: int, block: int) -> list[Entry]:
        response = self._call(
            lambda: self._client.xreadgroup(
                self.group,
                self.name,
                {self.stream: ">"},
                count=batch,
                block=block if block > 0 else None,
            )
        )
        return _entries_from_read(response)

    def _claim_stale(self, claim_idle: int, batch: int) -> list[Entry]:
        if claim_idle <= 0:
            return []
        response = self._call(
            lambda: self._client.xautoclaim(
                self.stream,
                self.group,
                self.name,
                min_idle_time=claim_idle,
                start_id="0-0",
                count=batch,
            )
        )
        return _entries_from_claim(response)

    def _call(self, operation: Any) -> Any:
        """Run a Redis call, recreating the group once if it disappeared."""

        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - classified below
            if _is_no_group(exc):
                _log.warning("consumer group %s disappeared; recreating", self.group)
                self.ensure_group()
                return None
            raise DependencyError(
                "Redis stream operation failed",
                details={"stream": self.stream, "group": self.group},
            ) from exc

    # -- handling ---------------------------------------------------------

    def _handle_batch(self, entries: Sequence[Entry]) -> None:
        """Process every entry in the batch, then persist the offset.

        A stop request during the batch does not abandon entries already in
        our pending list: they are handled now, which is what makes shutdown
        graceful rather than lossy.
        """

        for entry_id, fields in entries:
            self._handle_entry(entry_id, fields)
            self._last_entry_id = entry_id
        self._save_offset()

    def _handle_entry(self, entry_id: str, fields: Mapping[str, Any]) -> str:
        attempts = 0
        payload: Mapping[str, Any] | None = None
        max_attempts = self._config.etl_max_attempts

        while True:
            attempts += 1
            try:
                payload, raw_text = parse_entry_fields(fields)
                with self._database.session_scope() as session:
                    result = process_payload(
                        session,
                        payload=payload,
                        raw_text=raw_text,
                        compliance_enabled=self._config.compliance_enabled,
                    )
            except Exception as exc:  # noqa: BLE001 - every failure is classified
                failure = classify_failure(exc)
                if failure.retryable and attempts < max_attempts:
                    if self._stop.is_set() or self._backoff(attempts):
                        _log.info(
                            "leaving %s pending: shutdown requested before retry %d",
                            entry_id,
                            attempts + 1,
                        )
                        self._counts["deferred"] += 1
                        record_event("deferred")
                        return "deferred"
                    self._counts["retries"] += 1
                    _log.warning(
                        "retrying %s after %s (%d/%d): %s",
                        entry_id,
                        failure.reason,
                        attempts,
                        max_attempts,
                        failure.message,
                    )
                    continue
                return self._dead_letter(entry_id, fields, failure, attempts, payload)

            self._ack(entry_id)
            self._observe_lag(entry_id)
            if result.written:
                self._counts["written"] += 1
                record_event("written")
                return "written"
            self._counts["duplicate"] += 1
            record_duplicate()
            record_event("duplicate")
            return "duplicate"

    def _dead_letter(
        self,
        entry_id: str,
        fields: Mapping[str, Any],
        failure: FailureInfo,
        attempts: int,
        payload: Mapping[str, Any] | None,
    ) -> str:
        try:
            enqueue_failure(
                client=self._client,
                database=self._database,
                config=self._config,
                source_stream=self.stream,
                entry_id=entry_id,
                fields=fields,
                failure=failure,
                attempts=attempts,
                event_id=event_id_hint(payload),
            )
        except Exception as exc:  # noqa: BLE001 - keep the message pending
            _log.error(
                "could not persist DLQ entry for %s (%s); leaving it pending",
                entry_id,
                exc,
            )
            self._counts["dlq_failed"] += 1
            record_event("failed")
            return "dlq_failed"

        self._ack(entry_id)
        self._counts["dlq"] += 1
        record_event("dlq")
        record_dirty(failure.reason)
        return "dlq"

    def _ack(self, entry_id: str) -> None:
        try:
            self._client.xack(self.stream, self.group, entry_id)
        except Exception as exc:  # noqa: BLE001 - redelivery is safe
            _log.warning("could not acknowledge %s: %s", entry_id, exc)

    def _backoff(self, attempt: int) -> bool:
        seconds = self._config.etl_backoff_ms * (2 ** (attempt - 1)) / 1000
        if seconds <= 0:
            return self._stop.is_set()
        return self._stop.wait(seconds)

    def _idle_sleep(self, block: int) -> None:
        self._stop.wait(block / 1000 if block > 0 else 1.0)

    def _touch_liveness(self, ttl_seconds: int) -> None:
        """Refresh the liveness key; bookkeeping never fails a consume run."""

        try:
            self._client.set(
                liveness_key(self.stream), str(int(time.time())), ex=ttl_seconds
            )
        except Exception as exc:  # noqa: BLE001 - health reporting is best effort
            _log.debug("could not refresh the liveness key: %s", exc)

    def _observe_lag(self, entry_id: str) -> None:
        try:
            millis = int(entry_id.split("-", 1)[0])
        except (AttributeError, ValueError):
            return
        observe_lag(time.time() - millis / 1000)

    def _save_offset(self) -> None:
        if not self._last_entry_id:
            return
        try:
            with self._database.session_scope() as session:
                ConsumerOffsetRepository(session).save(
                    consumer_group=self.group,
                    stream=self.stream,
                    last_event_id=self._last_entry_id,
                )
        except Exception as exc:  # noqa: BLE001 - XACK is the authoritative cursor
            _log.warning("could not persist the consumer offset: %s", exc)

    def _pending_total(self) -> int | None:
        try:
            summary = self._client.xpending(self.stream, self.group)
        except Exception:  # noqa: BLE001 - reporting must not fail a run
            return None
        if not summary:
            return 0
        try:
            return int(summary.get("pending", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return None


@contextmanager
def graceful_signals(consumer: Consumer) -> Iterator[None]:
    """Turn SIGINT/SIGTERM into a graceful stop for the duration of ``run``."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        _log.info("received signal %s; finishing the current batch", signum)
        consumer.request_stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _handler)
        except (ValueError, OSError):  # pragma: no cover - platform dependent
            continue
    try:
        yield
    finally:
        for number, handler in previous.items():
            try:
                signal.signal(number, handler)
            except (ValueError, OSError):  # pragma: no cover - platform dependent
                continue


def _default_name() -> str:
    host = socket.gethostname().strip() or "localhost"
    return host[:200]


def _entries_from_read(response: Any) -> list[Entry]:
    if not response:
        return []
    entries: list[Entry] = []
    for _stream, items in response:
        for item in items or []:
            entry_id, fields = item
            entries.append((entry_id, dict(fields or {})))
    return entries


def _entries_from_claim(response: Any) -> list[Entry]:
    if not response:
        return []
    # redis-py returns (next_start_id, entries, deleted_ids); older servers may
    # answer a two-element list.
    items = response[1] if len(response) > 1 else []
    entries: list[Entry] = []
    for item in items or []:
        if not item:
            continue
        entry_id, fields = item
        entries.append((entry_id, dict(fields or {})))
    return entries


def _is_no_group(exc: BaseException) -> bool:
    return "NOGROUP" in str(exc)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["ConsumeReport", "Consumer", "graceful_signals"]
