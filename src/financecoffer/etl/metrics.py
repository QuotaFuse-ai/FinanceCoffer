"""In-process ETL metrics named after the metrics spec.

``prometheus_client`` is not a dependency, so counters and gauges live in a
small thread-safe registry. Values are per process; ``etl status`` reports the
durable numbers (pending, DLQ depth, lag) by reading Redis and PostgreSQL
instead of trusting this registry across processes.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

EVENTS_TOTAL = "fc_etl_events_total"
DIRTY_DATA_TOTAL = "fc_dirty_data_total"
DUPLICATE_EVENTS_TOTAL = "fc_duplicate_event_total"
CONSUME_LAG_SECONDS = "fc_etl_consume_lag_seconds"
PENDING_MESSAGES = "fc_etl_pending_messages"
DLQ_DEPTH = "fc_etl_dlq_depth"

METRIC_TYPES: Mapping[str, str] = {
    EVENTS_TOTAL: "counter",
    DIRTY_DATA_TOTAL: "counter",
    DUPLICATE_EVENTS_TOTAL: "counter",
    CONSUME_LAG_SECONDS: "gauge",
    PENDING_MESSAGES: "gauge",
    DLQ_DEPTH: "gauge",
}

Labels = Mapping[str, str]
_Key = tuple[str, tuple[tuple[str, str], ...]]


def _key(name: str, labels: Labels | None) -> _Key:
    if not labels:
        return name, ()
    return name, tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _render_key(key: _Key) -> str:
    name, labels = key
    if not labels:
        return name
    body = ",".join(f'{label}="{_escape(value)}"' for label, value in labels)
    return f"{name}{{{body}}}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class MetricsRegistry:
    """Thread-safe counters and gauges keyed by metric name and label set."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[_Key, float] = {}

    def inc(self, name: str, *, labels: Labels | None = None, amount: float = 1.0) -> None:
        key = _key(name, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def set(self, name: str, value: float, *, labels: Labels | None = None) -> None:
        with self._lock:
            self._values[_key(name, labels)] = float(value)

    def get(self, name: str, *, labels: Labels | None = None) -> float:
        with self._lock:
            return self._values.get(_key(name, labels), 0.0)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            items = sorted(self._values.items())
        return {_render_key(key): value for key, value in items}

    def render(self) -> str:
        """Render the Prometheus text exposition format."""

        with self._lock:
            items = sorted(self._values.items())
        emitted: set[str] = set()
        lines: list[str] = []
        for key, value in items:
            name = key[0]
            if name not in emitted:
                emitted.add(name)
                lines.append(f"# TYPE {name} {METRIC_TYPES.get(name, 'gauge')}")
            lines.append(f"{_render_key(key)} {value}")
        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        with self._lock:
            self._values.clear()


METRICS = MetricsRegistry()


def record_event(status: str) -> None:
    METRICS.inc(EVENTS_TOTAL, labels={"status": status})


def record_dirty(reason: str) -> None:
    METRICS.inc(DIRTY_DATA_TOTAL, labels={"reason": reason})


def record_duplicate() -> None:
    METRICS.inc(DUPLICATE_EVENTS_TOTAL)


def observe_lag(seconds: float) -> None:
    METRICS.set(CONSUME_LAG_SECONDS, max(0.0, seconds))


def set_pending(count: int) -> None:
    METRICS.set(PENDING_MESSAGES, count)


def set_dlq_depth(count: int) -> None:
    METRICS.set(DLQ_DEPTH, count)


__all__ = [
    "CONSUME_LAG_SECONDS",
    "DIRTY_DATA_TOTAL",
    "DLQ_DEPTH",
    "DUPLICATE_EVENTS_TOTAL",
    "EVENTS_TOTAL",
    "METRICS",
    "METRIC_TYPES",
    "MetricsRegistry",
    "PENDING_MESSAGES",
    "observe_lag",
    "record_dirty",
    "record_duplicate",
    "record_event",
    "set_dlq_depth",
    "set_pending",
]
