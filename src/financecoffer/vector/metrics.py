"""In-process Prometheus metrics for the vector layer.

The metric names are part of ``docs/specs/financecoffer.md`` section 7:
``fc_missing_chunk_total`` counts chunks that could not be embedded, and
``fc_vector_search_seconds`` is the hybrid-search latency histogram. Chunk and
embedding throughput use two additional counters so a failed run is visible
without subtracting totals.

The registry is a process-local object: ``vector embed`` and ``/metrics`` live in
different processes, so the CLI writes the numbers to its own registry and the
API scrapes its own. That matches how the rest of the project reports counters
(``fc_api_*``) and keeps the CLI free of a metrics server.
"""

from __future__ import annotations

import math
import threading
from collections import Counter
from typing import Any

SEARCH_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: Label values this layer itself mints; rendered at zero before they occur so
#: the family never disappears from ``/metrics`` (QUOT-127). Reasons raised by
#: the embedding client are appended with their real counts when they happen.
MISSING_CHUNK_REASONS: tuple[str, ...] = ("embedding_failed",)
CHUNK_STATUSES: tuple[str, ...] = ("written", "deleted")
EMBEDDING_STATUSES: tuple[str, ...] = ("ok", "rejected")


def _with_baseline(
    baseline: tuple[str, ...], observed: list[tuple[str, int]]
) -> list[tuple[str, int]]:
    """Return ``baseline`` at zero, overlaid with (and extended by) ``observed``."""

    counts = dict.fromkeys(baseline, 0)
    for key, value in observed:
        counts[key] = value
    return sorted(counts.items())


def _format_seconds(value: float) -> str:
    return f"{value:.6f}"


class VectorMetrics:
    """Thread-safe counters and one latency histogram, rendered as text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._missing: Counter[str] = Counter()
        self._chunks: Counter[str] = Counter()
        self._embeddings: Counter[str] = Counter()
        self._search_count = 0
        self._search_sum = 0.0
        # Cumulative bucket counts, one per upper bound; ``+Inf`` is the count.
        self._buckets = [0] * (len(SEARCH_BUCKETS) + 1)

    def reset(self) -> None:
        """Clear every series (tests use this between cases)."""

        with self._lock:
            self._missing.clear()
            self._chunks.clear()
            self._embeddings.clear()
            self._search_count = 0
            self._search_sum = 0.0
            self._buckets = [0] * (len(SEARCH_BUCKETS) + 1)

    def missing_chunk(self, reason: str = "embedding_failed", count: int = 1) -> None:
        """Count chunks lost because embedding or slicing failed."""

        if count <= 0:
            return
        with self._lock:
            self._missing[reason] += int(count)

    def chunk(self, status: str = "written", count: int = 1) -> None:
        """Count chunk rows written or deleted."""

        if count <= 0:
            return
        with self._lock:
            self._chunks[status] += int(count)

    def embedding(self, status: str = "ok", count: int = 1) -> None:
        """Count vectors accepted or rejected by validation."""

        if count <= 0:
            return
        with self._lock:
            self._embeddings[status] += int(count)

    def observe_search(self, seconds: float) -> None:
        """Record one hybrid-search duration (seconds)."""

        value = max(float(seconds), 0.0)
        if not math.isfinite(value):
            return
        with self._lock:
            self._search_count += 1
            self._search_sum += value
            for position, bound in enumerate(SEARCH_BUCKETS):
                if value <= bound:
                    self._buckets[position] += 1
            self._buckets[-1] += 1

    def snapshot(self) -> dict[str, Any]:
        """Return plain counters, used by tests and ``vector stats``."""

        with self._lock:
            return {
                "missing_chunk": dict(self._missing),
                "chunks": dict(self._chunks),
                "embeddings": dict(self._embeddings),
                "search_count": self._search_count,
                "search_seconds_total": round(self._search_sum, 6),
            }

    def render(self) -> str:
        """Render the Prometheus text exposition (version 0.0.4).

        Every family is rendered on every scrape: the label values this layer
        mints are emitted at 0 before they first occur, so a cold process
        exposes the whole documented set instead of only the parts that moved.
        """

        with self._lock:
            missing = sorted(self._missing.items())
            chunks = sorted(self._chunks.items())
            embeddings = sorted(self._embeddings.items())
            count = self._search_count
            seconds = self._search_sum
            buckets = list(self._buckets)

        lines = [
            "# HELP fc_missing_chunk_total Chunks lost to slicing or embedding failures.",
            "# TYPE fc_missing_chunk_total counter",
        ]
        for reason, value in _with_baseline(MISSING_CHUNK_REASONS, missing):
            lines.append(f'fc_missing_chunk_total{{reason="{reason}"}} {value}')

        lines.extend(
            [
                "# HELP fc_vector_chunks_total Announcement chunk rows written or deleted.",
                "# TYPE fc_vector_chunks_total counter",
            ]
        )
        for status, value in _with_baseline(CHUNK_STATUSES, chunks):
            lines.append(f'fc_vector_chunks_total{{status="{status}"}} {value}')

        lines.extend(
            [
                "# HELP fc_vector_embeddings_total Embedding vectors accepted or rejected.",
                "# TYPE fc_vector_embeddings_total counter",
            ]
        )
        for status, value in _with_baseline(EMBEDDING_STATUSES, embeddings):
            lines.append(f'fc_vector_embeddings_total{{status="{status}"}} {value}')

        lines.extend(
            [
                "# HELP fc_vector_search_seconds Hybrid search latency in seconds.",
                "# TYPE fc_vector_search_seconds histogram",
            ]
        )
        for position, bound in enumerate(SEARCH_BUCKETS):
            lines.append(
                f'fc_vector_search_seconds_bucket{{le="{bound}"}} {buckets[position]}'
            )
        lines.append(f'fc_vector_search_seconds_bucket{{le="+Inf"}} {buckets[-1]}')
        lines.append(f"fc_vector_search_seconds_sum {_format_seconds(seconds)}")
        lines.append(f"fc_vector_search_seconds_count {count}")
        return "\n".join(lines) + "\n"


VECTOR_METRICS = VectorMetrics()

__all__ = ["SEARCH_BUCKETS", "VECTOR_METRICS", "VectorMetrics"]
