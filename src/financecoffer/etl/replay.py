"""``etl replay --from-ods``: requeue stored ODS records for re-delivery.

Replay never rewrites ODS. It selects raw rows with the requested filters and
resets the matching ``ops.etl_event_log`` rows to ``received``; the raw payload
stays exactly where it was so ``ods show --raw`` keeps returning the bytes
upstream sent.

Rows are selected newest-first by the ODS index and then reversed, so the
manifest and the requeue order follow upstream ``fetch_time`` order.
"""

from __future__ import annotations

from typing import Any

from financecoffer.core.errors import InvalidArgumentError
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.repositories.ods import RawDataRepository
from financecoffer.db.repositories.ops import EventLogRepository
from financecoffer.db.session import Database
from financecoffer.ods.service import MAX_REPLAY, normalize_data_type, parse_instant


def replay_from_ods(
    *,
    database: Database,
    data_type: str | None = None,
    symbol: str | None = None,
    since: str | None = None,
    until: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Re-deliver ODS rows matching the filters; ``dry_run`` writes nothing."""

    normalized_type = normalize_data_type(data_type)
    start = parse_instant(since, field_name="since")
    end = parse_instant(until, field_name="until", end_of_day=True)
    if start is not None and end is not None and start >= end:
        raise InvalidArgumentError(
            "--since must be earlier than --until",
            details={"field": "since"},
        )
    maximum = MAX_REPLAY if limit is None else max(1, min(int(limit), MAX_REPLAY))

    with database.session_scope() as session:
        rows = list(
            RawDataRepository(session).search(
                since=start,
                until=end,
                data_type=normalized_type,
                symbol=symbol,
                limit=maximum,
            )
        )
        rows.reverse()
        manifest = [_manifest(row) for row in rows]
        truncated = len(rows) == maximum
        if dry_run:
            return {
                "dry_run": True,
                "count": len(manifest),
                "truncated": truncated,
                "records": manifest,
            }

        event_log = EventLogRepository(session)
        requeued = 0
        unmatched: list[str] = []
        for row in rows:
            if event_log.requeue(row.event_id) is not None:
                requeued += 1
            else:
                unmatched.append(row.event_id)

    return {
        "dry_run": False,
        "count": len(manifest),
        "truncated": truncated,
        "requeued": requeued,
        "unmatched": unmatched,
        "records": manifest,
    }


def _manifest(row: StockRawData) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "data_type": row.data_type,
        "symbol": row.symbol,
        "source": row.source,
        "fetch_time": row.fetch_time.isoformat() if row.fetch_time else None,
        "etl_status": row.etl_status,
    }


__all__ = ["replay_from_ods"]
