"""In-process token-bucket rate limiting for the REST surface (S-07-2).

One bucket per client IP, stored in a plain dict guarded by a lock so the ASGI
threadpool can share it safely. Standard library only: no new runtime
dependencies.

The bucket map is bounded: stale keys are pruned once it grows past
``max_keys``, so a flood of spoofed source addresses cannot grow it without
limit.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

MAX_TRACKED_CLIENTS = 10_000
_IDLE_SECONDS = 300.0


class TokenBucket:
    """Per-key token bucket with lazy refill.

    ``rate_per_minute`` is the sustained rate; ``burst`` (default: the same
    number) is the bucket capacity, so a client may spend a full minute's
    budget at once and then refill steadily.
    """

    def __init__(
        self,
        *,
        rate_per_minute: int,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        if burst is not None and burst <= 0:
            raise ValueError("burst must be positive")
        self.rate_per_minute = int(rate_per_minute)
        self.capacity = float(burst if burst is not None else rate_per_minute)
        self._refill_per_second = self.rate_per_minute / 60.0
        self._clock = clock
        self._max_keys = max(1, int(max_keys))
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Consume one token for ``key``; return ``False`` when it is empty."""

        now = self._clock()
        with self._lock:
            self._prune(now)
            tokens = self._refilled(key, now)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key`` may retry; always at least one."""

        now = self._clock()
        with self._lock:
            tokens = self._refilled(key, now)
        missing = max(0.0, 1.0 - tokens)
        return max(1, math.ceil(missing / self._refill_per_second))

    def _refilled(self, key: str, now: float) -> float:
        tokens, last = self._buckets.get(key, (self.capacity, now))
        if now > last:
            tokens = min(self.capacity, tokens + (now - last) * self._refill_per_second)
        return tokens

    def _prune(self, now: float) -> None:
        if len(self._buckets) < self._max_keys:
            return
        for key in [key for key, (_, last) in self._buckets.items() if now - last > _IDLE_SECONDS]:
            del self._buckets[key]
        if len(self._buckets) >= self._max_keys:
            oldest = min(self._buckets, key=lambda key: self._buckets[key][1])
            del self._buckets[oldest]


__all__ = ["MAX_TRACKED_CLIENTS", "TokenBucket"]
