"""The two exact cut-offs the cold archive turns on.

``ArchiveService.run`` moves a row out of the hot tables when *either* test
fires: the row is older than the retention window, or its big column is larger
than the 8 KiB ceiling. Both are strict comparisons, so the interesting values
are the boundaries themselves:

* 8191 / 8192 bytes stay hot, 8193 bytes goes cold -- one byte decides;
* a row dated exactly on the cutoff day stays hot, one day earlier goes cold.

A third case pins the rerun contract: archiving twice uploads nothing new, and
a row whose marker write was interrupted reuses the object that is already in
the bucket instead of writing a second copy under the same key.

Objects live under a per-run ``test/`` prefix and are deleted afterwards; the
scratch database is shared with the other integration suites and cleaned per
test. When MinIO or PostgreSQL is unreachable the module skips.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, text

from financecoffer.archive import ArchiveService, S3Storage, cold_cutoff
from financecoffer.archive.service import DEFAULT_MIN_BYTES
from financecoffer.config.loader import load_config
from financecoffer.core.config import Config
from financecoffer.db.models.dwd import (
    StockAnnouncement,
    StockFinancialSnapshot,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.models.ops import ArchiveJob, EtlEventLog
from financecoffer.db.session import Database
from financecoffer.etl.processor import process_payload
from financecoffer.events import compute_event_id

ROOT = Path(__file__).resolve().parents[2]
PREFIX = f"test/boundary-{uuid.uuid4().hex[:8]}"
PROVIDER = "boundary-suite"
SYMBOL = "920030"
CRAWL_TIME = "2026-10-27T02:00:00Z"
COLD_YEARS = 3

DWD_MODELS = (
    StockFinancialSnapshot,
    StockAnnouncement,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)


# -- environment ------------------------------------------------------------


def _config() -> Config:
    env_file = ROOT / ".env"
    try:
        if env_file.is_file():
            return load_config(env_file=env_file)
        return load_config(env=os.environ)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot load the archive configuration: {exc}")


def _reachable(endpoint: str) -> bool:
    cleaned = endpoint.replace("http://", "").replace("https://", "")
    host, _, port = cleaned.partition(":")
    try:
        socket.create_connection((host, int(port or 9000)), 2).close()
    except OSError:
        return False
    return True


@pytest.fixture(scope="module")
def config() -> Config:
    settings = _config()
    if not _reachable(settings.minio_endpoint):
        pytest.skip(f"MinIO is not reachable at {settings.minio_endpoint}")
    if settings.minio_access_key is None or settings.minio_secret_key is None:
        pytest.skip("MinIO credentials are not configured")
    return settings


@pytest.fixture(scope="module")
def storage(config: Config) -> Iterator[S3Storage]:
    store = S3Storage(config)
    try:
        store.head(f"{PREFIX}/probe")  # auth + bucket probe, creates nothing
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"MinIO at {config.minio_endpoint} is not usable: {exc}")
    yield store
    for item in list(store.iter_objects(PREFIX)):
        store.delete(item.key)


@pytest.fixture()
def db(migrated_warehouse: str) -> Iterator[Database]:
    database = Database(migrated_warehouse)
    _clean(database)
    try:
        yield database
    finally:
        try:
            _clean(database)
        finally:
            database.dispose()


@pytest.fixture()
def prefix() -> str:
    return f"{PREFIX}/{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def archive(
    config: Config, db: Database, storage: S3Storage, prefix: str
) -> Iterator[ArchiveService]:
    service = ArchiveService(config=config, dsn=db.dsn, storage=storage, prefix=prefix)
    try:
        yield service
    finally:
        service.close()
        for item in list(storage.iter_objects(prefix)):
            storage.delete(item.key)


# -- helpers ----------------------------------------------------------------


def _clean(database: Database) -> None:
    with database.session_scope() as session:
        session.execute(delete(ArchiveJob).where(ArchiveJob.symbol == SYMBOL))
        for model in DWD_MODELS:
            session.execute(delete(model).where(model.provider == PROVIDER))
        session.execute(delete(StockRawData).where(StockRawData.symbol == SYMBOL))
        session.execute(delete(EtlEventLog).where(EtlEventLog.provider == PROVIDER))


def _event(data_type: str, *, biz_key: str, content: dict[str, Any], symbol: str = SYMBOL) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider=PROVIDER, data_type=data_type, symbol=symbol, biz_key=biz_key
        ),
        "biz_key": biz_key,
        "event_time": "2026-10-27T10:00:00+08:00",
        "data_type": data_type,
        "symbol": symbol,
        "market": "CN",
        "source_info": {"provider": PROVIDER, "crawl_time": CRAWL_TIME},
        "content": content,
        "trace_id": "trace-boundary",
    }


def _announcement(*, body: str, title: str, publish_date: str) -> dict[str, Any]:
    return {
        "quality_metrics": {"quality_status": "normal"},
        "raw_html": f"<p>{body}</p>",
        "clean_content": body,
        "structured_fields": {
            "title": title,
            "ann_type": "periodic",
            "publish_date": publish_date,
        },
    }


def _ingest(database: Database, body: dict[str, Any]) -> None:
    with database.session_scope() as session:
        process_payload(
            session,
            payload=body,
            raw_text=json.dumps(body, ensure_ascii=False),
            compliance_enabled=False,
        )


def _announce(database: Database, *, body: str, title: str, publish_date: str) -> int:
    envelope = _event(
        "announcement",
        biz_key=f"{publish_date}|{title}",
        content=_announcement(body=body, title=title, publish_date=publish_date),
    )
    _ingest(database, envelope)
    with database.session_scope() as session:
        return session.scalars(
            select(StockAnnouncement.id).where(
                StockAnnouncement.event_id == envelope["event_id"]
            )
        ).one()


def _row(database: Database, row_id: int) -> StockAnnouncement:
    with database.session_scope() as session:
        return session.scalars(
            select(StockAnnouncement).where(StockAnnouncement.id == row_id)
        ).one()


def _job_count(database: Database) -> int:
    with database.session_scope() as session:
        return len(
            list(session.scalars(select(ArchiveJob).where(ArchiveJob.symbol == SYMBOL)))
        )


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


# -- the size boundary ------------------------------------------------------


def test_eight_kibibytes_is_the_strict_size_boundary(
    db: Database, storage: S3Storage, archive: ArchiveService, prefix: str
) -> None:
    day = _today()
    sizes = (8191, 8192, 8193)
    ids = {
        size: _announce(db, body="A" * size, title=f"size-{size}", publish_date=day)
        for size in sizes
    }

    assert DEFAULT_MIN_BYTES == 8192

    report = archive.run(older_than_years=COLD_YEARS, min_bytes=8192)

    assert report["candidates"] == 1
    assert report["archived"] == 1
    assert report["skipped_existing"] == 0
    assert report["bytes"] == 8193

    for size in (8191, 8192):
        row = _row(db, ids[size])
        assert row.is_cold is False, f"{size} bytes must stay hot"
        assert row.minio_url is None
        assert row.clean_content == "A" * size

    cold = _row(db, ids[8193])
    assert cold.is_cold is True
    assert cold.clean_content is None
    assert cold.minio_url is not None
    assert cold.minio_url.startswith(f"minio://{archive.bucket}/")
    assert "/dwd/stock_announcement/" in cold.minio_url
    assert cold.minio_url.endswith(f"/{ids[8193]}.txt")

    objects = [item.key for item in storage.iter_objects(prefix)]
    assert len(objects) == 1
    assert objects[0].startswith(f"{prefix}/dwd/stock_announcement/")


# -- the age boundary -------------------------------------------------------


def test_the_cutoff_day_itself_stays_hot(db: Database, archive: ArchiveService) -> None:
    cutoff = cold_cutoff(datetime.now(UTC).date(), COLD_YEARS)
    edge = cutoff.isoformat()
    older = (cutoff - timedelta(days=1)).isoformat()

    on_edge = _announce(db, body="edge-but-small", title="on-cutoff", publish_date=edge)
    before = _announce(db, body="older-but-small", title="before-cutoff", publish_date=older)

    report = archive.run(older_than_years=COLD_YEARS, min_bytes=8192)

    assert report["cutoff"] == edge
    assert report["candidates"] == 1
    assert report["archived"] == 1

    edge_row = _row(db, on_edge)
    assert edge_row.is_cold is False
    assert edge_row.clean_content == "edge-but-small"

    old_row = _row(db, before)
    assert old_row.is_cold is True
    assert old_row.clean_content is None


# -- the rerun contract -----------------------------------------------------


def test_a_rerun_reuses_the_object_instead_of_uploading_twice(
    db: Database, storage: S3Storage, archive: ArchiveService, prefix: str
) -> None:
    body = "Z" * 9000
    row_id = _announce(db, body=body, title="rerun", publish_date=_today())

    first = archive.run(older_than_years=COLD_YEARS, min_bytes=8192)
    assert (first["archived"], first["skipped_existing"]) == (1, 0)
    assert len([item.key for item in storage.iter_objects(prefix)]) == 1
    jobs = _job_count(db)
    assert jobs == 1
    url = _row(db, row_id).minio_url
    assert url is not None

    second = archive.run(older_than_years=COLD_YEARS, min_bytes=8192)
    assert second["candidates"] == 0
    assert second["archived"] == 0
    assert second["skipped_existing"] == 0
    assert _job_count(db) == jobs

    # A crash between the upload and the marker write leaves the object behind
    # with the row still hot; the next run must find it and skip the upload.
    with db.session_scope() as session:
        session.execute(
            text(
                "UPDATE dwd.stock_announcement "
                "SET is_cold = false, minio_url = NULL, clean_content = :body "
                "WHERE id = :id"
            ),
            {"id": row_id, "body": body},
        )

    third = archive.run(older_than_years=COLD_YEARS, min_bytes=8192)
    assert third["archived"] == 1
    assert third["skipped_existing"] == 1
    assert third["bytes"] == 9000
    assert len([item.key for item in storage.iter_objects(prefix)]) == 1
    assert _row(db, row_id).minio_url == url
