"""Vector layer service: chunk, embed, search, index and statistics.

The pipeline is deliberately one pass per announcement: tokenize the DWD
announcement, embed every chunk in bounded batches, then upsert the rows. The
schema keeps ``embedding NOT NULL``, so there is no "pending chunk" state - a
row only exists once its vector does. ``vector chunk`` is therefore a read-only
plan that reports what ``vector embed`` would write.

Search stays filter-first by delegating to :class:`QueryService`, which applies
``symbol`` and ``ann_date`` before the cosine operator. A missing symbol is
rejected here, before any connection, so a caller can never trigger a full-table
vector scan (``docs/specs/cli.md`` section 3.10).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from financecoffer.core.config import Config
from financecoffer.core.errors import InvalidArgumentError
from financecoffer.db.session import Database
from financecoffer.query.service import MAX_TOP_K, QueryResult, QueryService, normalize_symbol
from financecoffer.vector.chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    ApproximateTokenizer,
    Chunk,
    HuggingFaceTokenizer,
    Tokenizer,
    chunk_text,
    default_tokenizer,
    validate_chunk_settings,
)
from financecoffer.vector.embeddings import (
    DEFAULT_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingOutcome,
)
from financecoffer.vector.metrics import VECTOR_METRICS
from financecoffer.vector.writer import ChunkWriter, EmbeddedChunk, UpsertCounts

INDEX_NAME = "idx_ann_chunk_embedding"
MAX_LIMIT = 1000

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_ann_chunk_embedding "
    "ON vector.announcement_chunk USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)"
)
_DROP_INDEX = "DROP INDEX IF EXISTS vector.idx_ann_chunk_embedding"
_REINDEX_INDEX = "REINDEX INDEX vector.idx_ann_chunk_embedding"
_INDEX_STATUS = (
    "SELECT c.relname AS name, am.amname AS method, "
    "pg_get_indexdef(c.oid) AS definition, pg_relation_size(c.oid) AS size_bytes "
    "FROM pg_class AS c "
    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
    "JOIN pg_am AS am ON am.oid = c.relam "
    "WHERE n.nspname = 'vector' AND c.relname = :name"
)

_STATS = (
    "SELECT count(*) AS chunks, "
    "count(DISTINCT dwd_ref_id) AS announcements, "
    "count(DISTINCT symbol) AS symbols, "
    "coalesce(sum(token_count), 0) AS tokens, "
    "min(embedded_at) AS first_embedded_at, "
    "max(embedded_at) AS last_embedded_at, "
    "string_agg(DISTINCT embedding_model, ',') AS models "
    "FROM vector.announcement_chunk"
)


@dataclass(frozen=True)
class AnnouncementSource:
    """The DWD columns the pipeline needs, copied out of the detail table."""

    dwd_ref_id: int
    symbol: str
    ann_date: date
    ann_type: str | None
    title: str | None
    content: str


@dataclass(frozen=True)
class AnnouncementResult:
    """Outcome of one announcement; ``failed`` counts chunks without a vector."""

    dwd_ref_id: int
    symbol: str
    chunks: int = 0
    written: int = 0
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    failed: int = 0
    error: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "dwd_ref_id": self.dwd_ref_id,
            "symbol": self.symbol,
            "chunks": self.chunks,
            "written": self.written,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "failed": self.failed,
            "error": self.error,
        }


@dataclass(frozen=True)
class EmbedReport:
    """Aggregate of one ``vector embed`` run."""

    model: str
    announcements: int = 0
    skipped: int = 0
    failed: int = 0
    chunks: int = 0
    written: int = 0
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    missing: int = 0
    results: tuple[AnnouncementResult, ...] = field(default_factory=tuple)

    def payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "announcements": self.announcements,
            "skipped": self.skipped,
            "failed": self.failed,
            "chunks": self.chunks,
            "written": self.written,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "missing_chunks": self.missing,
            "results": [result.payload() for result in self.results],
        }


def _bounded(value: int, *, field_name: str, low: int, high: int) -> int:
    if value < low or value > high:
        raise InvalidArgumentError(
            f"{field_name} must be between {low} and {high}",
            details={"field": field_name, "value": value},
        )
    return value


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


class VectorService:
    """Chunk, embed and search announcements; owns one lazy database engine."""

    def __init__(
        self,
        *,
        config: Config,
        dsn: str | None = None,
        timeout: int = 30,
        retries: int = 2,
        batch_size: int = DEFAULT_BATCH_SIZE,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_OVERLAP,
        tokenizer: Tokenizer | None = None,
        embedding_client: EmbeddingClient | None = None,
        writer_factory: Callable[[Session], ChunkWriter] = ChunkWriter,
        query_service: QueryService | None = None,
    ) -> None:
        validate_chunk_settings(chunk_size, overlap)
        self.config = config
        self.dsn = dsn or config.db_url
        self.timeout = timeout
        self.retries = retries
        self.batch_size = batch_size
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.tokenizer = tokenizer or default_tokenizer()
        self._embedding_client = embedding_client
        self._writer_factory = writer_factory
        self._query_service = query_service
        self._database: Database | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def database(self) -> Database:
        if self._database is None:
            self._database = Database(self.dsn)
        return self._database

    @property
    def embedding(self) -> EmbeddingClient:
        if self._embedding_client is None:
            self._embedding_client = EmbeddingClient(
                self.config,
                timeout=self.timeout,
                batch_size=self.batch_size,
                retries=self.retries,
            )
        return self._embedding_client

    @property
    def query(self) -> QueryService:
        if self._query_service is None:
            self._query_service = QueryService(
                config=self.config,
                dsn=self.dsn,
                timeout=self.timeout,
            )
        return self._query_service

    def close(self) -> None:
        if self._database is not None:
            self._database.dispose()
            self._database = None

    def __enter__(self) -> VectorService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def tokenizer_name(self) -> str:
        if isinstance(self.tokenizer, HuggingFaceTokenizer):
            return "bge-m3"
        if isinstance(self.tokenizer, ApproximateTokenizer):
            return "approximate"
        return type(self.tokenizer).__name__

    # -- chunking -----------------------------------------------------------

    def chunk_preview(
        self,
        symbol: str | None = None,
        *,
        limit: int = 100,
        chunk_size: int | None = None,
        overlap: int | None = None,
    ) -> dict[str, Any]:
        """Report the chunks ``embed`` would produce; never writes rows."""

        canonical = self._optional_symbol(symbol)
        bounded_limit = _bounded(limit, field_name="limit", low=1, high=MAX_LIMIT)
        size, window = self._settings(chunk_size, overlap)
        sources = self._sources(canonical, limit=bounded_limit, pending=False)

        items: list[dict[str, Any]] = []
        total_chunks = 0
        total_tokens = 0
        for source in sources:
            chunks = self._chunks(source, size, window)
            total_chunks += len(chunks)
            tokens = sum(chunk.token_count for chunk in chunks)
            total_tokens += tokens
            items.append(
                {
                    "dwd_ref_id": source.dwd_ref_id,
                    "symbol": source.symbol,
                    "ann_date": _iso(source.ann_date),
                    "title": source.title,
                    "chunks": len(chunks),
                    "tokens": tokens,
                    "first_chunk_tokens": chunks[0].token_count if chunks else 0,
                    "last_chunk_tokens": chunks[-1].token_count if chunks else 0,
                }
            )

        return {
            "symbol": canonical,
            "chunk_size": size,
            "overlap": window,
            "tokenizer": self.tokenizer_name,
            "announcements": len(sources),
            "chunks": total_chunks,
            "tokens": total_tokens,
            "items": items,
        }

    # -- writing ------------------------------------------------------------

    def embed(
        self,
        symbol: str | None = None,
        *,
        limit: int = 50,
        pending: bool = True,
        model: str | None = None,
        chunk_size: int | None = None,
        overlap: int | None = None,
    ) -> EmbedReport:
        """Chunk, embed and upsert announcements selected from DWD."""

        canonical = self._optional_symbol(symbol)
        bounded_limit = _bounded(limit, field_name="limit", low=1, high=MAX_LIMIT)
        size, window = self._settings(chunk_size, overlap)
        sources = self._sources(canonical, limit=bounded_limit, pending=pending)
        return self.embed_sources(
            sources,
            model=model,
            chunk_size=size,
            overlap=window,
        )

    def embed_sources(
        self,
        sources: Iterable[AnnouncementSource],
        *,
        model: str | None = None,
        chunk_size: int | None = None,
        overlap: int | None = None,
    ) -> EmbedReport:
        """Run the chunk/embed/upsert pipeline over already-selected DWD rows."""

        size, window = self._settings(chunk_size, overlap)
        embedding_model = model or self.config.embedding_model
        embedded_at = datetime.now(UTC)

        results: list[AnnouncementResult] = []
        for source in sources:
            results.append(
                self._process_source(
                    source,
                    embedding_model=embedding_model,
                    embedded_at=embedded_at,
                    chunk_size=size,
                    overlap=window,
                )
            )

        return EmbedReport(
            model=embedding_model,
            announcements=len(results),
            skipped=sum(1 for result in results if result.chunks == 0),
            failed=sum(1 for result in results if result.failed > 0),
            chunks=sum(result.chunks for result in results),
            written=sum(result.written for result in results),
            inserted=sum(result.inserted for result in results),
            updated=sum(result.updated for result in results),
            deleted=sum(result.deleted for result in results),
            missing=sum(result.failed for result in results),
            results=tuple(results),
        )

    def _process_source(
        self,
        source: AnnouncementSource,
        *,
        embedding_model: str,
        embedded_at: datetime,
        chunk_size: int,
        overlap: int,
    ) -> AnnouncementResult:
        chunks = self._chunks(source, chunk_size, overlap)
        if not chunks:
            return AnnouncementResult(dwd_ref_id=source.dwd_ref_id, symbol=source.symbol)

        outcome = self.embedding.embed([chunk.text for chunk in chunks])
        rows: list[EmbeddedChunk] = []
        for chunk, vector in zip(chunks, outcome.vectors, strict=False):
            if vector is None:
                continue
            rows.append(
                EmbeddedChunk(
                    chunk_index=chunk.index,
                    chunk_text=chunk.text,
                    token_count=chunk.token_count,
                    embedding=vector,
                )
            )

        VECTOR_METRICS.embedding("ok", len(rows))
        if outcome.failures:
            VECTOR_METRICS.embedding("rejected", outcome.failures)
            self._record_missing(outcome)

        counts = UpsertCounts()
        if rows:
            with self.database.session_scope() as session:
                counts = self._writer_factory(session).replace(
                    dwd_ref_id=source.dwd_ref_id,
                    symbol=source.symbol,
                    ann_date=source.ann_date,
                    ann_type=source.ann_type,
                    chunk_total=len(chunks),
                    chunks=rows,
                    embedding_model=embedding_model,
                    embedded_at=embedded_at,
                )
            VECTOR_METRICS.chunk("written", counts.written)
            VECTOR_METRICS.chunk("deleted", counts.deleted)

        return AnnouncementResult(
            dwd_ref_id=source.dwd_ref_id,
            symbol=source.symbol,
            chunks=len(chunks),
            written=counts.written,
            inserted=counts.inserted,
            updated=counts.updated,
            deleted=counts.deleted,
            failed=outcome.failures,
            error=outcome.errors[0] if outcome.errors else None,
        )

    @staticmethod
    def _record_missing(outcome: EmbeddingOutcome) -> None:
        for reason, count in outcome.reasons.items():
            VECTOR_METRICS.missing_chunk(reason, count)

    # -- search -------------------------------------------------------------

    def search(
        self,
        symbol: str | None,
        question: str | None,
        *,
        year_range: str | None = None,
        top_k: int = 10,
    ) -> QueryResult:
        """Filter-first semantic search; ``symbol`` is mandatory."""

        if not symbol or not str(symbol).strip():
            raise InvalidArgumentError(
                "vector search requires --symbol",
                details={"field": "symbol", "hint": "a symbol is required to avoid a full scan"},
            )
        canonical = normalize_symbol(symbol)
        text = (question or "").strip()
        if not text:
            raise InvalidArgumentError("query must not be empty", details={"field": "query"})
        bounded = _bounded(top_k, field_name="top_k", low=1, high=MAX_TOP_K)

        started = time.perf_counter()
        try:
            return self.query.search_announcements(
                canonical,
                text,
                year_range=year_range,
                top_k=bounded,
            )
        finally:
            VECTOR_METRICS.observe_search(time.perf_counter() - started)

    # -- maintenance --------------------------------------------------------

    def reindex(
        self,
        symbol: str | None = None,
        *,
        limit: int = 200,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Re-embed stored announcements and rebuild the HNSW index."""

        report = self.embed(symbol, limit=limit, pending=False, model=model)
        payload = report.payload()
        payload["index"] = self.index_rebuild()
        return payload

    def stats(self, symbol: str | None = None) -> dict[str, Any]:
        """Describe stored chunks, pending announcements and the HNSW index."""

        canonical = self._optional_symbol(symbol)
        where = " WHERE symbol = :symbol" if canonical else ""
        params = {"symbol": canonical} if canonical else {}
        pending_sql = (
            "SELECT count(*) FROM dwd.stock_announcement AS a "
            "WHERE a.clean_content IS NOT NULL AND btrim(a.clean_content) <> '' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM vector.announcement_chunk AS c WHERE c.dwd_ref_id = a.id"
            ")"
        )
        if canonical:
            pending_sql += " AND a.symbol = :symbol"

        with self.database.session() as session:
            row = session.execute(text(_STATS + where), params).mappings().one()
            pending = session.execute(text(pending_sql), params).scalar_one()

        models = row.get("models")
        return {
            "symbol": canonical,
            "chunks": int(row.get("chunks") or 0),
            "announcements": int(row.get("announcements") or 0),
            "symbols": int(row.get("symbols") or 0),
            "tokens": int(row.get("tokens") or 0),
            "models": [part for part in (models or "").split(",") if part],
            "first_embedded_at": _iso(row.get("first_embedded_at")),
            "last_embedded_at": _iso(row.get("last_embedded_at")),
            "pending_announcements": int(pending or 0),
            "chunk_size": self.chunk_size,
            "overlap": self.overlap,
            "tokenizer": self.tokenizer_name,
            "embedding_dim": self.config.embedding_dim,
            "embedding_model": self.config.embedding_model,
            "index": self.index_status(),
        }

    def index_status(self) -> dict[str, Any]:
        """Return the HNSW index definition, or ``exists: false``."""

        with self.database.session() as session:
            row = session.execute(text(_INDEX_STATUS), {"name": INDEX_NAME}).mappings().first()
        if row is None:
            return {
                "name": INDEX_NAME,
                "exists": False,
                "method": None,
                "definition": None,
                "size_bytes": 0,
            }
        return {
            "name": row["name"],
            "exists": True,
            "method": row["method"],
            "definition": row["definition"],
            "size_bytes": int(row["size_bytes"] or 0),
        }

    def index_create(self) -> dict[str, Any]:
        """Create the HNSW index when it is missing (idempotent)."""

        with self.database.session_scope() as session:
            session.execute(text(_CREATE_INDEX))
        return self.index_status()

    def index_drop(self) -> dict[str, Any]:
        """Drop the HNSW index; searches keep working with a sequential scan."""

        with self.database.session_scope() as session:
            session.execute(text(_DROP_INDEX))
        return self.index_status()

    def index_rebuild(self) -> dict[str, Any]:
        """Rebuild the index in place, or create it when absent."""

        with self.database.session_scope() as session:
            exists = (
                session.execute(text(_INDEX_STATUS), {"name": INDEX_NAME}).first()
                is not None
            )
            session.execute(text(_REINDEX_INDEX if exists else _CREATE_INDEX))
        return self.index_status()

    # -- helpers ------------------------------------------------------------

    def _settings(
        self, chunk_size: int | None, overlap: int | None
    ) -> tuple[int, int]:
        size = self.chunk_size if chunk_size is None else int(chunk_size)
        window = self.overlap if overlap is None else int(overlap)
        validate_chunk_settings(size, window)
        return size, window

    def _chunks(self, source: AnnouncementSource, chunk_size: int, overlap: int) -> list[Chunk]:
        return chunk_text(
            source.content,
            tokenizer=self.tokenizer,
            chunk_size=chunk_size,
            overlap=overlap,
        )

    @staticmethod
    def _optional_symbol(symbol: str | None) -> str | None:
        if symbol is None or not str(symbol).strip():
            return None
        return normalize_symbol(symbol)

    def _sources(
        self, symbol: str | None, *, limit: int, pending: bool
    ) -> list[AnnouncementSource]:
        clauses = [
            "a.clean_content IS NOT NULL",
            "btrim(a.clean_content) <> ''",
        ]
        params: dict[str, Any] = {"limit": limit}
        if symbol is not None:
            clauses.append("a.symbol = :symbol")
            params["symbol"] = symbol
        if pending:
            clauses.append(
                "NOT EXISTS ("
                "SELECT 1 FROM vector.announcement_chunk AS c WHERE c.dwd_ref_id = a.id"
                ")"
            )
        sql = (
            "SELECT a.id AS dwd_ref_id, a.symbol, a.ann_date, a.ann_type, "
            "a.title, a.clean_content AS content "
            "FROM dwd.stock_announcement AS a "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY a.ann_date DESC, a.id DESC LIMIT :limit"
        )
        with self.database.session() as session:
            rows = session.execute(text(sql), params).mappings().all()
        return [
            AnnouncementSource(
                dwd_ref_id=int(row["dwd_ref_id"]),
                symbol=str(row["symbol"]),
                ann_date=row["ann_date"],
                ann_type=row["ann_type"],
                title=row["title"],
                content=str(row["content"]),
            )
            for row in rows
        ]


__all__ = [
    "INDEX_NAME",
    "MAX_LIMIT",
    "AnnouncementResult",
    "AnnouncementSource",
    "EmbedReport",
    "VectorService",
]
