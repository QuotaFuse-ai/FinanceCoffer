"""Unit tests for the cold archive: layout rules, storage adapter, service order.

Nothing here needs PostgreSQL or MinIO. The object layout and the upload rules
are pure functions, the S3 adapter is driven through a stand-in boto3 client,
and the service is exercised with a fake object store plus fixed row lists, so
the "upload first, mark second" contract and the failure accounting can be
checked without a warehouse. The real stores are covered in
``tests/integration/test_archive.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from financecoffer.archive.layout import (
    TABLES,
    build_candidate,
    canonical_json,
    cold_cutoff,
    key_from_url,
    minio_url,
    object_key,
    plain_mapping,
    sha256_hex,
    years_ago,
)
from financecoffer.archive.service import ArchiveService, _ArchivedRef, _TableStats
from financecoffer.archive.storage import S3Storage, StoredObject, endpoint_url
from financecoffer.core.config import Config
from financecoffer.core.errors import ConfigError, InvalidArgumentError, StorageError
from financecoffer.db.models.dwd import (
    StockAnnouncement,
    StockFinancialSnapshot,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)

DSN = "postgresql://fcoffer:fcoffer@127.0.0.1:5432/financecoffer"
#: Nothing listens here. The stubbed service must never need a warehouse, so a
#: connection attempt shows up in the report as ``journal_error`` instead of
#: quietly writing into a developer's database.
OFFLINE_DSN = "postgresql://fcoffer:fcoffer@127.0.0.1:1/financecoffer"
SPECS = {spec.name: spec for spec in TABLES}


# --- fakes -----------------------------------------------------------------


class FakeStorage:
    """In-memory ``ObjectStorage`` that records every call it receives."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.bodies: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.put_error: Exception | None = None
        self.drop_writes = False

    def head(self, key: str) -> StoredObject | None:
        self.calls.append(("head", key))
        return self.objects.get(key)

    def put(self, key: str, data: bytes, *, content_type: str, sha256: str) -> StoredObject:
        self.calls.append(("put", key))
        if self.put_error is not None:
            raise self.put_error
        if self.drop_writes:
            return StoredObject(key=key, size=0)
        self.bodies[key] = data
        stored = StoredObject(
            key=key,
            size=len(data),
            sha256=sha256,
            metadata={"sha256": sha256},
        )
        self.objects[key] = stored
        return stored

    def get(self, key: str) -> tuple[bytes, StoredObject]:
        self.calls.append(("get", key))
        if key not in self.bodies:
            raise StorageError("archived object is missing", details={"key": key})
        return self.bodies[key], self.objects[key]

    def iter_objects(self, prefix: str) -> Iterator[StoredObject]:
        self.calls.append(("iter", prefix))
        for key, stored in self.objects.items():
            if key.startswith(prefix):
                yield stored

    def delete(self, key: str) -> None:
        self.calls.append(("delete", key))
        self.objects.pop(key, None)
        self.bodies.pop(key, None)


class ExplodingDatabase:
    """``Database`` stand-in: every transaction attempt fails loudly."""

    def __init__(self) -> None:
        self.calls = 0

    def session_scope(self) -> Any:
        self.calls += 1
        raise RuntimeError("database is offline")

    def dispose(self) -> None:
        pass


def make_service(
    storage: FakeStorage | None = None,
    *,
    prefix: str = "",
    years: int = 3,
) -> ArchiveService:
    config = Config(
        db_url=DSN,
        minio_bucket="financecoffer",
        cold_after_years=years,
    )
    return ArchiveService(
        config=config,
        dsn=DSN,
        storage=storage if storage is not None else FakeStorage(),
        prefix=prefix,
    )


def announcement(
    *,
    row_id: int = 7,
    symbol: str = "600000",
    ann_date: date = date(2019, 4, 1),
    body: str = "贵州茅台 2019 年年度报告摘要",
    content_hash: str | None = None,
) -> StockAnnouncement:
    return StockAnnouncement(
        id=row_id,
        symbol=symbol,
        ann_date=ann_date,
        content_hash=content_hash if content_hash is not None else sha256_hex(body.encode()),
        clean_content=body,
        is_cold=False,
    )


# --- layout: cold decision -------------------------------------------------


