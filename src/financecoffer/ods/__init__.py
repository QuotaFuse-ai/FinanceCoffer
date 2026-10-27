"""ODS raw layer: append-only landing plus read/replay operations.

``landing`` is the write path shared by ``fcoffer ingest event``; ``service`` is
the read path behind ``fcoffer ods list|show|stats|replay``. Replay re-delivers
through ``ops.etl_event_log`` and never rewrites an ODS row.
"""

from __future__ import annotations

from financecoffer.ods.landing import (
    DEFAULT_SOURCE,
    INITIAL_ETL_STATUS,
    ODS_SOURCES,
    LandingResult,
    ensure_partition,
    fetch_time_for,
    land_event,
    partition_name_for,
    resolve_source,
)
from financecoffer.ods.service import (
    DEFAULT_LIMIT,
    ETL_STATUSES,
    GROUP_BYS,
    MAX_LIMIT,
    MAX_REPLAY,
    OdsService,
    normalize_data_type,
    normalize_etl_status,
    normalize_group_by,
    parse_instant,
)

__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_SOURCE",
    "ETL_STATUSES",
    "GROUP_BYS",
    "INITIAL_ETL_STATUS",
    "MAX_LIMIT",
    "MAX_REPLAY",
    "ODS_SOURCES",
    "LandingResult",
    "OdsService",
    "ensure_partition",
    "fetch_time_for",
    "land_event",
    "normalize_data_type",
    "normalize_etl_status",
    "normalize_group_by",
    "parse_instant",
    "partition_name_for",
    "resolve_source",
]
