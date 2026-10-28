"""Operational snapshot of the consumer: lag, pending, DLQ, latest error.

Everything here is read-only and comes from the systems that own the truth:
pending counts and lag from Redis (``XPENDING`` / ``XINFO GROUPS``), dead-letter
depth and the most recent error from PostgreSQL. That is why the numbers agree
with a manual ``XPENDING`` -- :func:`financecoffer.etl.status.collect_status`
does not maintain a parallel counter.
"""

from __future__ import annotations

import logging
from typing import Any

from financecoffer.core.config import Config
from financecoffer.db.repositories.ops import DlqRepository
from financecoffer.db.session import Database
from financecoffer.etl.control import control_state
from financecoffer.etl.metrics import set_dlq_depth

_log = logging.getLogger(__name__)


def collect_status(
    *,
    client: Any,
    database: Database,
    config: Config,
    consumer_name: str | None = None,
) -> dict[str, Any]:
    """Build the ``etl status`` payload.

    The ``except Exception`` blocks below degrade a broken Redis or PostgreSQL
    into an ``errors`` entry so a status report still renders. They are reached
    through this service call (see ``tests/unit/test_etl.py::
    test_status_survives_redis_and_database_outages``); the ``etl status`` CLI
    command fails fast before this point because ``create_client()`` pings
    Redis and raises ``E_DEPENDENCY`` when it is down.
    """

    stream = config.event_stream
    group = config.event_group
    snapshot: dict[str, Any] = {
        "stream": stream,
        "group": group,
        "consumer": consumer_name,
        "paused": False,
        "control": None,
        "lag": None,
        "pending": 0,
        "pending_by_consumer": [],
        "consumers": [],
        "last_delivered_id": None,
        "dlq": {"stream": config.etl_dlq_stream, "stream_depth": 0, "database_pending": 0},
        "last_error": None,
        "errors": [],
    }

    try:
        snapshot["control"] = control_state(client, config.etl_control_key)
        snapshot["paused"] = bool(snapshot["control"].get("paused"))
        stream_length = int(client.xlen(stream) or 0)
        group_info = _group_info(client, stream, group)
        if group_info is None:
            snapshot["stream_length"] = stream_length
            snapshot["errors"].append(
                {
                    "area": "redis",
                    "reason": "group_missing",
                    "message": f"consumer group {group!r} does not exist yet",
                }
            )
        else:
            snapshot["stream_length"] = stream_length
            snapshot["last_delivered_id"] = group_info.get("last-delivered-id")
            snapshot["lag"] = _lag(group_info, stream_length)
            snapshot["consumers"] = _consumers(client, stream, group)
            summary = client.xpending(stream, group)
            snapshot["pending"] = int((summary or {}).get("pending", 0) or 0)
            snapshot["pending_by_consumer"] = [
                {"name": item.get("name"), "pending": int(item.get("pending", 0) or 0)}
                for item in (summary or {}).get("consumers", []) or []
            ]
        snapshot["dlq"]["stream_depth"] = int(client.xlen(config.etl_dlq_stream) or 0)
    except Exception as exc:  # noqa: BLE001 - status must render even when Redis is down
        _log.warning("could not read consumer state from Redis: %s", exc)
        snapshot["errors"].append(
            {"area": "redis", "reason": "unavailable", "message": "could not read Redis"}
        )

    try:
        with database.session_scope() as session:
            repository = DlqRepository(session)
            snapshot["dlq"]["database_pending"] = repository.depth()
            set_dlq_depth(snapshot["dlq"]["database_pending"])
            latest = repository.latest(limit=1)
            if latest:
                row = latest[0]
                snapshot["last_error"] = {
                    "id": row.id,
                    "event_id": row.event_id,
                    "reason": row.reason,
                    "error": row.error_detail,
                    "status": row.status,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
    except Exception as exc:  # noqa: BLE001 - status must render even when the database is down
        _log.warning("could not read DLQ state from PostgreSQL: %s", exc)
        snapshot["errors"].append(
            {"area": "database", "reason": "unavailable", "message": "could not read PostgreSQL"}
        )

    snapshot["healthy"] = not snapshot["errors"]
    return snapshot


def _group_info(client: Any, stream: str, group: str) -> dict[str, Any] | None:
    try:
        groups = client.xinfo_groups(stream)
    except Exception:
        return None
    for item in groups or []:
        if item.get("name") == group:
            return dict(item)
    return None


def _lag(group_info: dict[str, Any], stream_length: int) -> int | None:
    lag = group_info.get("lag")
    if lag is not None:
        try:
            return max(0, int(lag))
        except (TypeError, ValueError):
            pass
    entries_read = group_info.get("entries-read")
    if entries_read is None:
        return None
    try:
        return max(0, stream_length - int(entries_read))
    except (TypeError, ValueError):
        return None


def _consumers(client: Any, stream: str, group: str) -> list[dict[str, Any]]:
    try:
        rows = client.xinfo_consumers(stream, group)
    except Exception:
        return []
    return [
        {
            "name": item.get("name"),
            "pending": int(item.get("pending", 0) or 0),
            "idle_ms": int(item.get("idle", 0) or 0),
        }
        for item in rows or []
    ]


__all__ = ["collect_status"]
