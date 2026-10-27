"""Announcement chunking -> real embeddings -> pgvector -> filter-first search.

The pipeline runs against the local bge-m3 endpoint (``FC_EMBEDDING_URL``) and
a scratch PostgreSQL database with pgvector. Every test skips when either
dependency is unreachable, so the suite stays green on a bare machine.

Search must never scan the whole chunk table: ``symbol`` is mandatory and the
candidate set is cut with the ``(symbol, ann_date)`` B-Tree before the cosine
distance is computed (``docs/specs/financecoffer.md`` section 3.5). The
``EXPLAIN`` test asserts that shape on the exact filter/order query instead of
trusting the wording of the SQL.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, text

from financecoffer.config import load_config
from financecoffer.core.config import Config
from financecoffer.core.errors import InvalidArgumentError
from financecoffer.db.models.dwd import StockAnnouncement
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.models.ops import EtlEventLog
from financecoffer.db.session import Database
from financecoffer.etl.processor import process_payload
from financecoffer.events import compute_event_id
from financecoffer.vector.embeddings import EmbeddingClient
from financecoffer.vector.service import INDEX_NAME, VectorService

REPO_ROOT = Path(__file__).resolve().parents[2]

PROVIDER = "t016-vector"
SYMBOL = "920017"
OTHER_SYMBOL = "920018"
CRAWL_TIME = "2026-10-27T02:00:00Z"
DIM = 1024

Q3_TEXT = (
    "公司发布2026年第三季度报告。报告期内营业收入同比增长12%，"
    "归属于上市公司股东的净利润创历史新高，主要原因是高端产品销量提升。"
)
BUYBACK_TEXT = (
    "公司董事会审议通过股份回购方案，拟使用自有资金回购公司股份，"
    "回购金额不超过人民币5亿元，用于员工持股计划。"
)
INQUIRY_TEXT = (
    "公司收到监管问询函，要求说明关联交易的定价依据与决策程序，"
    "公司将在规定期限内书面回复。"
)


@pytest.fixture(scope="module")
def config(migrated_warehouse: str) -> Config:
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        pytest.skip("repository .env is missing; embedding endpoint is unknown")

    loaded = load_config(
        env={},
        env_file=env_file,
        overrides={"db_url": migrated_warehouse},
    )
    probe = EmbeddingClient(loaded, timeout=5, retries=0, batch_size=1)
    try:
        outcome = probe.embed(["health check"])
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"embedding client failed: {exc}")
    if outcome.failures:
        detail = outcome.errors[0] if outcome.errors else "no vector returned"
        pytest.skip(f"embedding service is unreachable at {loaded.embedding_url}: {detail}")
    return loaded


@pytest.fixture()
def db(config: Config, migrated_warehouse: str) -> Iterator[Database]:
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
def service(config: Config, db: Database) -> Iterator[VectorService]:
    client = VectorService(config=config, dsn=db.dsn)
    try:
        yield client
    finally:
        client.close()


def _clean(database: Database) -> None:
    with database.session_scope() as session:
        session.execute(
            text(
                "DELETE FROM vector.announcement_chunk "
                "WHERE symbol IN (:symbol, :other)"
            ),
            {"symbol": SYMBOL, "other": OTHER_SYMBOL},
        )
        session.execute(
            delete(StockAnnouncement).where(StockAnnouncement.provider == PROVIDER)
        )
        session.execute(
            delete(StockRawData).where(StockRawData.symbol.in_([SYMBOL, OTHER_SYMBOL]))
        )
        session.execute(delete(EtlEventLog).where(EtlEventLog.provider == PROVIDER))


def event(
    *,
    symbol: str,
    biz_key: str,
    title: str,
    body: str,
    publish_date: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider=PROVIDER,
            data_type="announcement",
            symbol=symbol,
            biz_key=biz_key,
        ),
        "biz_key": biz_key,
        "event_time": "2026-10-27T10:00:00+08:00",
        "data_type": "announcement",
        "symbol": symbol,
        "market": "CN",
        "source_info": {"provider": PROVIDER, "crawl_time": CRAWL_TIME},
        "content": {
            "quality_metrics": {"quality_status": "normal", "confidence": "high"},
            "raw_html": f"<p>{body}</p>",
            "clean_content": body,
            "structured_fields": {
                "title": title,
                "ann_type": "periodic",
                "publish_date": publish_date,
            },
        },
        "trace_id": "trace-t016-vector",
    }


def seed(database: Database) -> None:
    """Land three same-symbol announcements plus one decoy for another symbol."""

    bodies = [
        event(
            symbol=SYMBOL,
            biz_key="2026Q3-report",
            title="2026年第三季度报告",
            body=Q3_TEXT,
            publish_date="2026-10-20",
        ),
        event(
            symbol=SYMBOL,
            biz_key="2026-buyback",
            title="股份回购方案",
            body=BUYBACK_TEXT,
            publish_date="2026-09-15",
        ),
        event(
            symbol=SYMBOL,
            biz_key="2026-inquiry",
            title="监管问询函",
            body=INQUIRY_TEXT,
            publish_date="2026-08-01",
        ),
        event(
            symbol=OTHER_SYMBOL,
            biz_key="2026Q3-report",
            title="第三季度报告（另一家公司）",
            body=Q3_TEXT.replace("高端产品", "新能源产品"),
            publish_date="2026-10-21",
        ),
    ]
    with database.session_scope() as session:
        for body in bodies:
            result = process_payload(
                session,
                payload=body,
                raw_text=json.dumps(body, ensure_ascii=False),
                compliance_enabled=False,
            )
            assert result.status == "written", result.as_dict()


def chunk_rows(database: Database, symbol: str) -> list[dict[str, Any]]:
    with database.session() as session:
        rows = session.execute(
            text(
                "SELECT dwd_ref_id, chunk_index, chunk_text, "
                "token_count, embedding_model, vector_dims(embedding) AS dims, "
                "embedding::text AS vector "
                "FROM vector.announcement_chunk WHERE symbol = :symbol "
                "ORDER BY dwd_ref_id, chunk_index"
            ),
            {"symbol": symbol},
        ).mappings().all()
    return [dict(row) for row in rows]


def _parse_vector(literal: str) -> list[float]:
    return [float(part) for part in json.loads(literal)]


def test_chunks_are_embedded_stored_and_retrieved(service: VectorService, db: Database) -> None:
    seed(db)

    preview = service.chunk_preview(SYMBOL)
    assert preview["symbol"] == SYMBOL
    assert preview["announcements"] == 3
    assert preview["chunks"] >= 3
    assert chunk_rows(db, SYMBOL) == [], "chunk_preview must stay read-only"

    report = service.embed(SYMBOL, limit=10, pending=True)
    assert report.failed == 0, report.payload()
    assert report.announcements == 3
    assert report.chunks == preview["chunks"]
    assert report.inserted == report.chunks
    assert report.model == service.config.embedding_model

    rows = chunk_rows(db, SYMBOL)
    assert len(rows) == report.chunks
    assert all(row["dims"] == DIM for row in rows)
    assert all(row["embedding_model"] == service.config.embedding_model for row in rows)
    for row in rows:
        vector = _parse_vector(row["vector"])
        assert len(vector) == DIM
        norm = sum(component * component for component in vector) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-3)
        assert row["chunk_text"].strip()

    by_ref: dict[int, list[int]] = {}
    for row in rows:
        by_ref.setdefault(row["dwd_ref_id"], []).append(row["chunk_index"])
    for ref, indices in by_ref.items():
        assert indices == list(range(len(indices))), (ref, indices)

    # Pending selection is empty on the second pass: no duplicated positions.
    again = service.embed(SYMBOL, limit=10, pending=True)
    assert again.announcements == 0
    assert chunk_rows(db, SYMBOL) == rows

    decoy = service.embed(OTHER_SYMBOL, limit=10, pending=True)
    assert decoy.inserted == decoy.chunks > 0

    result = service.search(SYMBOL, "第三季度营业收入增长情况", top_k=5)
    data = result.data
    assert data["symbol"] == SYMBOL
    assert data["year_range"] == [2022, 2026]
    assert data["count"] == len(data["results"]) > 0
    assert {item["symbol"] for item in data["results"]} == {SYMBOL}
    scores = [item["score"] for item in data["results"]]
    assert scores == sorted(scores, reverse=True)
    assert data["results"][0]["title"] == "2026年第三季度报告"
    assert result.source == "vector"

    stats = service.stats(SYMBOL)
    assert stats["chunks"] == len(rows)
    assert stats["announcements"] == 3
    assert stats["pending_announcements"] == 0
    assert stats["embedding_dim"] == DIM


def test_search_rejects_a_missing_symbol_before_connecting(config: Config) -> None:
    offline = VectorService(
        config=config,
        dsn="postgresql://nobody:nobody@127.0.0.1:1/never",
        timeout=1,
    )
    try:
        for symbol in (None, "", "   "):
            with pytest.raises(InvalidArgumentError) as missing:
                offline.search(symbol, "任何问题")
            assert missing.value.details["field"] == "symbol"

        with pytest.raises(InvalidArgumentError) as blank:
            offline.search(SYMBOL, "  ")
        assert blank.value.details["field"] == "query"

        with pytest.raises(InvalidArgumentError) as injected:
            offline.search("'; DROP TABLE vector.announcement_chunk; --", "x")
        assert injected.value.details["field"] == "symbol"
    finally:
        offline.close()


def _walk(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _walk(child)


def test_explain_filters_with_the_btree_index_before_ordering(
    service: VectorService, db: Database
) -> None:
    seed(db)
    report = service.embed(SYMBOL, limit=10, pending=True)
    assert report.failed == 0

    status = service.index_status()
    assert status["name"] == INDEX_NAME
    assert status["exists"] is True
    assert status["method"] == "hnsw"
    assert "vector_cosine_ops" in status["definition"]

    query_vector = service.embedding.embed_query("第三季度营业收入增长情况")
    literal = "[" + ",".join(f"{value:.9f}" for value in query_vector) + "]"
    sql = (
        "SELECT c.id FROM vector.announcement_chunk AS c "
        "WHERE c.symbol = :symbol AND c.ann_date >= :start AND c.ann_date <= :end "
        "ORDER BY c.embedding <=> CAST(:vector AS vector) LIMIT :limit"
    )
    params = {
        "symbol": SYMBOL,
        "start": "2022-01-01",
        "end": "2026-12-31",
        "vector": literal,
        "limit": 10,
    }

    with service.database.session() as session:
        # The table is tiny in a scratch database, so a sequential scan is the
        # normal choice; disabling it exposes the index the production plan
        # must use once the chunk table is large.
        session.execute(text("SET LOCAL enable_seqscan = off"))
        session.execute(text("SET LOCAL enable_indexscan = off"))
        raw = session.execute(
            text("EXPLAIN (FORMAT JSON) " + sql), params
        ).scalar_one()

    plan = raw[0]["Plan"] if isinstance(raw, list) else json.loads(raw)[0]["Plan"]
    nodes = list(_walk(plan))

    sorts = [node for node in nodes if node.get("Node Type") == "Sort"]
    assert sorts, "the distance ordering must stay an explicit sort over the candidates"
    sort_keys = [key for node in sorts for key in node.get("Sort Key", [])]
    assert any("<=>" in key for key in sort_keys), sort_keys

    filters = [node for node in nodes if node.get("Index Name") == "idx_ann_chunk_filter"]
    assert filters, [node.get("Node Type") for node in nodes]
    conditions = " ".join(
        str(part)
        for node in filters
        for part in (node.get("Index Cond"), node.get("Filter"))
        if part
    )
    assert "symbol" in conditions and "ann_date" in conditions, conditions

    sort_subtree = list(_walk(sorts[0]))
    assert any(
        node.get("Index Name") == "idx_ann_chunk_filter" for node in sort_subtree
    ), "the B-Tree filter must feed the sort, not run after it"
    assert not any(
        node.get("Node Type") == "Seq Scan"
        and node.get("Relation Name") == "announcement_chunk"
        for node in nodes
    )