def test_cold_cutoff_is_the_anniversary_itself() -> None:
    # A row stamped exactly three years ago is still hot: the cutoff is strict.
    assert cold_cutoff(date(2026, 10, 28), 3) == date(2023, 10, 28)
    assert cold_cutoff(date(2026, 10, 28), 0) == date(2026, 10, 28)


def test_years_ago_falls_back_to_february_28_on_a_leap_day() -> None:
    assert years_ago(date(2024, 2, 29), 1) == date(2023, 2, 28)
    assert years_ago(date(2024, 2, 29), 4) == date(2020, 2, 29)


# --- layout: object keys ---------------------------------------------------


def test_announcement_key_is_table_symbol_year_and_id() -> None:
    row = announcement()
    assert object_key(SPECS["stock_announcement"], row) == (
        "dwd/stock_announcement/600000/2019/7.txt"
    )


def test_versioned_keys_encode_the_business_identity() -> None:
    kline = StockKline(symbol="600000", trade_date=date(2019, 1, 2), adjust="none", version=1)
    quote = StockQuoteSnapshot(symbol="600000", trade_date=date(2019, 1, 2), version=2)
    fin = StockFinancialSnapshot(
        symbol="600000",
        report_date=date(2018, 12, 31),
        report_type="annual",
        version=3,
    )
    risk = StockRiskEvent(id=11, symbol="600000", occur_date=date(2019, 3, 8))

    assert object_key(SPECS["stock_kline"], kline) == (
        "dwd/stock_kline/600000/2019/2019-01-02_none_1.json"
    )
    assert object_key(SPECS["stock_quote_snapshot"], quote) == (
        "dwd/stock_quote_snapshot/600000/2019/2019-01-02_2.json"
    )
    assert object_key(SPECS["stock_financial_snapshot"], fin) == (
        "dwd/stock_financial_snapshot/600000/2018/2018-12-31_annual_3.json"
    )
    assert object_key(SPECS["stock_risk_event"], risk) == (
        "dwd/stock_risk_event/600000/2019/11.json"
    )


def test_prefix_and_hostile_segments_are_normalised() -> None:
    row = announcement(symbol="600/000")

    key = object_key(SPECS["stock_announcement"], row, prefix="test/t012/")

    assert key == "test/t012/dwd/stock_announcement/600_000/2019/7.txt"
    assert "\\" not in key and "\x00" not in key


def test_minio_url_round_trips_and_rejects_other_buckets() -> None:
    url = minio_url("financecoffer", "dwd/stock_kline/600000/2019/x.json")

    assert url == "minio://financecoffer/dwd/stock_kline/600000/2019/x.json"
    assert key_from_url(url, "financecoffer") == "dwd/stock_kline/600000/2019/x.json"
    assert key_from_url(url.replace("minio://", "s3://"), "financecoffer").endswith("x.json")
    with pytest.raises(StorageError):
        key_from_url(url, "other-bucket")
    with pytest.raises(StorageError):
        key_from_url("https://minio.local/x.json", "financecoffer")


# --- layout: payloads ------------------------------------------------------


def test_announcement_payload_is_the_body_and_matches_content_hash() -> None:
    row = announcement()

    candidate = build_candidate(SPECS["stock_announcement"], row, bucket="financecoffer")

    assert candidate.payload == row.clean_content.encode("utf-8")
    assert candidate.expected_sha256 == row.content_hash
    assert candidate.content_type == "text/plain; charset=utf-8"
    assert candidate.integrity_error is None
    assert candidate.minio_url == f"minio://financecoffer/{candidate.object_key}"


def test_announcement_hash_mismatch_is_reported_not_hidden() -> None:
    row = announcement(content_hash="0" * 64)

    candidate = build_candidate(SPECS["stock_announcement"], row, bucket="financecoffer")

    assert candidate.integrity_error == "content_hash does not match the stored body"


def test_announcement_without_body_cannot_be_archived() -> None:
    row = announcement()
    row.clean_content = None

    with pytest.raises(StorageError) as excinfo:
        build_candidate(SPECS["stock_announcement"], row, bucket="financecoffer")

    assert excinfo.value.details["table"] == "stock_announcement"


