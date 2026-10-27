"""Repository behaviour against a migrated PostgreSQL database.

Covers the three write contracts from the schema spec: ODS is append-only, DWD
writes a new version instead of updating history, and repeated ``event_id``
values never create a second row.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from financecoffer.db.models import (
    AnnouncementChunk,
    EtlDlq,
    EtlEventLog,
    StockAgentAnalysisWide,
    StockAnnouncement,
    StockFinancialSnapshot,
)
from financecoffer.db.repositories import (
    AgentAnalysisWideRepository,
    AnnouncementChunkRepository,
    AnnouncementRepository,
    ArchiveJobRepository,
    ConsumerOffsetRepository,
    DlqRepository,
    EventLogRepository,
    FinancialSnapshotRepository,
    HeartbeatRepository,
    KlineRepository,
    NorthboundFlowRepository,
    RawDataRepository,
)
from financecoffer.db.session import Database

NOW = datetime.now(UTC)


@pytest.fixture()
def db(migrated_warehouse: str) -> Iterator[Database]:
    database = Database(migrated_warehouse)
    try:
        yield database
    finally:
        database.dispose()


def snapshot(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "symbol": "FS0001",
        "report_date": date(2026, 9, 30),
        "report_type": "Q3",
        "amount_revenue": Decimal("100.0000"),
        "amount_net_profit": Decimal("-5.5000"),
        "quality_status": "normal",
        "fetch_time": NOW,
        "event_id": "evt_fs_1",
        "provider": "eastmoney",
    }
    values.update(overrides)
    return values


def kline(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "symbol": "K0001",
        "trade_date": date(2026, 10, 20),
        "adjust": "qfq",
        "open": Decimal("10.0000"),
        "high": Decimal("12.0000"),
        "low": Decimal("9.5000"),
        "close": Decimal("11.0000"),
        "volume": Decimal("1000000.0000"),
        "amount": Decimal("11000000.0000"),
        "quality_status": "normal",
        "fetch_time": NOW,
        "event_id": "evt_k_1",
        "provider": "eastmoney",
    }
    values.update(overrides)
    return values


def announcement(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "symbol": "ANN001",
        "ann_date": date(2026, 10, 20),
        "ann_type": "earnings",
        "title": "Q3 earnings",
        "content_hash": "a" * 64,
        "clean_content": "revenue grew",
        "quality_status": "normal",
        "fetch_time": NOW,
        "event_id": "evt_ann_1",
        "provider": "eastmoney",
    }
    values.update(overrides)
    return values


def raw(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event_id": "evt_raw_1",
        "data_type": "financial",
        "symbol": "ODS001",
        "source": "eastmoney",
        "biz_key": "2026Q3",
        "fetch_time": NOW,
        "raw_json": {"n": 1},
    }
    values.update(overrides)
    return values


def test_ods_appends_and_replays_in_order(db: Database) -> None:
    with db.session_scope() as session:
        repository = RawDataRepository(session)
        first = repository.append(**raw())
        repository.append_many(
            [
                raw(
                    event_id="evt_raw_2",
                    fetch_time=NOW + timedelta(minutes=1),
                    raw_json={"n": 2},
                ),
                raw(
                    event_id="evt_raw_3",
                    data_type="kline",
                    fetch_time=NOW + timedelta(minutes=2),
                    raw_json={"n": 3},
                ),
            ]
        )
        assert first.id is not None

    with db.session_scope() as session:
        repository = RawDataRepository(session)
        replayed = repository.iter_range(
            NOW - timedelta(hours=1), NOW + timedelta(hours=1), symbol="ODS001"
        )
        assert [row.event_id for row in replayed] == [
            "evt_raw_1",
            "evt_raw_2",
            "evt_raw_3",
        ]

        klines = repository.iter_range(
            NOW - timedelta(hours=1),
            NOW + timedelta(hours=1),
            data_type="kline",
        )
        assert [row.event_id for row in klines] == ["evt_raw_3"]
        assert repository.by_event_id("evt_raw_2")[0].raw_json == {"n": 2}


def test_versioned_fact_appends_and_never_updates_history(db: Database) -> None:
    with db.session_scope() as session:
        first = FinancialSnapshotRepository(session).insert_version(**snapshot())
        assert first is not None
        assert first.version == 1

    with db.session_scope() as session:
        repository = FinancialSnapshotRepository(session)
        second = repository.insert_version(
            **snapshot(event_id="evt_fs_2", amount_revenue=Decimal("200.0000"))
        )
        assert second is not None
        assert second.version == 2

        replay = repository.insert_version(
            **snapshot(event_id="evt_fs_2", amount_revenue=Decimal("999.0000"))
        )
        assert replay is None

        latest = repository.latest(
            symbol="FS0001", report_date=date(2026, 9, 30), report_type="Q3"
        )
        assert latest is not None
        assert latest.version == 2
        assert latest.amount_revenue == Decimal("200.0000")

        original = repository.get("FS0001", date(2026, 9, 30), "Q3", 1)
        assert original is not None
        assert original.amount_revenue == Decimal("100.0000")

        total = session.scalar(
            select(func.count())
            .select_from(StockFinancialSnapshot)
            .where(StockFinancialSnapshot.symbol == "FS0001")
        )
        assert total == 2


def test_kline_versions_are_scoped_to_the_adjust_factor(db: Database) -> None:
    with db.session_scope() as session:
        repository = KlineRepository(session)
        assert repository.insert_version(**kline()).version == 1  # type: ignore[union-attr]
        assert (
            repository.insert_version(
                **kline(adjust="hfq", event_id="evt_k_2")
            ).version  # type: ignore[union-attr]
            == 1
        )
        assert (
            repository.insert_version(
                **kline(event_id="evt_k_3", close=Decimal("11.5000"))
            ).version  # type: ignore[union-attr]
            == 2
        )

    with db.session_scope() as session:
        repository = KlineRepository(session)
        latest = repository.latest(
            symbol="K0001", trade_date=date(2026, 10, 20), adjust="qfq"
        )
        assert latest is not None
        assert latest.version == 2
        assert latest.close == Decimal("11.5000")


def test_database_rejects_history_updates(db: Database) -> None:
    with db.session_scope() as session:
        FinancialSnapshotRepository(session).insert_version(
            **snapshot(symbol="TRG001", event_id="evt_trg_1")
        )

    with pytest.raises(IntegrityError, match="append-only"):
        with db.session_scope() as session:
            session.execute(
                update(StockFinancialSnapshot)
                .where(StockFinancialSnapshot.symbol == "TRG001")
                .values(amount_revenue=Decimal("1.0000"))
                .execution_options(synchronize_session=False)
            )

    with db.session_scope() as session:
        row = FinancialSnapshotRepository(session).get(
            "TRG001", date(2026, 9, 30), "Q3", 1
        )
        assert row is not None
        assert row.amount_revenue == Decimal("100.0000")


def test_announcement_dedupes_on_event_id_and_content_hash(db: Database) -> None:
    with db.session_scope() as session:
        first = AnnouncementRepository(session).insert_once(**announcement())
        assert first is not None
        first_id = first.id

    with db.session_scope() as session:
        repository = AnnouncementRepository(session)
        assert repository.insert_once(**announcement()) is None
        assert repository.insert_once(**announcement(event_id="evt_ann_2")) is None

        second = repository.insert_once(
            **announcement(
                event_id="evt_ann_3",
                content_hash="b" * 64,
                title="a different filing",
            )
        )
        assert second is not None
        assert second.id != first_id

        total = session.scalar(
            select(func.count())
            .select_from(StockAnnouncement)
            .where(StockAnnouncement.symbol == "ANN001")
        )
        assert total == 2


def test_northbound_repeat_is_a_no_op(db: Database) -> None:
    values: dict[str, Any] = {
        "trade_date": date(2026, 10, 20),
        "channel": "sh_connect",
        "market": "CN",
        "net_buy_amount": Decimal("123.4500"),
        "quality_status": "normal",
        "fetch_time": NOW,
        "event_id": "evt_nb_1",
        "provider": "eastmoney",
    }
    with db.session_scope() as session:
        repository = NorthboundFlowRepository(session)
        assert repository.insert_once(**values) is not None
        assert repository.insert_once(**values) is None

    with db.session_scope() as session:
        total = session.scalar(select(func.count()).select_from(NorthboundFlowRepository.model))
        assert total == 1


def test_event_log_claims_once_and_tracks_status(db: Database) -> None:
    with db.session_scope() as session:
        repository = EventLogRepository(session)
        claimed = repository.claim(
            "evt_log_1",
            provider="eastmoney",
            data_type="kline",
            symbol="LOG001",
            biz_key="2026-10-20",
        )
        assert claimed is not None
        assert claimed.status == "received"
        assert claimed.attempts == 0

        assert (
            repository.claim(
                "evt_log_1",
                provider="eastmoney",
                data_type="kline",
                symbol="LOG001",
                biz_key="2026-10-20",
            )
            is None
        )

        started = repository.begin("evt_log_1")
        assert started is not None
        assert started.status == "processing"
        assert started.attempts == 1

        done = repository.succeed("evt_log_1")
        assert done is not None
        assert done.status == "done"
        assert done.processed_at is not None
        assert done.error is None

        assert repository.claim(
            "evt_log_2",
            provider="eastmoney",
            data_type="kline",
            symbol="LOG002",
            biz_key="2026-10-20",
        )
        failed = repository.fail("evt_log_2", "parse error")
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error == "parse error"

        assert repository.fail("evt_log_missing", "boom") is None

    with db.session_scope() as session:
        total = session.scalar(select(func.count()).select_from(EtlEventLog))
        assert total == 2


def test_event_identity_blocks_a_second_event_with_a_null_symbol(db: Database) -> None:
    with db.session_scope() as session:
        repository = EventLogRepository(session)
        assert (
            repository.claim(
                "evt_ns_1",
                provider="eastmoney",
                data_type="northbound",
                biz_key="2026-10-20",
            )
            is not None
        )
        assert (
            repository.claim(
                "evt_ns_2",
                provider="eastmoney",
                data_type="northbound",
                biz_key="2026-10-20",
            )
            is None
        )

        total = session.scalar(
            select(func.count())
            .select_from(EtlEventLog)
            .where(EtlEventLog.data_type == "northbound")
        )
        assert total == 1


def test_heartbeat_upsert_keeps_one_row(db: Database) -> None:
    with db.session_scope() as session:
        repository = HeartbeatRepository(session)
        first = repository.record(
            "eastmoney", last_seen=NOW, status="ok", items=5, run_id="run-1"
        )
        assert first.items == 5

    with db.session_scope() as session:
        repository = HeartbeatRepository(session)
        second = repository.record(
            "eastmoney", last_seen=NOW + timedelta(minutes=5), status="partial", items=2
        )
        assert second.items == 2
        assert second.status == "partial"

        total = session.scalar(
            select(func.count())
            .select_from(repository.model)
            .where(repository.model.source == "eastmoney")
        )
        assert total == 1


def test_consumer_offset_advances(db: Database) -> None:
    with db.session_scope() as session:
        repository = ConsumerOffsetRepository(session)
        repository.save(
            consumer_group="etl",
            stream="finance.events",
            last_event_id="evt_1",
            last_event_time=NOW,
        )

    with db.session_scope() as session:
        repository = ConsumerOffsetRepository(session)
        repository.save(
            consumer_group="etl",
            stream="finance.events",
            last_event_id="evt_2",
            last_event_time=NOW + timedelta(seconds=1),
        )
        offset = repository.offset("etl", "finance.events")
        assert offset is not None
        assert offset.last_event_id == "evt_2"
        assert offset.shard == "0"


def test_dlq_lifecycle(db: Database) -> None:
    with db.session_scope() as session:
        repository = DlqRepository(session)
        entry = repository.enqueue(
            stream="finance.events",
            reason="bad_payload",
            raw_event={"unexpected": True},
            event_id="evt_dlq_1",
            entry_id="1-0",
        )
        assert entry.status == "pending"
        assert entry.retry_count == 0
        dlq_id = entry.id

    with db.session_scope() as session:
        repository = DlqRepository(session)
        assert [item.id for item in repository.pending()] == [dlq_id]

        retried = repository.mark_retried(dlq_id)
        assert retried is not None
        assert retried.status == "retried"
        assert retried.retry_count == 1
        assert repository.pending() == []

    with db.session_scope() as session:
        assert DlqRepository(session).purge(dlq_id) is not None
        row = session.get(EtlDlq, dlq_id)
        assert row is not None
        assert row.status == "purged"


def test_archive_job_lifecycle(db: Database) -> None:
    with db.session_scope() as session:
        repository = ArchiveJobRepository(session)
        job = repository.create(
            table_name="dwd.stock_kline",
            object_key="archive/kline/2026-10.parquet",
            symbol="K0001",
        )
        job_id = job.id

        running = repository.mark_running(job_id)
        assert running is not None
        assert running.status == "running"
        assert running.attempts == 1
        assert running.started_at is not None

        done = repository.mark_done(job_id, rows_archived=10, bytes_archived=2048)
        assert done is not None
        assert done.status == "done"
        assert done.rows_archived == 10
        assert done.finished_at is not None

        failing = repository.create(
            table_name="dwd.stock_announcement", object_key="archive/ann.parquet"
        )
        failed = repository.mark_failed(failing.id, "upload failed")
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error == "upload failed"


def test_dws_upsert_keeps_one_row_per_symbol(db: Database) -> None:
    with db.session_scope() as session:
        repository = AgentAnalysisWideRepository(session)
        first = repository.upsert(
            symbol="DWS001",
            as_of=NOW,
            metrics_json={"pe": 12.5},
            risk_summary={"level": "low"},
            latest_announcements=[],
            fiscal_years=5,
            source_version="v1",
        )
        assert first.metrics_json == {"pe": 12.5}

    with db.session_scope() as session:
        repository = AgentAnalysisWideRepository(session)
        second = repository.upsert(
            symbol="DWS001",
            as_of=NOW + timedelta(minutes=1),
            metrics_json={"pe": 13.5},
            source_version="v2",
        )
        assert second.metrics_json == {"pe": 13.5}
        assert second.source_version == "v2"
        assert second.risk_summary == {"level": "low"}

        total = session.scalar(
            select(func.count())
            .select_from(StockAgentAnalysisWide)
            .where(StockAgentAnalysisWide.symbol == "DWS001")
        )
        assert total == 1


def test_vector_chunks_round_trip_and_dedupe(db: Database) -> None:
    with db.session_scope() as session:
        stored = AnnouncementRepository(session).insert_once(
            **announcement(
                symbol="CHK001",
                content_hash="c" * 64,
                event_id="evt_chk_1",
            )
        )
        assert stored is not None
        announcement_id = stored.id

    embedding = [float(value % 7) / 2 for value in range(1024)]
    chunk_values: dict[str, Any] = {
        "symbol": "CHK001",
        "ann_date": date(2026, 10, 20),
        "ann_type": "earnings",
        "chunk_index": 0,
        "chunk_text": "revenue grew",
        "token_count": 2,
        "embedding": embedding,
        "dwd_ref_id": announcement_id,
    }

    with db.session_scope() as session:
        chunk = AnnouncementChunkRepository(session).append(**chunk_values)
        assert chunk.id is not None
        chunk_id = chunk.id

    with db.session_scope() as session:
        loaded = session.get(AnnouncementChunk, chunk_id)
        assert loaded is not None
        assert loaded.embedding == embedding
        assert loaded.embedding_model == "BAAI/bge-m3"
        assert (
            AnnouncementChunkRepository(session).insert_once(**chunk_values) is None
        )
