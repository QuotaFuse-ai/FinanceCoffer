"""Prometheus families that ``/metrics`` samples from the ``ops`` schema.

Every family below is *registered*: it is always rendered, with a zero sample
for each label value the DDL allows, and only the observed counts are overlaid
on top. A family that disappears when nothing has happened yet is worse than a
zero: ``absent()`` alerts and ``rate()`` panels go blank exactly when the
service starts, and a dashboard cannot tell "no failures" from "no exporter".

The baseline label values are the closed sets the database enforces
(``0002_ops.sql``) plus the fixed failure classifications of
``etl/processor.py:classify_failure``. ``fc_dirty_data_total{reason}`` is the
one open-ended label: the baseline names the reasons the classifier mints on
its own, and any other ``reason`` a validation failure carries is appended
with its real count when it occurs.

The numbers themselves stay a read-only sample of the warehouse; when the
sample fails the families still render, at zero, and the caller logs it.
"""

from __future__ import annotations

from collections.abc import Mapping

from financecoffer.query.service import BusinessMetrics

#: ``ck_etl_event_log_status`` in 0002_ops.sql.
ETL_EVENT_STATUSES: tuple[str, ...] = (
    "received",
    "processing",
    "done",
    "failed",
    "dlq",
    "duplicate",
)

#: The reasons ``classify_failure`` returns without a caller-supplied hint.
DIRTY_REASONS: tuple[str, ...] = (
    "validation",
    "unsupported",
    "storage",
    "dependency",
    "internal",
)

#: ``ck_archive_job_status`` in 0002_ops.sql.
ARCHIVE_JOB_STATUSES: tuple[str, ...] = ("pending", "running", "done", "failed")


def _label(value: str) -> str:
    """Escape a free-text label value per the Prometheus text format."""

    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _merge(baseline: tuple[str, ...], observed: Mapping[str, int] | None) -> dict[str, int]:
    """Return ``baseline`` at zero, overlaid with the observed counts."""

    merged = dict.fromkeys(baseline, 0)
    for key, count in (observed or {}).items():
        merged[str(key)] = int(count)
    return merged


def _counter(
    lines: list[str], name: str, help_text: str, label: str, samples: dict[str, int]
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} counter")
    for value, count in sorted(samples.items()):
        lines.append(f'{name}{{{label}="{_label(value)}"}} {count}')


def render_ops_metrics(business: BusinessMetrics | None) -> list[str]:
    """Render the ``ops``-derived families; ``None`` renders the zero baseline."""

    events = business.etl_events if business is not None else None
    dirty = business.dirty_data if business is not None else None
    jobs = business.archive_jobs if business is not None else None
    lag = business.consume_lag_seconds if business is not None else None

    lines: list[str] = []
    _counter(
        lines,
        "fc_etl_events_total",
        "ETL events recorded per status.",
        "status",
        _merge(ETL_EVENT_STATUSES, events),
    )
    _counter(
        lines,
        "fc_dirty_data_total",
        "Dead-lettered events per reason.",
        "reason",
        _merge(DIRTY_REASONS, dirty),
    )
    _counter(
        lines,
        "fc_archive_job_total",
        "Archive jobs per status.",
        "status",
        _merge(ARCHIVE_JOB_STATUSES, jobs),
    )
    lines.append(
        "# HELP fc_etl_consume_lag_seconds Age of the oldest event still queued for ETL "
        "(0 when the queue is empty)."
    )
    lines.append("# TYPE fc_etl_consume_lag_seconds gauge")
    lines.append(f"fc_etl_consume_lag_seconds {float(lag or 0.0):.3f}")
    return lines


__all__ = [
    "ARCHIVE_JOB_STATUSES",
    "DIRTY_REASONS",
    "ETL_EVENT_STATUSES",
    "render_ops_metrics",
]
