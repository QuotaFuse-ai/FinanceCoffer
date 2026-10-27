"""ETL consumer: at-least-once ingestion from a Redis Stream.

The package owns everything between ``fc:stockdata:events`` and the warehouse:

* :mod:`financecoffer.etl.consumer` -- the loop: group setup, own-pending
  recovery, stale-entry reclaim, batched reads and graceful shutdown;
* :mod:`financecoffer.etl.processor` -- one event, one transaction:
  validate -> register -> ODS -> DWD;
* :mod:`financecoffer.etl.dlq` -- dead-letter persistence and replay;
* :mod:`financecoffer.etl.control` -- the durable pause switch;
* :mod:`financecoffer.etl.status` -- lag, pending and DLQ numbers;
* :mod:`financecoffer.etl.metrics` -- named counters and gauges.

``redis`` is an optional extra, so the package never imports it at module load
time: only :func:`financecoffer.etl.client.create_client` does.
"""

from __future__ import annotations

from financecoffer.etl.client import close_client, create_client
from financecoffer.etl.consumer import Consumer, ConsumeReport
from financecoffer.etl.control import control_state, is_paused, pause, resume
from financecoffer.etl.dlq import DlqRecord, enqueue_failure, retry_dlq
from financecoffer.etl.processor import (
    ProcessResult,
    classify_failure,
    process_payload,
    record_heartbeat,
)
from financecoffer.etl.replay import replay_from_ods
from financecoffer.etl.status import collect_status

__all__ = [
    "ConsumeReport",
    "Consumer",
    "DlqRecord",
    "ProcessResult",
    "classify_failure",
    "close_client",
    "collect_status",
    "control_state",
    "create_client",
    "enqueue_failure",
    "is_paused",
    "pause",
    "process_payload",
    "record_heartbeat",
    "replay_from_ods",
    "resume",
    "retry_dlq",
]
