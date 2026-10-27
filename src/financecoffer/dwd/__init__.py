"""DWD write path: turn one validated event into an append-only fact row.

``write_event`` dispatches on ``data_type`` and must run inside the caller's
transaction (the ``ingest event`` session), so ODS, event log and DWD commit or
roll back together.
"""

from __future__ import annotations

from financecoffer.dwd.writer import WriteOutcome, write_event

__all__ = ["WriteOutcome", "write_event"]
