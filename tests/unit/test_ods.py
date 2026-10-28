"""ODS landing and replay (T-005).

The write path is checked against a fake session that records every statement it
was handed: landing may only ``INSERT``, ``replay --dry-run`` must issue no DML
at all, and a real replay may only update ``ops.etl_event_log`` -- never the ODS
row. ``raw_json`` / ``raw_text`` are compared with the upstream payload so a
landing can never rewrite what the publisher sent.
"""

from __future__ import annotations

import ast
import json
import re
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Delete, Insert, Update
from sqlalchemy.sql.elements import TextClause
from typer.testing import CliRunner

from financecoffer import events
from financecoffer.cli import app
from financecoffer.core.config import Config
from financecoffer.core.errors import (
    ConfigError,
    InvalidArgumentError,
    NotFoundError,
    StorageError,
)
from financecoffer.db.models.ods import StockRawData
from financecoffer.events import ValidatedEvent
from financecoffer.ods import landing, service
from financecoffer.ods.landing import (
    fetch_time_for,
    land_event,
    partition_name_for,
    resolve_source,
)
from financecoffer.ods.service import (
    ETL_STATUSES,
    GROUP_BYS,
    MAX_LIMIT,
    OdsService,
    normalize_data_type,
    normalize_etl_status,
    normalize_group_by,
    parse_instant,
)

PROVIDER = "eastmoney"
SYMBOL = "600000"
BIZ_KEY = "2026Q3-600000"
CRAWL_TIME = "2026-10-27T02:00:00Z"

runner = CliRunner()


# -- fakes -------------------------------------------------------------------


