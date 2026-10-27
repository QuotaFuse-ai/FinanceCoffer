"""Integration tests for the cold archive against real MinIO and PostgreSQL.

The three paths the task asks for are covered end to end:

* archive: an old announcement, an oversized announcement and the versioned
  tables land in MinIO, and only after the object is confirmed;
* restore: an announcement body comes back and still matches ``content_hash``;
* the source tables stay readable after archiving -- versioned tables keep every
  business column and every row, only ``is_cold`` / ``minio_url`` move.

Objects live under a per-run ``test/`` prefix and are deleted afterwards. When
MinIO is unreachable the whole module skips, and the scratch database is created
and dropped like the other integration suites do it.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from financecoffer.archive import ArchiveService, S3Storage
from financecoffer.archive.layout import sha256_hex
from financecoffer.cli import app
from financecoffer.config.loader import load_config
from financecoffer.core.config import Config
from financecoffer.db import MigrationRunner, connect
from financecoffer.db.models.dwd import (
    StockAnnouncement,
    StockFinancialSnapshot,
    StockKline,
    StockQuoteSnapshot,
    StockRiskEvent,
)
from financecoffer.db.session import Database

ROOT = Path(__file__).resolve().parents[2]
MAINTENANCE_DSN = os.environ.get(
    "FC_TEST_DB_URL", "postgresql://fcoffer:fcoffer@127.0.0.1:5432/postgres"
)
PREFIX = f"test/t012-{uuid.uuid4().hex[:8]}"
SYMBOL = "600000"
OTHER_SYMBOL = "600519"

runner = CliRunner()


# --- environment -----------------------------------------------------------


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
        pytest.skip("MinIO credentials are not configured in .env")
    return settings


@pytest.fixture(scope="module")
def storage(config: Config) -> Iterator[S3Storage]:
    store = S3Storage(config)
    try:
        store.head(f"{PREFIX}/probe")  # auth + bucket check, creates nothing
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"MinIO at {config.minio_endpoint} is not usable: {exc}")
    yield store
    for item in list(store.iter_objects(PREFIX)):
        store.delete(item.key)


@pytest.fixture
def scratch_database() -> Iterator[Database]:
    """A fresh migrated warehouse database, dropped after the test."""

    try:
        admin = connect(MAINTENANCE_DSN, timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL is not reachable: {exc}")
    name = f"fcoffer_t012_it_{uuid.uuid4().hex[:10]}"
    try:
        admin.autocommit = True
        admin.execute(f'CREATE DATABASE "{name}"')
    except Exception as exc:  # pragma: no cover - environment dependent
        admin.close()
        pytest.skip(f"cannot create a scratch database: {exc}")
    admin.close()

    dsn = MAINTENANCE_DSN.rsplit("/", 1)[0] + f"/{name}"
    try:
        MigrationRunner(dsn, timeout=10).apply()
        database = Database(dsn)
        try:
            yield database
        finally:
            database.dispose()
    finally:
        cleanup = connect(MAINTENANCE_DSN, timeout=5)
        try:
            cleanup.autocommit = True
            cleanup.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            cleanup.close()


@pytest.fixture
def service(
    config: Config, scratch_database: Database, storage: S3Storage
) -> Iterator[ArchiveService]:
    instance = ArchiveService(
        config=config,
        dsn=scratch_database.dsn,
        storage=storage,
        prefix=PREFIX,
    )
    try:
        yield instance
    finally:
        instance.close()


# --- seed data -------------------------------------------------------------

TODAY = datetime.now(UTC).date()
OLD = TODAY - timedelta(days=365 * 5)
RECENT = TODAY - timedelta(days=10)
REPORT_DATE = OLD - timedelta(days=120)
BIG_BODY = "内容" * 3000  # 9000 bytes, over the 8192-byte threshold
OLD_BODY = "2019 年年度报告摘要：营业收入同比增长 15.2%。"
HOT_BODY = "本周投资者关系活动记录表。"


def _columns(model: Any) -> list[str]:
    return [column.key for column in model.__table__.columns]


def _read(database: Database, model: Any, key: Any) -> dict[str, Any]:
    with database.session_scope() as session:
        row = session.get(model, key)
        assert row is not None, f"{model.__tablename__} lost its row {key}"
        return {name: getattr(row, name) for name in _columns(model)}


def _count(database: Database, model: Any) -> int:
    with database.session_scope() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _id_of(database: Database, model: Any, event_id: str) -> int:
    with database.session_scope() as session:
        value = session.scalar(select(model.id).where(model.event_id == event_id))
        assert value is not None
        return int(value)


def _seed(database: Database) -> dict[str, Any]:
    """Insert rows on both sides of the age and the size boundary."""

    stamp = datetime(2019, 4, 1, 9, 30, tzinfo=UTC)
    with database.session_scope() as session:
        session.add_all(
            [
                StockAnnouncement(
                    symbol=SYMBOL,
                    ann_date=OLD,
                    ann_type="annual",
                    title="2019 年年度报告",
                    content_hash=sha256_hex(OLD_BODY.encode("utf-8")),
                    clean_content=OLD_BODY,
                    quality_status="normal",
                    fetch_time=stamp,
                    event_id="t012-ann-old",
                    provider="test",
                ),
                StockAnnouncement(
                    symbol=SYMBOL,
                    ann_date=RECENT,
                    ann_type="report",
                    title="近期长公告",
                    content_hash=sha256_hex(BIG_BODY.encode("utf-8")),
                    clean_content=BIG_BODY,
                    quality_status="normal",
                    fetch_time=stamp,
                    event_id="t012-ann-big",
                    provider="test",
                ),
                StockAnnouncement(
                    symbol=SYMBOL,
                    ann_date=RECENT,
                    ann_type="report",
                    title="近期短公告",
                    content_hash=sha256_hex(HOT_BODY.encode("utf-8")),
                    clean_content=HOT_BODY,
                    quality_status="normal",
                    fetch_time=stamp,
                    event_id="t012-ann-hot",
                    provider="test",
                ),
                StockKline(
                    symbol=SYMBOL,
                    trade_date=OLD,
                    adjust="none",
                    version=1,
                    open=Decimal("10.0000"),
                    high=Decimal("11.0000"),
                    low=Decimal("9.5000"),
                    close=Decimal("10.5000"),
                    volume=Decimal("1000.0000"),
                    quality_status="normal",
                    fetch_time=stamp,
                    event_id="t012-kline-old",
                    provider="test",
                ),
                StockKline(
                    symbol=SYMBOL,
                    trade_date=RECENT,
                    adjust="none",
                    version=1,
                    close=Decimal("12.0000"),
                    quality_status="normal",
                    fetch_time=stamp,
                    event_id="t012-kline-hot",
                    provider="test",
                ),
                StockQuoteSnapshot(
                    symbol=SYMBOL,
                    trade_date=OLD,
                    version=1,
                    close=Decimal("10.5000"),
                    total_market_cap=Decimal("1000.0000"),
                    quality_status="normal",
                    fetch_time=stamp,
                    event_id="t012-quote-old",
                    provider="test",
                ),
                StockFinancialSnapshot(
                    symbol=SYMBOL,
                    report_date=REPORT_DATE,
                    report_type="annual",
                    statement="income",
                    version=1,
                    amount_revenue=Decimal("100.0000"),
                    amount_net_profit=Decimal("10.0000"),
                    quality_status="normal",
                    balance_check_result={"assets": "100", "liabilities": "40"},
                    fetch_time=stamp,
                    event_id="t012-fin-old",
                    provider="test",
                ),
                StockRiskEvent(
                    symbol=OTHER_SYMBOL,
                    occur_date=OLD,
                    risk_type="litigation",
                    severity="medium",
                    summary="涉及重大诉讼",
                    quality_status="normal",
                    fetch_time=stamp,
                    event_id="t012-risk-old",
                    provider="test",
                ),
            ]
        )

    return {
        "announcements": {
            "old": _id_of(database, StockAnnouncement, "t012-ann-old"),
            "big": _id_of(database, StockAnnouncement, "t012-ann-big"),
            "hot": _id_of(database, StockAnnouncement, "t012-ann-hot"),
        },
        "kline": (SYMBOL, OLD, "none", 1),
        "kline_hot": (SYMBOL, RECENT, "none", 1),
        "quote": (SYMBOL, OLD, 1),
        "financial": (SYMBOL, REPORT_DATE, "annual", "income", 1),
        "risk": _id_of(database, StockRiskEvent, "t012-risk-old"),
    }


# --- tests -----------------------------------------------------------------


def test_dry_run_on_an_empty_warehouse_reports_zero(scratch_database: Database) -> None:
    result = runner.invoke(
        app,
        ["--db-url", scratch_database.dsn, "archive", "run", "--dry-run"],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)

    assert payload["ok"] is True
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["candidates"] == 0
    assert payload["data"]["archived"] == 0
    assert payload["data"]["fc_archive_failed_total"] == 0
    assert payload["meta"]["source"] == "archive"
    assert _count(scratch_database, StockAnnouncement) == 0


def test_archive_restore_and_verify_round_trip(
    service: ArchiveService, scratch_database: Database, storage: S3Storage
) -> None:
    ids = _seed(scratch_database)
    announcements = ids["announcements"]
    before = {
        "kline": _read(scratch_database, StockKline, ids["kline"]),
        "quote": _read(scratch_database, StockQuoteSnapshot, ids["quote"]),
        "financial": _read(scratch_database, StockFinancialSnapshot, ids["financial"]),
        "risk": _read(scratch_database, StockRiskEvent, ids["risk"]),
    }
    row_counts = {
        model.__tablename__: _count(scratch_database, model)
        for model in (
            StockAnnouncement,
            StockKline,
            StockQuoteSnapshot,
            StockFinancialSnapshot,
            StockRiskEvent,
        )
    }

    # -- 1. archive ------------------------------------------------------
    report = service.run()

    assert report["failed"] == 0, report["failures"]
    assert report["archived"] == 6
    assert report["older_than_years"] == 3
    assert report["min_bytes"] == 8192
    assert report["bytes"] > len(BIG_BODY.encode("utf-8"))

    old_ann = _read(scratch_database, StockAnnouncement, announcements["old"])
    big_ann = _read(scratch_database, StockAnnouncement, announcements["big"])
    hot_ann = _read(scratch_database, StockAnnouncement, announcements["hot"])

    for archived, body in ((old_ann, OLD_BODY), (big_ann, BIG_BODY)):
        assert archived["is_cold"] is True
        assert archived["clean_content"] is None  # only the object keeps the body
        assert archived["content_hash"] == sha256_hex(body.encode("utf-8"))
        assert str(archived["minio_url"]).startswith(f"minio://{service.bucket}/{PREFIX}/dwd/")
    assert str(big_ann["minio_url"]).endswith(".txt")

    # The recent, small announcement stays hot and readable.
    assert hot_ann["is_cold"] is False
    assert hot_ann["clean_content"] == HOT_BODY
    assert hot_ann["minio_url"] is None

    # The object holds the original body, so its hash still identifies the row.
    key = str(old_ann["minio_url"]).split(f"{service.bucket}/", 1)[1]
    stored = storage.head(key)
    assert stored is not None
    assert stored.sha256 == old_ann["content_hash"]
    body, _ = storage.get(key)
    assert body.decode("utf-8") == OLD_BODY

    # -- 2. the source tables stay fully readable ------------------------
    for table, model, primary_key in (
        ("kline", StockKline, ids["kline"]),
        ("quote", StockQuoteSnapshot, ids["quote"]),
        ("financial", StockFinancialSnapshot, ids["financial"]),
        ("risk", StockRiskEvent, ids["risk"]),
    ):
        after = _read(scratch_database, model, primary_key)
        assert after["is_cold"] is True
        assert after["minio_url"] is not None
        for column in _columns(model):
            if column in {"is_cold", "minio_url", "ingested_at"}:
                continue
            assert after[column] == before[table][column], f"{table}.{column} was rewritten"
        assert _count(scratch_database, model) == row_counts[model.__tablename__]

    assert _read(scratch_database, StockKline, ids["kline_hot"])["is_cold"] is False

    status = service.status()
    assert status["totals"]["cold"] == 6
    assert status["totals"]["hot"] == 2
    assert status["totals"]["anomalies"] == 0
    assert status["totals"]["archived_bytes"] == report["bytes"]

    # -- 3. a rerun is idempotent and uploads nothing ---------------------
    objects_before = len(list(storage.iter_objects(PREFIX)))
    again = service.run()

    assert again["candidates"] == 0
    assert again["archived"] == 0
    assert len(list(storage.iter_objects(PREFIX))) == objects_before

    # -- 4. restore brings the announcement body back ---------------------
    restored = service.restore(SYMBOL)

    assert restored["failed"] == 0, restored["failures"]
    assert restored["fc_missing_chunk_total"] == 0
    assert restored["restored"] == 2
    assert restored["verified"] == 3  # kline, quote, financial (risk is another symbol)

    back = _read(scratch_database, StockAnnouncement, announcements["old"])
    assert back["clean_content"] == OLD_BODY
    assert back["is_cold"] is False
    assert back["minio_url"] == old_ann["minio_url"]  # kept as provenance
    assert back["content_hash"] == old_ann["content_hash"]
    assert _read(scratch_database, StockAnnouncement, announcements["big"])["clean_content"] == (
        BIG_BODY
    )

    # Versioned rows were verified, not rewritten: they stay cold.
    assert _read(scratch_database, StockKline, ids["kline"])["is_cold"] is True

    other = service.restore(OTHER_SYMBOL)
    assert other["restored"] == 0
    assert other["verified"] == 1  # the risk event, matched by symbol

    # -- 5. re-archiving reuses the same objects --------------------------
    redo = service.run()

    assert redo["archived"] == 2
    assert redo["skipped_existing"] == 2  # same keys, same hashes, no re-upload
    assert redo["failed"] == 0
    assert len(list(storage.iter_objects(PREFIX))) == objects_before
    assert _read(scratch_database, StockAnnouncement, announcements["old"])["is_cold"] is True

    # -- 6. a tampered object is reported, never silently accepted -------
    storage.put(key, b"tampered body", content_type="text/plain", sha256=str(stored.sha256))

    verified = service.verify(sample=100)

    assert verified["failed"] >= 1
    assert verified["fc_archive_failed_total"] >= 1
    assert any(item["reason"] == "sha256 mismatch" for item in verified["failures"])

    damaged = service.restore(SYMBOL)

    assert damaged["failed"] >= 1
    assert damaged["fc_missing_chunk_total"] >= 1
    assert damaged["restored"] == 1  # the untouched announcement still restores