def test_versioned_payload_carries_the_cold_marker_without_mutating_the_row() -> None:
    kline = StockKline(
        symbol="600000",
        trade_date=date(2019, 1, 2),
        adjust="none",
        version=1,
        close=Decimal("11.5000"),
        quality_status="normal",
        is_cold=False,
    )

    candidate = build_candidate(SPECS["stock_kline"], kline, bucket="financecoffer")

    assert candidate.content_type == "application/json; charset=utf-8"
    assert b'"is_cold":true' in candidate.payload
    assert candidate.minio_url.encode() in candidate.payload
    # The row keeps its hot marker: only PostgreSQL writes change it.
    assert kline.is_cold is False
    assert kline.minio_url is None


def test_canonical_json_is_sorted_compact_and_json_safe() -> None:
    payload = canonical_json(
        {
            "symbol": "600000",
            "close": Decimal("11.5000"),
            "trade_date": date(2019, 1, 2),
            "note": "贵州茅台",
        }
    )

    assert payload.decode("utf-8") == (
        '{"close":"11.5000","note":"贵州茅台","symbol":"600000","trade_date":"2019-01-02"}'
    )
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_plain_mapping_is_json_safe() -> None:
    mapped = plain_mapping(
        {"symbol": "600000", "trade_date": date(2019, 1, 2), "close": Decimal("1.5")}
    )

    assert mapped == {"symbol": "600000", "trade_date": "2019-01-02", "close": "1.5"}


# --- storage adapter -------------------------------------------------------


