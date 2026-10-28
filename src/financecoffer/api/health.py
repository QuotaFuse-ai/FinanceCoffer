"""Dependency probes rendered by ``GET /health``.

Every probe here is bounded and never raises: a dependency that is down must
not take the readiness endpoint down with it, because orchestration uses that
endpoint to decide whether to route traffic to this process. Each probe
reports its own ``status`` (and, for calls that block on a socket, how long it
took), so the aggregate ``status`` rule can be revisited without touching the
probe itself.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

from financecoffer.core.config import Config

#: Budget for one socket operation (connect, then read). A ``PING`` performs at
#: most both, so a black-holed Redis is abandoned inside ~1 second.
REDIS_OP_TIMEOUT = 0.5


def redis_check(config: Config, *, timeout: float = REDIS_OP_TIMEOUT) -> dict[str, Any]:
    """``PING`` Redis once over ``config.redis_url`` and report the outcome.

    ``status`` is ``ok`` when the server answered, ``unreachable`` on any
    transport or protocol error, and ``not_installed`` when the optional
    ``redis`` client (``financecoffer[etl]``) is not available.
    """

    started = time.perf_counter()
    status = "ok"
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:  # pragma: no cover - depends on install extras
        status = "not_installed"
    else:
        client: Any = None
        try:
            client = redis.Redis.from_url(
                config.redis_url,
                socket_connect_timeout=timeout,
                socket_timeout=timeout,
                # redis-py retries ten times by default; a readiness probe must
                # issue exactly one PING to keep its own deadline.
                retry=Retry(NoBackoff(), 0),
            )
            client.ping()
        except Exception:  # noqa: BLE001 - redis-py raises several transport errors
            status = "unreachable"
        finally:
            if client is not None:
                with suppress(Exception):
                    client.close()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return {"status": status, "elapsed_ms": elapsed_ms}


def etl_check(service: Any) -> dict[str, Any]:
    """Report the ETL queue depth and lag: ``{status, lag_seconds, pending}``.

    The numbers come from :meth:`QueryService.etl_queue`, which shares
    :func:`financecoffer.query.service.etl_queue_state` with
    ``fc_etl_consume_lag_seconds`` on ``/metrics`` -- one definition of
    consumption lag for both surfaces.

    ``status`` is ``unknown`` on purpose: which lag counts as degraded is part
    of the readiness table still being decided, so this probe reports facts and
    leaves the verdict alone. Both numbers are ``None`` when the queue could not
    be sampled, because that is not a measurement either.
    """

    try:
        snapshot = service.etl_queue()
    except Exception:  # noqa: BLE001 - a probe must never crash /health
        snapshot = None
    if snapshot is None:
        return {"status": "unknown", "lag_seconds": None, "pending": None}
    pending, lag = snapshot
    return {
        "status": "unknown",
        "lag_seconds": None if lag is None else round(float(lag), 3),
        "pending": int(pending),
    }
