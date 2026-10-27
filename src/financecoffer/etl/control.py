"""Durable pause/resume switch for the consumer.

A Redis hash (``fc:etl:control`` by default) is the control plane because it is
already the consumer's dependency, survives a process restart and is visible to
every instance without a database round trip. The hash keeps the reason and the
timestamp so an operator can see who paused the pipeline and when; the loop
only reads the ``paused`` field, at batch boundaries.

Pausing stops *picking up* messages. A message already being processed is
finished and acknowledged, so pausing never leaves a half-written event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

PAUSED_FIELD = "paused"
_TRUE = frozenset({"1", "true", "yes", "on"})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _flag(raw: Any) -> bool:
    return str(raw).strip().lower() in _TRUE


def control_state(client: Any, key: str) -> dict[str, Any]:
    """Return the current switch plus its last transition metadata."""

    raw = client.hgetall(key) or {}
    return {
        "paused": _flag(raw.get(PAUSED_FIELD, "0")),
        "changed_at": raw.get("changed_at"),
        "reason": raw.get("reason"),
    }


def is_paused(client: Any, key: str) -> bool:
    return _flag(client.hget(key, PAUSED_FIELD) or "0")


def pause(client: Any, key: str, *, reason: str | None = None) -> dict[str, Any]:
    client.hset(
        key,
        mapping={
            PAUSED_FIELD: "1",
            "changed_at": _now(),
            "reason": reason or "operator pause",
        },
    )
    return control_state(client, key)


def resume(client: Any, key: str) -> dict[str, Any]:
    client.hset(
        key,
        mapping={
            PAUSED_FIELD: "0",
            "changed_at": _now(),
            "reason": "operator resume",
        },
    )
    return control_state(client, key)


__all__ = ["PAUSED_FIELD", "control_state", "is_paused", "pause", "resume"]