class FakeClientError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeS3Client:
    """Just enough of the boto3 S3 surface for the adapter."""

    def __init__(self) -> None:
        self.stored: dict[str, dict[str, Any]] = {}
        self.failure: Exception | None = None
        self.puts: list[dict[str, Any]] = []

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        entry = self.stored.get(Key)
        if entry is None:
            raise FakeClientError("404", 404)
        return {
            "ContentLength": entry["size"],
            "ETag": '"abc"',
            "Metadata": entry["metadata"],
        }

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)
        self.stored[kwargs["Key"]] = {
            "size": len(kwargs["Body"]),
            "metadata": kwargs.get("Metadata") or {},
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        entry = self.stored.get(Key)
        if entry is None:
            raise FakeClientError("NoSuchKey", 404)
        return {
            "Body": _Body(b"payload"),
            "ContentLength": entry["size"],
            "Metadata": entry["metadata"],
        }


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def storage_with(client: FakeS3Client) -> S3Storage:
    config = Config(
        minio_bucket="financecoffer",
        minio_access_key=SecretStr("access"),
        minio_secret_key=SecretStr("secret"),
    )
    return S3Storage(config, client=client)


def test_put_records_the_sha256_as_object_metadata() -> None:
    client = FakeS3Client()

    stored = storage_with(client).put(
        "dwd/x.txt", b"body", content_type="text/plain", sha256="a" * 64
    )

    assert stored.sha256 == "a" * 64
    assert client.puts[0]["Metadata"] == {"sha256": "a" * 64}
    assert client.puts[0]["ContentType"] == "text/plain"


def test_head_returns_none_for_a_missing_object() -> None:
    assert storage_with(FakeS3Client()).head("dwd/missing.txt") is None


def test_get_reports_a_missing_object_as_storage_error() -> None:
    with pytest.raises(StorageError) as excinfo:
        storage_with(FakeS3Client()).get("dwd/missing.txt")

    assert excinfo.value.message == "archived object is missing"


def test_storage_errors_carry_bucket_and_key_but_no_credentials() -> None:
    client = FakeS3Client()
    client.failure = RuntimeError("AccessDenied for secret_key=AKIAEXAMPLE123")

    with pytest.raises(StorageError) as excinfo:
        storage_with(client).head("dwd/x.txt")

    assert excinfo.value.details["bucket"] == "financecoffer"
    assert excinfo.value.details["key"] == "dwd/x.txt"
    assert "AKIAEXAMPLE123" not in excinfo.value.details["error"]
    assert "***" in excinfo.value.details["error"]


def test_client_requires_credentials() -> None:
    with pytest.raises(ConfigError) as excinfo:
        S3Storage(Config(minio_bucket="financecoffer"))

    assert excinfo.value.details["hint"].startswith("set FC_MINIO_ACCESS_KEY")


def test_endpoint_url_adds_the_scheme_once() -> None:
    assert endpoint_url("127.0.0.1:9100") == "http://127.0.0.1:9100"
    assert endpoint_url("http://127.0.0.1:9100/") == "http://127.0.0.1:9100"
    assert endpoint_url("https://minio.local") == "https://minio.local"


# --- service: argument validation -----------------------------------------


def test_older_than_years_defaults_to_the_configured_window() -> None:
    service = make_service(years=5)

    assert service._resolve_years(None) == 5
    assert service._resolve_years(0) == 0
    with pytest.raises(InvalidArgumentError):
        service._resolve_years(-1)


def test_min_bytes_limit_and_sample_are_validated() -> None:
    service = make_service()

    assert service._resolve_min_bytes(None) == 8192
    assert service._resolve_min_bytes(1) == 1
    assert service._resolve_limit(None) is None
    assert service._resolve_limit(3) == 3
    assert service._resolve_sample(None) == 100
    for call in (
        lambda: service._resolve_min_bytes(0),
        lambda: service._resolve_limit(0),
        lambda: service._resolve_sample(0),
        lambda: service._resolve_sample(10_001),
    ):
        with pytest.raises(InvalidArgumentError):
            call()


# --- service: the upload/mark order ---------------------------------------


def test_cold_predicate_adds_the_size_test_only_for_big_columns() -> None:
    service = make_service()
    cutoff = date(2023, 10, 28)

    def compiled(table: str) -> str:
        expr = service._cold_predicate(SPECS[table], cutoff, 8192)
        return str(select(1).where(expr).compile(dialect=postgresql.dialect()))

    kline = compiled("stock_kline")
    announcement_sql = compiled("stock_announcement")
    financial = compiled("stock_financial_snapshot")

    assert "octet_length" not in kline
    assert "is_cold" in kline and "trade_date" in kline
    assert "octet_length" in announcement_sql and "clean_content" in announcement_sql
    assert "balance_check_result" in financial


def test_ensure_object_skips_an_identical_object() -> None:
    storage = FakeStorage()
    service = make_service(storage)
    candidate = build_candidate(SPECS["stock_announcement"], announcement(), bucket="financecoffer")
    storage.put(
        candidate.object_key,
        candidate.payload,
        content_type=candidate.content_type,
        sha256=candidate.expected_sha256,
    )
    storage.calls.clear()

    uploaded = service._ensure_object(candidate)

    assert uploaded is False
    assert [call for call in storage.calls if call[0] == "put"] == []


def test_ensure_object_uploads_then_confirms_by_reading_it_back() -> None:
    storage = FakeStorage()
    service = make_service(storage)
    candidate = build_candidate(SPECS["stock_announcement"], announcement(), bucket="financecoffer")

    uploaded = service._ensure_object(candidate)

    assert uploaded is True
    assert [name for name, _ in storage.calls] == ["head", "put", "head"]
    assert storage.bodies[candidate.object_key] == candidate.payload


def test_ensure_object_rejects_an_upload_that_never_landed() -> None:
    storage = FakeStorage()
    storage.drop_writes = True
    service = make_service(storage)
    candidate = build_candidate(SPECS["stock_announcement"], announcement(), bucket="financecoffer")

    with pytest.raises(StorageError) as excinfo:
        service._ensure_object(candidate)

    assert excinfo.value.message == "uploaded object cannot be found"


def test_archive_row_stops_before_the_marker_when_the_upload_fails() -> None:
    storage = FakeStorage()
    storage.put_error = StorageError("MinIO put_object failed", details={"key": "k"})
    service = make_service(storage)
    service._database = ExplodingDatabase()  # type: ignore[assignment]
    marked: list[str] = []
    service._mark_archived = lambda spec, candidate: marked.append(candidate.object_key)  # type: ignore[method-assign]
    stats = _TableStats(table="stock_announcement")
    failures: list[dict[str, Any]] = []

    service._archive_row(
        SPECS["stock_announcement"], announcement(), stats, failures
    )

    assert marked == []
    assert (stats.candidates, stats.archived, stats.failed) == (1, 0, 1)
    assert failures[0]["reason"] == "MinIO put_object failed"
    assert failures[0]["journal_error"] == "database is offline"


def test_archive_row_marks_the_row_after_the_object_is_confirmed() -> None:
    storage = FakeStorage()
    service = make_service(storage)
    marked: list[str] = []
    service._mark_archived = lambda spec, candidate: marked.append(candidate.object_key)  # type: ignore[method-assign]
    stats = _TableStats(table="stock_announcement")
    failures: list[dict[str, Any]] = []

    service._archive_row(SPECS["stock_announcement"], announcement(), stats, failures)

    assert marked == ["dwd/stock_announcement/600000/2019/7.txt"]
    assert [name for name, _ in storage.calls] == ["head", "put", "head"]
    assert storage.calls[1][1] == marked[0]
    assert (stats.archived, stats.failed) == (1, 0)
    assert failures == []


# --- service: run ----------------------------------------------------------


class StubService(ArchiveService):
    """``run`` over a fixed row list, with the marker recorded instead of written.

    Failed rows are journalled in memory too: ``_record_failed_job`` is the only
    other place ``run`` reaches the warehouse, and a unit test must not write to
    whatever database its DSN happens to point at.
    """

    def __init__(self, rows: dict[str, list[Any]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rows = rows
        self.marked: list[str] = []
        self.windows: list[date] = []
        self.scopes: list[tuple[str, ...]] = []
        self.journaled: list[dict[str, Any]] = []

    def _record_failed_job(
        self, spec: Any, object_key: str, symbol: str | None, reason: str
    ) -> str | None:
        self.journaled.append(
            {
                "table": spec.name,
                "object_key": object_key,
                "symbol": symbol,
                "reason": reason,
            }
        )
        return None

    def _iter_cold_rows(
        self,
        spec: Any,
        cutoff: date,
        min_bytes: int,
        *,
        limit: int | None,
        symbols: Any = (),
    ) -> Iterator[Any]:
        self.windows.append(cutoff)
        self.scopes.append(tuple(symbols))
        rows = [row for row in self.rows.get(spec.name, []) if not symbols or row.symbol in symbols]
        yield from rows[:limit] if limit is not None else rows

    def _mark_archived(self, spec: Any, candidate: Any) -> None:
        self.marked.append(candidate.object_key)


def stub_service(rows: dict[str, list[Any]], *, storage: FakeStorage | None = None) -> StubService:
    store = storage if storage is not None else FakeStorage()
    # A port nothing listens on: if a stub ever dials the warehouse the failure
    # surfaces as ``journal_error`` instead of landing in a real ledger.
    config = Config(db_url=OFFLINE_DSN, minio_bucket="financecoffer")
    return StubService(
        rows,
        config=config,
        dsn=OFFLINE_DSN,
        storage=store,
        prefix="test/t012",
    )


def test_dry_run_counts_bytes_and_writes_nothing() -> None:
    storage = FakeStorage()
    service = stub_service({"stock_announcement": [announcement()]}, storage=storage)

    payload = service.run(dry_run=True)

    assert payload["dry_run"] is True
    assert payload["candidates"] == 1
    assert payload["archived"] == 0
    assert payload["bytes"] == len(announcement().clean_content.encode())
    assert storage.calls == []
    assert service.marked == []
    assert payload["tables"][0] == {
        "table": "stock_announcement",
        "candidates": 1,
        "archived": 0,
        "skipped_existing": 0,
        "bytes": payload["bytes"],
        "failed": 0,
    }


def test_run_archives_and_reports_the_cutoff_it_used() -> None:
    storage = FakeStorage()
    service = stub_service(
        {"stock_announcement": [announcement(), announcement(row_id=8)]}, storage=storage
    )

    payload = service.run(older_than_years=2)

    assert payload["older_than_years"] == 2
    assert payload["cutoff"] == cold_cutoff(datetime.now(UTC).date(), 2).isoformat()
    assert payload["archived"] == 2
    assert payload["skipped_existing"] == 0
    assert len(service.marked) == 2
    assert {name for name, _ in storage.calls} == {"head", "put"}


def test_run_stops_at_the_limit_and_counts_a_rerun_idempotently() -> None:
    storage = FakeStorage()
    rows = [announcement(row_id=7), announcement(row_id=8), announcement(row_id=9)]
    service = stub_service({"stock_announcement": rows}, storage=storage)

    first = service.run(limit=2)
    assert first["archived"] == 2

    puts_after_first = len([call for call in storage.calls if call[0] == "put"])

    # A rerun over the same window finds the objects already in place: the
    # marker is re-applied but no bytes are uploaded again.
    service.marked.clear()
    second = service.run(limit=2)
    puts_after_second = len([call for call in storage.calls if call[0] == "put"])

    assert second["archived"] == 2
    assert second["skipped_existing"] == 2
    assert puts_after_second == puts_after_first
    assert first["bytes"] == second["bytes"]


def test_run_reports_a_broken_announcement_without_aborting_the_batch() -> None:
    storage = FakeStorage()
    broken = announcement(row_id=3, content_hash="0" * 64)
    service = stub_service(
        {"stock_announcement": [broken, announcement(row_id=4)]}, storage=storage
    )

    payload = service.run()

    assert payload["archived"] == 1
    assert payload["failed"] == 1
    assert payload["failed"] == 1
    assert payload["failures"][0]["primary_key"] == {"id": 3}
    assert payload["failures"][0]["reason"] == "content_hash does not match the stored body"
    # The row is booked in the job ledger, but through the in-memory journal of
    # the stub -- never through a live database connection.
    assert "journal_error" not in payload["failures"][0]
    assert service.journaled == [
        {
            "table": "stock_announcement",
            "object_key": "test/t012/dwd/stock_announcement/600000/2019/3.txt",
            "symbol": "600000",
            "reason": "content_hash does not match the stored body",
        }
    ]
    assert service.marked == ["test/t012/dwd/stock_announcement/600000/2019/4.txt"]


# --- service: scope filters ------------------------------------------------


def test_symbol_filter_is_normalised_and_deduplicated() -> None:
    assert ArchiveService._resolve_symbols(None) == ()
    assert ArchiveService._resolve_symbols([]) == ()
    assert ArchiveService._resolve_symbols(["600000", " 600000 ", "600519"]) == (
        "600000",
        "600519",
    )


def test_table_filter_keeps_schema_order_and_rejects_unknown_names() -> None:
    assert ArchiveService._resolve_tables(None) == TABLES
    chosen = ArchiveService._resolve_tables(["stock_risk_event", "stock_announcement"])
    assert [spec.name for spec in chosen] == [
        spec.name for spec in TABLES if spec.name in {"stock_announcement", "stock_risk_event"}
    ]

    with pytest.raises(InvalidArgumentError) as excinfo:
        ArchiveService._resolve_tables(["stock_bogus"])

    assert excinfo.value.details["field"] == "table"
    assert "stock_announcement" in excinfo.value.details["allowed"]


def test_run_with_a_symbol_scope_leaves_the_other_symbol_hot() -> None:
    storage = FakeStorage()
    rows = [
        announcement(row_id=7, symbol="600000"),
        announcement(row_id=8, symbol="600519"),
    ]
    service = stub_service({"stock_announcement": rows}, storage=storage)

    payload = service.run(symbols=["600000"])

    assert payload["symbols"] == ["600000"]
    assert payload["archived"] == 1
    assert service.scopes[0] == ("600000",)
    assert service.marked == ["test/t012/dwd/stock_announcement/600000/2019/7.txt"]


def test_run_with_a_table_scope_only_visits_the_named_table() -> None:
    storage = FakeStorage()
    risk = StockRiskEvent(id=11, symbol="600519", occur_date=date(2019, 3, 8))
    service = stub_service(
        {"stock_announcement": [announcement()], "stock_risk_event": [risk]},
        storage=storage,
    )

    payload = service.run(tables=["stock_risk_event"])

    assert payload["tables_scoped"] == ["stock_risk_event"]
    assert payload["archived"] == 1
    assert service.marked == ["test/t012/dwd/stock_risk_event/600519/2019/11.json"]


def test_dry_run_previews_each_candidate_and_writes_nothing() -> None:
    storage = FakeStorage()
    service = stub_service({"stock_announcement": [announcement()]}, storage=storage)

    payload = service.run(dry_run=True, symbols=["600000"])

    assert payload["dry_run"] is True
    assert payload["candidates"] == 1
    assert payload["candidates_detail_truncated"] is False
    assert payload["candidates_detail"] == [
        {
            "table": "stock_announcement",
            "primary_key": {"id": 7},
            "symbol": "600000",
            "reference_date": "2019-04-01",
            "object_key": "test/t012/dwd/stock_announcement/600000/2019/7.txt",
            "bytes": len(announcement().clean_content.encode()),
        }
    ]
    assert storage.calls == []
    assert service.marked == []


def test_dry_run_truncates_the_preview_but_counts_every_candidate() -> None:
    rows = [announcement(row_id=index) for index in range(1, 61)]
    service = stub_service({"stock_announcement": rows})

    payload = service.run(dry_run=True)

    assert payload["candidates"] == 60
    assert len(payload["candidates_detail"]) == 50
    assert payload["candidates_detail_truncated"] is True
    assert service.marked == []


# --- service: restore preview ---------------------------------------------


class StubRestore(ArchiveService):
    """``restore`` over fixed refs with the write-back recorded, not applied."""

    def __init__(self, refs: list[_ArchivedRef], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.refs = refs
        self.written: list[tuple[dict[str, Any], str]] = []

    def _archived_refs(
        self,
        spec: Any,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[_ArchivedRef]:
        for ref in self.refs:
            if ref.spec.name == spec.name:
                yield ref

    def _restore_announcement(self, ref: _ArchivedRef, text: str) -> None:
        self.written.append((ref.primary_key, text))


def restore_service(
    refs: list[_ArchivedRef], *, storage: FakeStorage
) -> StubRestore:
    config = Config(db_url=OFFLINE_DSN, minio_bucket="financecoffer")
    return StubRestore(refs, config=config, dsn=OFFLINE_DSN, storage=storage)


def _archive_object(storage: FakeStorage, key: str, body: bytes) -> None:
    storage.put(
        key,
        body,
        content_type="text/plain; charset=utf-8",
        sha256=sha256_hex(body),
    )


def test_restore_dry_run_verifies_objects_without_writing_back() -> None:
    body = "贵州茅台 2019 年年度报告摘要"
    announcement_key = "test/t012/dwd/stock_announcement/600000/2019/7.txt"
    kline_key = "dwd/stock_kline/600000/2019/2019-01-02_none_1.json"
    storage = FakeStorage()
    _archive_object(storage, announcement_key, body.encode("utf-8"))
    _archive_object(storage, kline_key, b'{"is_cold":true}')
    refs = [
        _ArchivedRef(
            spec=SPECS["stock_announcement"],
            primary_key={"id": 7},
            url=minio_url("financecoffer", announcement_key),
            content_hash=sha256_hex(body.encode("utf-8")),
        ),
        _ArchivedRef(
            spec=SPECS["stock_kline"],
            primary_key={"symbol": "600000", "trade_date": date(2019, 1, 2)},
            url=minio_url("financecoffer", kline_key),
            content_hash=None,
        ),
    ]
    service = restore_service(refs, storage=storage)

    preview = service.restore("600000", dry_run=True)

    assert preview["dry_run"] is True
    assert preview["scanned"] == 2
    assert preview["would_restore"] == 1
    assert preview["verified"] == 1  # the kline is verified, never rewritten
    assert preview["restored"] == 0
    assert preview["failed"] == 0
    assert service.written == []


def test_restore_writes_the_announcement_body_back() -> None:
    body = "贵州茅台 2019 年年度报告摘要"
    key = "test/t012/dwd/stock_announcement/600000/2019/7.txt"
    storage = FakeStorage()
    _archive_object(storage, key, body.encode("utf-8"))
    ref = _ArchivedRef(
        spec=SPECS["stock_announcement"],
        primary_key={"id": 7},
        url=minio_url("financecoffer", key),
        content_hash=sha256_hex(body.encode("utf-8")),
    )
    service = restore_service([ref], storage=storage)

    report = service.restore("600000")

    assert report["restored"] == 1
    assert report["would_restore"] == 0
    assert service.written == [({"id": 7}, body)]
