"""DWD write path and read/replay operations.

``write_event`` dispatches on ``data_type`` and must run inside the caller's
transaction (the ``ingest event`` session), so ODS, event log and DWD commit or
roll back together. ``DwdService`` backs ``dwd show|versions|backfill|verify``.
"""

from __future__ import annotations

from financecoffer.dwd.service import (
    BACKFILL_DATA_TYPES,
    MAX_BACKFILL,
    MAX_VERIFY,
    DwdService,
    normalize_report_type,
    parse_report_date,
)
from financecoffer.dwd.writer import WriteOutcome, write_event

__all__ = [
    "BACKFILL_DATA_TYPES",
    "MAX_BACKFILL",
    "MAX_VERIFY",
    "DwdService",
    "WriteOutcome",
    "normalize_report_type",
    "parse_report_date",
    "write_event",
]
