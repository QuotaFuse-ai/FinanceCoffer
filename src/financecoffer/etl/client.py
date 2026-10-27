"""Redis access helpers.

``redis`` is an optional extra (``financecoffer[etl]``), so it is imported
inside :func:`create_client`: importing the CLI without the extra must keep
working, and a missing package becomes a ``DEPENDENCY`` error instead of a
traceback.
"""

from __future__ import annotations

from typing import Any

from financecoffer.core.errors import DependencyError
from financecoffer.core.redaction import redact_text

_HINT = "install the etl extra (pip install 'financecoffer[etl]') and check FC_REDIS_URL"


def create_client(url: str) -> Any:
    """Return a connected, decode-responses Redis client or raise ``E_DEPENDENCY``."""

    try:
        import redis
        from redis.exceptions import RedisError
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install extras
        raise DependencyError(
            "the redis package is not installed",
            details={"hint": _HINT},
        ) from exc

    try:
        client: Any = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
    except RedisError as exc:
        raise DependencyError(
            "cannot reach Redis",
            details={"redis_url": redact_text(url), "hint": _HINT},
        ) from exc
    return client


def close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def is_busy_group(exc: BaseException) -> bool:
    """True for the ``BUSYGROUP`` reply that means "the group already exists"."""

    try:
        from redis.exceptions import ResponseError
    except ModuleNotFoundError:  # pragma: no cover - depends on install extras
        return False
    return isinstance(exc, ResponseError) and "BUSYGROUP" in str(exc)


def is_redis_error(exc: BaseException) -> bool:
    try:
        from redis.exceptions import RedisError
    except ModuleNotFoundError:  # pragma: no cover - depends on install extras
        return False
    return isinstance(exc, RedisError)


__all__ = ["close_client", "create_client", "is_busy_group", "is_redis_error"]
