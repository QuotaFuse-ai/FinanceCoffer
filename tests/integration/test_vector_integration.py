"""Vector write path against a migrated PostgreSQL and the local bge-m3 server.

The scratch database is created by ``migrated_warehouse``; the embedding tests
skip when ``FC_EMBEDDING_URL`` (default ``127.0.0.1:8001``) is unreachable, so
the suite stays green without the model server. Symbols use the ``82`` test
range and rows are removed afterwards; announcement rows are never deleted by
the pipeline itself, only by this fixture.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import delete, select, text

from financecoffer.core.config import Config
from financecoffer.db.models.dwd import StockAnnouncement
from financecoffer.db.models.vector import AnnouncementChunk
from financecoffer.db.repositories.dwd import AnnouncementRepository
from financecoffer.db.session import Database
from financecoffer.vector.chunking import chunk_text
from financecoffer.vector.service import VectorService

EMBEDDING_URL = os.environ.get(
    "FC_EMBEDDING_URL", "http://127.0.0.1:8001/v1/embeddings"
)
PROVIDER = "t009-vector"
SHORT_SYMBOL = "820001"
LONG_SYMBOL = "820002"
SHORT_TEXT = "Revenue grew twelve percent year over year on strong demand."
LONG_TEXT = "The company reported revenue growth and discussed risk factors. " * 120


@pytest.fixture(scope="module")
def live_embedding() -> str:
    """Skip embedding-dependent tests when the model server is unavailable."""

    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                EMBEDDING_URL,
                json={"model": "BAAI/bge-m3", "input": ["ping"]},
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"embedding service is not reachable at {EMBEDDING_URL}: {exc}")
    vectors = body.get("data") if isinstance(body, dict) else None
    if not isinstance(vectors, list) or len(vectors) != 1:
        pytest.skip("embedding service returned an unexpected payload")
    return EMBEDDING_URL


@pytest.fixture(scope="module")
def warehouse(
    migrated_warehouse: str,
) -> Iterator[tuple[VectorService, dict[str, int], dict[str, str]]]:
    database = Database(migrated_warehouse)
    identifiers: dict[str, int] = {}
    contents = {"short": SHORT_TEXT, "long": LONG_TEXT}
    try:
        with database.session_scope() as session:
            repository = AnnouncementRepository(session)
            for key, symbol, content, ann_date in (
                ("short", SHORT_SYMBOL, SHORT_TEXT, date(2026, 10, 1)),
                ("long", LONG_SYMBOL, LONG_TEXT, date(2026, 10, 2)),
            ):
                row = repository.insert_once(
                    symbol=symbol,
                    ann_date=ann_date,
                    ann_type="periodic",
                    title=f"T-009 {key} announcement",
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    clean_content=content,
                    quality_status="normal",
                    confidence="high",
                    fetch_time=datetime(2026, 10, 20, 2, 0, tzinfo=UTC),
                    event_id=f"t009-{key}",
                    provider=PROVIDER,
                )
                assert row is not None
                identifiers[key] = row.id

        service = VectorService(
            config=Config(db_url=migrated_warehouse, embedding_url=EMBEDDING_URL),
            dsn=migrated_warehouse,
        )
        try:
            yield service, identifiers, contents
        finally:
            service.close()
    finally:
        with database.session_scope() as session:
            session.execute(delete(StockAnnouncement).where(StockAnnouncement.provider == PROVIDER))
        database.dispose()


def _stored(service: VectorService, dwd_ref_id: int) -> list[AnnouncementChunk]:
    with service.database.session() as session:
        statement = (
            select(AnnouncementChunk)
            .where(AnnouncementChunk.dwd_ref_id == dwd_ref_id)
            .order_by(AnnouncementChunk.chunk_index)
        )
        return list(session.scalars(statement))


def test_chunk_preview_splits_long_text_and_keeps_the_tail(
    warehouse: tuple[VectorService, dict[str, int], dict[str, str]],
) -> None:
    service, identifiers, contents = warehouse

    short = service.chunk_preview(SHORT_SYMBOL, limit=10)
    long = service.chunk_preview(LONG_SYMBOL, limit=10)

    assert short["announcements"] == 1
    assert short["items"][0]["chunks"] == 1
    assert short["items"][0]["dwd_ref_id"] == identifiers["short"]

    assert long["announcements"] == 1
    assert long["items"][0]["chunks"] > 1

    windows = chunk_text(
        contents["long"],
        tokenizer=service.tokenizer,
        chunk_size=512,
        overlap=50,
    )
    assert len(windows) == long["items"][0]["chunks"]
    assert windows[0].token_count == 512
    assert windows[-1].end_char == len(contents["long"])
    for previous, current in zip(windows[:-1], windows[1:], strict=True):
        assert previous.end_token - current.start_token == 50
    assert all(window.token_count > 0 for window in windows)


def test_embed_is_idempotent_and_search_stays_inside_one_symbol(
    warehouse: tuple[VectorService, dict[str, int], dict[str, str]],
    live_embedding: str,
) -> None:
    service, identifiers, contents = warehouse

    expected_short = len(
        chunk_text(contents["short"], tokenizer=service.tokenizer, chunk_size=512, overlap=50)
    )
    expected_long = len(
        chunk_text(contents["long"], tokenizer=service.tokenizer, chunk_size=512, overlap=50)
    )

    first_short = service.embed(SHORT_SYMBOL, limit=10)
    first_long = service.embed(LONG_SYMBOL, limit=10)
    assert (first_short.failed, first_short.missing) == (0, 0)
    assert (first_long.failed, first_long.missing) == (0, 0)
    assert first_short.inserted == expected_short
    assert first_long.inserted == expected_long
    assert first_short.updated == 0
    assert first_long.updated == 0

    short_rows = _stored(service, identifiers["short"])
    long_rows = _stored(service, identifiers["long"])
    assert len(short_rows) == expected_short
    assert len(long_rows) == expected_long
    assert [row.chunk_index for row in short_rows] == list(range(len(short_rows)))
    assert [row.chunk_index for row in long_rows] == list(range(len(long_rows)))
    assert all(row.token_count and row.token_count > 0 for row in short_rows + long_rows)
    assert all(row.embedding_model == "BAAI/bge-m3" for row in short_rows + long_rows)
    assert all(len(row.embedding) == 1024 for row in short_rows + long_rows)

    # Pending selection finds nothing new; a forced replay updates in place.
    pending = service.embed(SHORT_SYMBOL)
    assert pending.announcements == 0
    assert pending.written == 0
    replay_short = service.embed(SHORT_SYMBOL, pending=False)
    replay_long = service.embed(LONG_SYMBOL, pending=False)
    assert replay_short.inserted == 0
    assert replay_long.inserted == 0
    assert replay_short.updated == expected_short
    assert replay_long.updated == expected_long
    assert len(_stored(service, identifiers["short"])) == expected_short
    assert len(_stored(service, identifiers["long"])) == expected_long

    hits = service.search(SHORT_SYMBOL, "revenue growth", year_range="2026-2026", top_k=5)
    results = hits.data["results"]
    assert results
    assert {hit["symbol"] for hit in results} == {SHORT_SYMBOL}
    assert all(hit["chunk_text"] for hit in results)

    long_hits = service.search(LONG_SYMBOL, "risk factors", year_range="2026-2026", top_k=5)
    long_results = long_hits.data["results"]
    assert long_results
    assert {hit["symbol"] for hit in long_results} == {LONG_SYMBOL}


def test_hnsw_index_exists_and_the_plan_filters_before_ordering(
    warehouse: tuple[VectorService, dict[str, int], dict[str, str]],
) -> None:
    service, _, _ = warehouse

    status = service.index_status()
    assert status["exists"] is True
    assert status["method"] == "hnsw"
    assert "vector_cosine_ops" in status["definition"]

    probe = "[" + ",".join(["0.1"] * 1024) + "]"
    with service.database.session() as session:
        session.execute(text("SET LOCAL enable_seqscan = off"))
        plan_rows = session.execute(
            text(
                "EXPLAIN (COSTS OFF) "
                "SELECT c.id FROM vector.announcement_chunk AS c "
                "WHERE c.symbol = :symbol AND c.ann_date >= :start AND c.ann_date <= :end "
                "ORDER BY c.embedding <=> CAST(:probe AS vector) LIMIT 5"
            ),
            {
                "symbol": SHORT_SYMBOL,
                "start": date(2026, 1, 1),
                "end": date(2026, 12, 31),
                "probe": probe,
            },
        )
        plan = "\n".join(str(row[0]) for row in plan_rows)

    assert "Seq Scan on announcement_chunk" not in plan
    assert "announcement_chunk" in plan
    assert "<=>" in plan
    assert "idx_ann_chunk_filter" in plan or "idx_ann_chunk_embedding" in plan