class FakeRows:
    """The slice of ``Result`` the repositories touch."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = list(rows or [])

    def all(self) -> list[Any]:
        return list(self._rows)

    def one(self) -> Any:
        return self._rows[0]

    def one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def scalar(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)


class FakeSession:
    """Records statements and replays canned rows; no database involved."""

    def __init__(self, *, partition_exists: bool = True) -> None:
        self.partition_exists = partition_exists
        self.statements: list[Any] = []
        self.dml: list[Any] = []
        self.scalars_queue: deque[list[Any]] = deque()
        self.execute_queue: deque[list[Any]] = deque()
        self.added: list[Any] = []
        self.flush_count = 0

    def respond_scalars(self, *rows: Any) -> None:
        self.scalars_queue.append(list(rows))

    def respond_execute(self, *rows: Any) -> None:
        self.execute_queue.append(list(rows))

    def add(self, row: Any) -> None:
        self.added.append(row)

    def add_all(self, rows: Any) -> None:
        self.added.extend(rows)

    def flush(self) -> None:
        self.flush_count += 1
        for position, row in enumerate(self.added, start=1):
            if getattr(row, "id", None) is None:
                row.id = position

    def scalars(self, statement: Any) -> FakeRows:
        self.statements.append(statement)
        if isinstance(statement, (Insert, Update, Delete)):
            self.dml.append(statement)
        return FakeRows(self.scalars_queue.popleft() if self.scalars_queue else [])

    def execute(self, statement: Any, params: Any = None) -> FakeRows:
        self.statements.append(statement)
        if isinstance(statement, TextClause):
            return FakeRows([self.partition_exists])
        return FakeRows(self.execute_queue.popleft() if self.execute_queue else [])


class FakeDatabase:
    def __init__(self, session: FakeSession) -> None:
        self._session = session
        self.disposed = False

    @contextmanager
    def session_scope(self) -> Iterator[FakeSession]:
        yield self._session

    def dispose(self) -> None:
        self.disposed = True


def make_service(
    session: FakeSession, *, compliance_enabled: bool = False
) -> tuple[OdsService, FakeDatabase]:
    database = FakeDatabase(session)
    instance = OdsService(
        config=Config(
            db_url="postgresql://fcoffer:fcoffer@127.0.0.1:5432/financecoffer",
            compliance_enabled=compliance_enabled,
        ),
        database_factory=lambda _dsn: database,
    )
    return instance, database


# -- fixtures ----------------------------------------------------------------


def payload(
    *,
    provider: str = PROVIDER,
    data_type: str = "financial",
    symbol: str = "600000",
    biz_key: str = BIZ_KEY,
    event_time: str = "2026-10-27T10:00:00+08:00",
    crawl_time: str | None = CRAWL_TIME,
    hub: str | None = None,
) -> dict[str, Any]:
    source_info: dict[str, Any] = {
        "provider": provider,
        "crawl_url": "https://example.com/ann/1",
    }
    if crawl_time is not None:
        source_info["crawl_time"] = crawl_time
    if hub is not None:
        source_info["hub"] = hub
    return {
        "schema_version": "1.0",
        "event_id": events.compute_event_id(
            provider=provider, data_type=data_type, symbol=symbol, biz_key=biz_key
        ),
        "biz_key": biz_key,
        "event_time": event_time,
        "data_type": data_type,
        "symbol": symbol,
        "market": "CN",
        "source_info": source_info,
        "content": {
            "quality_metrics": {"quality_status": "normal", "confidence": "high"},
            "structured_fields": {
                "report_date": "2026-09-30",
                "report_type": "Q3",
                "amount_revenue": "1234567890123.4567",
                "amount_net_profit": "-5.5000",
            },
        },
        "trace_id": "trace-t005",
    }


def validated(**overrides: Any) -> ValidatedEvent:
    return events.validate_event(payload(**overrides))


def raw_row(**overrides: Any) -> StockRawData:
    values: dict[str, Any] = {
        "id": 1,
        "event_id": "evt_raw_1",
        "data_type": "financial",
        "symbol": "600000",
        "market": "CN",
        "source": "manual",
        "biz_key": BIZ_KEY,
        "fetch_time": datetime(2026, 10, 27, 2, 0, tzinfo=UTC),
        "ingested_at": datetime(2026, 10, 27, 2, 0, 5, tzinfo=UTC),
        "etl_status": "pending",
        "raw_json": {"n": 1},
        "raw_text": '{"n": 1}',
    }
    values.update(overrides)
    return StockRawData(**values)


def mutations(session: FakeSession) -> list[Any]:
    return list(session.dml)


# -- landing -----------------------------------------------------------------


def test_fetch_time_prefers_crawl_time() -> None:
    event = validated()

    assert fetch_time_for(event) == datetime(2026, 10, 27, 2, 0, tzinfo=UTC)


def test_fetch_time_falls_back_to_event_time_in_utc() -> None:
    event = validated(crawl_time=None, event_time="2026-10-28T10:00:00+08:00")

    assert fetch_time_for(event) == datetime(2026, 10, 28, 2, 0, tzinfo=UTC)


def test_partition_name_uses_the_utc_month() -> None:
    shanghai = timezone(timedelta(hours=8))

    partition, month = partition_name_for(datetime(2026, 11, 1, 0, 30, tzinfo=shanghai))

    assert partition == "ods.stock_raw_data_202610"
    assert month == "2026-10"


def test_land_event_appends_a_pending_row_without_rewriting_the_payload() -> None:
    event = validated()
    original = json.loads(json.dumps(event.raw))
    original_text = '{\n  "event_id": "evt_x",\n  "amount": "1.0000"\n}'
    session = FakeSession()

    result = land_event(session, event, raw_text=original_text)

    assert result.record_id == 1
    assert result.event_id == event.event_id
    assert result.source == "tickhub-cn"
    assert result.partition == "ods.stock_raw_data_202610"
    assert result.etl_status == "pending"
    assert result.as_dict()["fetch_time"] == "2026-10-27T02:00:00Z"

    [row] = session.added
    assert row.event_id == event.event_id
    assert row.data_type == "financial"
    assert row.symbol == "600000"
    assert row.market == "CN"
    assert row.source == "tickhub-cn"
    assert row.biz_key == BIZ_KEY
    assert row.fetch_time == datetime(2026, 10, 27, 2, 0, tzinfo=UTC)
    assert row.etl_status == "pending"
    assert row.raw_text == original_text
    assert row.raw_json == original
    assert row.raw_json is not event.raw
    assert row.compliance_tier is None
    assert row.commercial_use is None

    # The upstream payload is never mutated and only the partition probe plus
    # the INSERT reach the session: ODS has no UPDATE/DELETE path.
    assert dict(event.raw) == original
    assert mutations(session) == []
    assert session.flush_count == 1
    assert all(isinstance(statement, TextClause) for statement in session.statements)


def test_land_event_fails_loudly_when_the_month_has_no_partition() -> None:
    session = FakeSession(partition_exists=False)

    with pytest.raises(StorageError) as caught:
        land_event(session, validated(), raw_text='{"n": 1}')

    assert caught.value.details is not None
    assert caught.value.details["partition"] == "ods.stock_raw_data_202610"
    assert caught.value.details["month"] == "2026-10"
    assert caught.value.details["hint"] == "fcoffer db partitions create --month 2026-10"
    assert session.added == []
    assert mutations(session) == []


def test_land_event_rejects_an_empty_payload() -> None:
    with pytest.raises(InvalidArgumentError) as caught:
        land_event(FakeSession(), validated(), raw_text="")

    assert caught.value.details == {"field": "raw_text"}


def test_land_event_keeps_a_market_level_payload_without_a_symbol() -> None:
    # ``northbound`` is market-wide, so the contract carries ``symbol=null``.
    # ODS stores exactly that -- no placeholder symbol is invented (rule 1).
    event = replace(validated(), symbol=None)
    session = FakeSession()

    result = land_event(session, event, raw_text='{"net_buy_amount": "1.5"}')

    assert result.record_id == 1
    [row] = session.added
    assert row.symbol is None
    assert row.event_id == event.event_id
    assert row.source == "tickhub-cn"


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        # ``provider`` is the upstream vendor, never the hub name.
        (PROVIDER, "tickhub-cn"),
        ("cninfo", "tickhub-cn"),
        ("tencent", "tickhub-cn"),
        ("sec_edgar", "tickhub-us"),
        ("finnhub", "tickhub-us"),
        # A payload that names the hub itself (heartbeats do) keeps that label.
        ("tickhub-cn", "tickhub-cn"),
        ("tickhub-us", "tickhub-us"),
        # Neither a known hub nor a known vendor: an operator/hand-written file.
        ("unknown_vendor", "manual"),
    ],
)
def test_resolve_source_maps_vendors_to_their_publishing_hub(
    provider: str, expected: str
) -> None:
    assert resolve_source(validated(provider=provider)) == expected


@pytest.mark.parametrize("hub", ["tickhub-cn", "tickhub-us"])
def test_resolve_source_prefers_the_hub_marker(hub: str) -> None:
    event = validated(provider="unknown_vendor", hub=hub)

    assert resolve_source(event) == hub


def test_resolve_source_ignores_an_unknown_hub_marker() -> None:
    event = validated(provider="cninfo", hub="some-other-hub")

    assert resolve_source(event) == "tickhub-cn"


def test_resolve_source_accepts_an_explicit_override() -> None:
    assert resolve_source(validated(), source=" backfill ") == "backfill"


def test_resolve_source_rejects_unknown_labels() -> None:
    with pytest.raises(InvalidArgumentError) as caught:
        resolve_source(validated(), source="vendor")

    assert caught.value.details is not None
    assert caught.value.details["field"] == "source"


# -- parsing and normalization ----------------------------------------------


def test_parse_instant_date_only_since_is_midnight_utc() -> None:
    assert parse_instant("2026-10-01", field_name="since") == datetime(
        2026, 10, 1, tzinfo=UTC
    )


def test_parse_instant_date_only_until_includes_the_whole_day() -> None:
    assert parse_instant("2026-10-31", field_name="until", end_of_day=True) == datetime(
        2026, 11, 1, tzinfo=UTC
    )


def test_parse_instant_rfc3339_is_converted_to_utc() -> None:
    assert parse_instant("2026-10-27T10:00:00+08:00", field_name="since") == datetime(
        2026, 10, 27, 2, 0, tzinfo=UTC
    )
    assert parse_instant("2026-10-27T02:00:00Z", field_name="since") == datetime(
        2026, 10, 27, 2, 0, tzinfo=UTC
    )


def test_parse_instant_rejects_a_missing_timezone() -> None:
    with pytest.raises(InvalidArgumentError) as caught:
        parse_instant("2026-10-27T02:00:00", field_name="since")

    assert caught.value.details is not None
    assert caught.value.details["field"] == "since"


@pytest.mark.parametrize("value", ["27/10/2026", "2026-13-01T00:00:00Z", "yesterday"])
def test_parse_instant_rejects_unparsable_values(value: str) -> None:
    with pytest.raises(InvalidArgumentError):
        parse_instant(value, field_name="until")


def test_normalize_data_type_accepts_contract_values() -> None:
    assert normalize_data_type(" ANNOUNCEMENT ") == "announcement"
    assert normalize_data_type(None) is None


def test_normalize_data_type_rejects_unknown_values() -> None:
    with pytest.raises(InvalidArgumentError):
        normalize_data_type("sentiment")


def test_normalize_etl_status_uses_the_ddl_values() -> None:
    assert ETL_STATUSES == ("pending", "done", "failed")
    assert normalize_etl_status("FAILED") == "failed"
    with pytest.raises(InvalidArgumentError):
        normalize_etl_status("processed")


def test_normalize_group_by_defaults_and_validates() -> None:
    assert GROUP_BYS == ("data_type", "source", "etl_status")
    assert normalize_group_by(None) == "data_type"
    assert normalize_group_by("SOURCE") == "source"
    with pytest.raises(InvalidArgumentError):
        normalize_group_by("symbol")


# -- service reads -----------------------------------------------------------


def test_service_requires_a_configured_database() -> None:
    def factory(_dsn: str) -> Any:
        raise AssertionError("must not open a connection without a DSN")

    instance = OdsService(config=Config(), database_factory=factory)

    with pytest.raises(ConfigError, match="database URL is not configured"):
        instance.listing()


def test_listing_bounds_the_limit_and_forwards_filters() -> None:
    session = FakeSession()
    session.respond_scalars(raw_row())
    instance, database = make_service(session)

    data = instance.listing(
        data_type="financial",
        symbol="600000",
        since="2026-10-01",
        until="2026-10-31",
        etl_status="pending",
        limit=10,
    )

    assert data["count"] == 1
    assert data["records"][0]["id"] == 1
    assert data["records"][0]["fetch_time"] == "2026-10-27T02:00:00Z"
    assert data["since"] == "2026-10-01T00:00:00Z"
    assert data["until"] == "2026-11-01T00:00:00Z"
    assert data["limit"] == 10
    assert database.disposed is True
    assert mutations(session) == []
    assert all(not isinstance(statement, TextClause) for statement in session.statements)


@pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1])
def test_listing_rejects_an_out_of_range_limit(limit: int) -> None:
    instance, _ = make_service(FakeSession())

    with pytest.raises(InvalidArgumentError) as caught:
        instance.listing(limit=limit)

    assert caught.value.details is not None
    assert caught.value.details["field"] == "limit"


def test_listing_rejects_an_empty_window() -> None:
    instance, _ = make_service(FakeSession())

    with pytest.raises(InvalidArgumentError):
        instance.listing(since="2026-10-31", until="2026-10-01")


def test_show_returns_the_detail_without_compliance_when_disabled() -> None:
    session = FakeSession()
    session.respond_scalars(
        raw_row(request_params={"q": 1}, compliance_tier="S", commercial_use=False)
    )
    instance, _ = make_service(session)

    data = instance.show(1)

    assert data["id"] == 1
    assert data["request_params"] == {"q": 1}
    assert data["raw_json"] == {"n": 1}
    assert data["has_raw_text"] is True
    assert "compliance" not in data


def test_show_includes_compliance_when_enabled() -> None:
    session = FakeSession()
    session.respond_scalars(raw_row(compliance_tier="S", commercial_use=False))
    instance, _ = make_service(session, compliance_enabled=True)

    data = instance.show(1)

    assert data["compliance"] == {"compliance_tier": "S", "commercial_use": False}


def test_show_reports_a_missing_record() -> None:
    instance, _ = make_service(FakeSession())

    with pytest.raises(NotFoundError) as caught:
        instance.show(7)

    assert caught.value.details is not None
    assert caught.value.details["id"] == 7


def test_raw_text_returns_the_stored_document_verbatim() -> None:
    original = '{"b": 1,\n  "a": 2}'
    session = FakeSession()
    session.respond_scalars(raw_row(raw_text=original))
    instance, _ = make_service(session)

    assert instance.raw_text(1) == original


def test_raw_text_falls_back_to_compact_json() -> None:
    session = FakeSession()
    session.respond_scalars(raw_row(raw_text=None, raw_json={"b": 1, "a": 2}))
    instance, _ = make_service(session)

    assert instance.raw_text(1) == '{"b":1,"a":2}'


def test_raw_text_reports_a_file_only_record() -> None:
    session = FakeSession()
    session.respond_scalars(
        raw_row(raw_text=None, raw_json=None, raw_file_path="raw/2026/10/a.json")
    )
    instance, _ = make_service(session)

    with pytest.raises(NotFoundError):
        instance.raw_text(1)


def test_stats_counts_group_values() -> None:
    session = FakeSession()
    session.respond_execute(("financial", 3), ("kline", 1))
    instance, _ = make_service(session)

    data = instance.stats(group_by="data_type")

    assert data == {
        "group_by": "data_type",
        "groups": [
            {"value": "financial", "count": 3},
            {"value": "kline", "count": 1},
        ],
        "total": 4,
    }


# -- replay ------------------------------------------------------------------


def test_replay_requires_exactly_one_selector() -> None:
    instance, _ = make_service(FakeSession())

    with pytest.raises(InvalidArgumentError):
        instance.replay()
    with pytest.raises(InvalidArgumentError):
        instance.replay(record_ids=(1,), all_failed=True)


def test_replay_dry_run_only_prints_the_manifest() -> None:
    session = FakeSession()
    session.respond_scalars(raw_row(id=1, event_id="evt_1"))
    session.respond_scalars(raw_row(id=2, event_id="evt_2", etl_status="failed"))
    instance, database = make_service(session)

    data = instance.replay(record_ids=(1, 2), dry_run=True)

    assert data["dry_run"] is True
    assert data["count"] == 2
    assert data["truncated"] is False
    assert [record["id"] for record in data["records"]] == [1, 2]
    assert data["records"][0]["fetch_time"] == "2026-10-27T02:00:00Z"

    # Nothing was written anywhere: no flush, no INSERT/UPDATE/DELETE, and the
    # session only ran SELECTs.
    assert session.added == []
    assert session.flush_count == 0
    assert mutations(session) == []
    assert all(not isinstance(statement, TextClause) for statement in session.statements)
    assert database.disposed is True


def test_replay_requeues_the_event_log_and_never_touches_ods() -> None:
    session = FakeSession()
    session.respond_scalars(raw_row(id=1, event_id="evt_1"))
    session.respond_scalars(raw_row(id=2, event_id="evt_2", etl_status="failed"))
    session.respond_scalars(object())
    session.respond_scalars(object())
    instance, _ = make_service(session)

    data = instance.replay(record_ids=(1, 2))

    assert data["dry_run"] is False
    assert data["count"] == 2
    assert data["requeued"] == 2
    assert data["unmatched"] == []

    assert session.added == []
    assert session.flush_count == 0
    assert len(session.dml) == 2
    assert all(isinstance(statement, Update) for statement in session.dml)
    assert {statement.table.name for statement in session.dml} == {"etl_event_log"}


def test_replay_reports_event_logs_it_could_not_requeue() -> None:
    session = FakeSession()
    session.respond_scalars(raw_row(id=1, event_id="evt_orphan"))
    instance, _ = make_service(session)

    data = instance.replay(record_ids=(1,))

    assert data["requeued"] == 0
    assert data["unmatched"] == ["evt_orphan"]


def test_replay_reports_missing_record_ids() -> None:
    instance, _ = make_service(FakeSession())

    with pytest.raises(NotFoundError) as caught:
        instance.replay(record_ids=(4,))

    assert caught.value.details is not None
    assert caught.value.details["ids"] == [4]


def test_replay_all_failed_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "MAX_REPLAY", 2)
    session = FakeSession()
    session.respond_scalars(raw_row(id=1), raw_row(id=2))
    instance, _ = make_service(session)

    data = instance.replay(all_failed=True, dry_run=True)

    assert data["count"] == 2
    assert data["truncated"] is True
    assert data["dry_run"] is True


def test_failed_rows_come_from_the_event_log_not_etl_status() -> None:
    """ODS is append-only, so ``etl_status`` never leaves ``pending`` (DEC-0021)."""

    session = FakeSession()
    session.respond_scalars(raw_row(id=1))
    instance, _ = make_service(session)

    instance.replay(all_failed=True, dry_run=True)

    statement = session.statements[-1]
    sql = " ".join(str(statement.compile(compile_kwargs={"literal_binds": True})).split())
    assert "JOIN ops.etl_event_log ON ops.etl_event_log.event_id = ods.stock_raw_data.event_id" in sql
    assert "ops.etl_event_log.status IN ('failed', 'dlq')" in sql
    assert "ods.stock_raw_data.etl_status =" not in sql


# -- CLI ---------------------------------------------------------------------


def _invoke(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, list(args))


class _FakeCliService:
    calls: list[tuple[str, dict[str, Any]]] = []
    raw = '{\r\n  "b": 1\r\n}\r\n'

    def __init__(self, **_: Any) -> None:
        pass

    def listing(self, **kwargs: Any) -> dict[str, Any]:
        _FakeCliService.calls.append(("listing", kwargs))
        return {"count": 0, "records": []}

    def show(self, record_id: int) -> dict[str, Any]:
        _FakeCliService.calls.append(("show", {"id": record_id}))
        return {"id": record_id}

    def raw_text(self, record_id: int) -> str:
        _FakeCliService.calls.append(("raw_text", {"id": record_id}))
        return self.raw

    def stats(self, *, group_by: str) -> dict[str, Any]:
        _FakeCliService.calls.append(("stats", {"group_by": group_by}))
        return {"group_by": group_by, "groups": [], "total": 0}

    def replay(self, **kwargs: Any) -> dict[str, Any]:
        _FakeCliService.calls.append(("replay", kwargs))
        return {"dry_run": True, "count": 0, "truncated": False, "records": []}


def test_cli_show_raw_prints_the_document_without_an_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from financecoffer.cli.commands import ods as ods_module

    _FakeCliService.calls = []
    monkeypatch.setattr(ods_module, "OdsService", _FakeCliService)

    result = _invoke("ods", "show", "12", "--raw")

    assert result.exit_code == 0
    assert result.stdout_bytes == _FakeCliService.raw.encode("utf-8")
    assert b"\r\n" in result.stdout_bytes
    assert _FakeCliService.calls == [("raw_text", {"id": 12})]


def test_read_payload_keeps_the_upstream_bytes(tmp_path: Path) -> None:
    from financecoffer.cli.commands import ingest as ingest_module

    event_file = tmp_path / "event.json"
    original = b'{\r\n  "a": 1\r\n}\r\n'
    event_file.write_bytes(original)

    mapping, text = ingest_module._read_payload(str(event_file))

    assert mapping == {"a": 1}
    assert text == original.decode("utf-8")
    assert text.encode("utf-8") == original


def test_read_payload_rejects_non_utf8_bytes(tmp_path: Path) -> None:
    from financecoffer.cli.commands import ingest as ingest_module

    event_file = tmp_path / "event.json"
    event_file.write_bytes(b'{"a": "\xff"}')

    with pytest.raises(InvalidArgumentError) as caught:
        ingest_module._read_payload(str(event_file))

    assert caught.value.details is not None
    assert caught.value.details["errors"][0]["field"] == "file"
    assert "UTF-8" in caught.value.details["errors"][0]["reason"]


def test_cli_list_emits_the_ods_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    from financecoffer.cli.commands import ods as ods_module

    _FakeCliService.calls = []
    monkeypatch.setattr(ods_module, "OdsService", _FakeCliService)

    result = _invoke("ods", "list", "--data-type", "financial", "--limit", "5")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["meta"]["source"] == "ods"
    assert _FakeCliService.calls == [
        (
            "listing",
            {
                "data_type": "financial",
                "symbol": None,
                "since": None,
                "until": None,
                "etl_status": None,
                "limit": 5,
            },
        )
    ]


def test_cli_replay_forwards_the_dry_run_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from financecoffer.cli.commands import ods as ods_module

    _FakeCliService.calls = []
    monkeypatch.setattr(ods_module, "OdsService", _FakeCliService)

    result = _invoke("ods", "replay", "--all-failed", "--dry-run")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["dry_run"] is True
    assert _FakeCliService.calls == [
        ("replay", {"record_ids": (), "all_failed": True, "dry_run": True})
    ]


def test_ods_write_path_contains_no_update_or_delete() -> None:
    """Static guard: nothing in the ODS write path may rewrite a raw row.

    Dict ``update`` calls are fine; SQLAlchemy's ``update``/``delete`` helpers
    and SQL text that mutates ``ods.stock_raw_data`` are not.
    """

    package = Path(landing.__file__).parent
    files = sorted(package.glob("*.py")) + [
        package.parent / "db" / "repositories" / "ods.py"
    ]
    forbidden_sql = re.compile(
        r"\b(?:UPDATE|DELETE\s+FROM)\s+ods\.stock_raw_data\b", re.IGNORECASE
    )
    forbidden_imports = {"update", "delete"}

    for path in files:
        code = path.read_text(encoding="utf-8")
        assert forbidden_sql.search(code) is None, path
        tree = ast.parse(code, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "sqlalchemy"
            ):
                imported = {alias.name for alias in node.names}
                assert not imported & forbidden_imports, path
