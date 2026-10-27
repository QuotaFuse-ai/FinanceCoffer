"""Append-only landing of validated events into ``ods.stock_raw_data``.

The raw layer is the immutable first stop of the write path (DEC-0012):

* this package only inserts -- no code path here issues ``UPDATE`` or ``DELETE``
  against ODS, and no unique index is added (a partitioned table cannot carry
  one; ``ops.etl_event_log`` is the idempotency gate);
* a landed row carries the parsed payload in ``raw_json`` *and* the exact
  upstream bytes in ``raw_text``, so ``fcoffer ods show <id> --raw`` can return
  the original document unchanged;
* ``etl_status`` starts at ``pending``;
* landing into a month without a partition fails loudly with the command that
  creates it -- there is no DEFAULT partition to fall into.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from financecoffer.core.compliance import columns as compliance_columns
from financecoffer.core.errors import InvalidArgumentError, StorageError
from financecoffer.db.repositories.ods import RawDataRepository
from financecoffer.events.validation import ValidatedEvent

ODS_SOURCES: tuple[str, ...] = ("tickhub-cn", "tickhub-us", "backfill", "manual")
DEFAULT_SOURCE = "manual"

#: Hubs that publish into FinanceCoffer. ``source_info.hub`` names one of these.
ODS_HUBS: tuple[str, ...] = ("tickhub-cn", "tickhub-us")

#: Fallback for events published before ``source_info.hub`` existed: the hub
#: that collects each vendor. ``source_info.provider`` is the *vendor*
#: (``cninfo``, ``tencent``, ...), never the hub, so reading it as the ODS
#: source would label every real event ``manual``.
_HUB_BY_VENDOR: dict[str, str] = {
    # TickHub-CN vendors
    "eastmoney": "tickhub-cn",
    "eastmoney_datacenter": "tickhub-cn",
    "cninfo": "tickhub-cn",
    "sse": "tickhub-cn",
    "szse": "tickhub-cn",
    "tencent": "tickhub-cn",
    "sina": "tickhub-cn",
    "xueqiu": "tickhub-cn",
    "ths": "tickhub-cn",
    # TickHub-US vendors
    "sec_edgar": "tickhub-us",
    "finnhub": "tickhub-us",
    "polygon": "tickhub-us",
    "yfinance": "tickhub-us",
}

INITIAL_ETL_STATUS = "pending"


@dataclass(frozen=True)
class LandingResult:
    """The row appended by one landing, as reported to ``ingest event``."""

    record_id: int
    event_id: str
    source: str
    fetch_time: datetime
    partition: str
    etl_status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.record_id,
            "event_id": self.event_id,
            "source": self.source,
            "fetch_time": _rfc3339(self.fetch_time),
            "partition": self.partition,
            "etl_status": self.etl_status,
        }


def fetch_time_for(event: ValidatedEvent) -> datetime:
    """``fetch_time`` is when upstream fetched the event, in UTC.

    ``source_info.crawl_time`` is optional in the contract, so a publisher that
    omits it falls back to ``event_time``; both are already tz-aware or are read
    as UTC when naive.
    """

    value = event.source_info.crawl_time or event.event_time
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def resolve_source(event: ValidatedEvent, *, source: str | None = None) -> str:
    """Pick the ODS ``source`` label: override, then the publishing hub, then ``manual``.

    The hub comes from ``source_info.hub`` when the publisher sends it; events
    published before that field existed fall back to the vendor -> hub map, so
    a ``cninfo`` announcement still lands as ``tickhub-cn`` instead of
    ``manual``. Only a payload that matches no known hub or vendor -- a hand
    written file, a test fixture -- is labelled ``manual``.
    """

    if source is not None:
        normalized = source.strip().lower()
        if normalized not in ODS_SOURCES:
            raise InvalidArgumentError(
                "source must be one of: " + ", ".join(ODS_SOURCES),
                details={"field": "source", "value": source},
            )
        return normalized
    hub = (event.source_info.hub or "").strip().lower()
    if hub in ODS_HUBS:
        return hub
    provider = event.provider.strip().lower()
    # Heartbeats and some hub-native payloads name the hub itself as the
    # provider; that is a hub label, not a vendor.
    if provider in ODS_HUBS:
        return provider
    return _HUB_BY_VENDOR.get(provider, DEFAULT_SOURCE)


def partition_name_for(when: datetime) -> tuple[str, str]:
    """Return ``(qualified partition name, YYYY-MM)`` for ``when`` in UTC."""

    utc = when.astimezone(UTC)
    return f"ods.stock_raw_data_{utc:%Y%m}", f"{utc:%Y-%m}"


def ensure_partition(session: Session, when: datetime) -> str:
    """Return the partition covering ``when`` or raise a storage error.

    The check runs inside the landing transaction, so a missing partition rolls
    the whole ingest back: neither the event-log claim nor a partial ODS row
    survives. The error names the exact command that creates the month.
    """

    partition, month = partition_name_for(when)
    exists = session.execute(
        text("SELECT to_regclass(:name) IS NOT NULL"), {"name": partition}
    ).scalar()
    if not exists:
        raise StorageError(
            "ODS partition is missing for the event's fetch_time month",
            details={
                "partition": partition,
                "month": month,
                "hint": f"fcoffer db partitions create --month {month}",
            },
        )
    return partition


def land_event(
    session: Session,
    event: ValidatedEvent,
    *,
    raw_text: str,
    source: str | None = None,
) -> LandingResult:
    """Append ``event`` to ODS; the caller owns the transaction boundary."""

    if not raw_text:
        raise InvalidArgumentError(
            "raw_text must carry the upstream payload",
            details={"field": "raw_text"},
        )
    # ``symbol`` is nullable here on purpose: validation already rejects a
    # missing symbol for every stock-level type, and a market-level payload
    # (``northbound``) reaches ODS with ``symbol=null`` rather than a fake one.
    fetch_time = fetch_time_for(event)
    resolved_source = resolve_source(event, source=source)
    partition = ensure_partition(session, fetch_time)

    row = RawDataRepository(session).append(
        event_id=event.event_id,
        data_type=event.data_type.value,
        symbol=event.symbol,
        market=event.market.value,
        source=resolved_source,
        biz_key=event.biz_key,
        fetch_time=fetch_time,
        raw_json=dict(event.raw),
        raw_text=raw_text,
        etl_status=INITIAL_ETL_STATUS,
        **compliance_columns(event.compliance),
    )
    return LandingResult(
        record_id=row.id,
        event_id=row.event_id,
        source=row.source,
        fetch_time=row.fetch_time,
        partition=partition,
        etl_status=row.etl_status,
    )


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
