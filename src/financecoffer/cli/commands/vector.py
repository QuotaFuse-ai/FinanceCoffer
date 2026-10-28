"""Vector layer commands: chunk, embed, search, reindex, stats and the HNSW index.

``vector chunk`` only reports the windows ``vector embed`` would write, because
``vector.announcement_chunk.embedding`` is ``NOT NULL``: a chunk row exists only
once its vector does. ``vector search`` requires ``--symbol`` so the query stays
filter-first and can never trigger a full-table vector scan.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import typer
from sqlalchemy.exc import SQLAlchemyError

from financecoffer.cli.options import GlobalOptions, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_cli_config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import (
    FinanceCofferError,
    InvalidArgumentError,
    PartialFailureError,
    StorageError,
)
from financecoffer.query.service import MAX_TOP_K
from financecoffer.vector import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, VectorService
from financecoffer.vector.embeddings import DEFAULT_BATCH_SIZE
from financecoffer.vector.service import MAX_LIMIT

_MAX_BATCH_OPTION = 64


def _service(
    options: GlobalOptions,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> VectorService:
    try:
        config = load_cli_config(db_url=options.db_url)
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    return VectorService(
        config=config,
        dsn=options.db_url,
        timeout=options.timeout,
        retries=options.retries,
        batch_size=batch_size,
        chunk_size=chunk_size,
        overlap=overlap,
    )


def _run(
    options: GlobalOptions,
    action: Callable[[VectorService], Any],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Any:
    service = _service(
        options,
        chunk_size=chunk_size,
        overlap=overlap,
        batch_size=batch_size,
    )
    try:
        return action(service)
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    except SQLAlchemyError:
        emit_failure(
            options,
            StorageError(
                "vector database operation failed",
                details={"hint": "check FC_DB_URL and that migrations are applied"},
            ),
        )
    finally:
        service.close()


def _selection(options: GlobalOptions, *, symbol: str | None, all_symbols: bool) -> str | None:
    if symbol and all_symbols:
        emit_failure(
            options,
            InvalidArgumentError(
                "--all and --symbol are mutually exclusive",
                details={"field": "symbol"},
            ),
        )
    return None if all_symbols else symbol


def register(app: typer.Typer) -> None:
    vector = add_group(app, name="vector", help_text="Chunking, embeddings and HNSW search.")

    @vector.command("chunk", help="Report the token chunks embed would write (read-only).")
    def chunk(
        ctx: typer.Context,
        symbol: str | None = typer.Option(None, "--symbol", help="Limit to one symbol."),
        all_symbols: bool = typer.Option(
            False, "--all", help="Plan chunks for every announcement in DWD."
        ),
        limit: int = typer.Option(
            100, "--limit", min=1, max=MAX_LIMIT, help="Maximum announcements to plan."
        ),
        chunk_size: int = typer.Option(
            DEFAULT_CHUNK_SIZE, "--chunk-size", min=1, help="Tokens per chunk."
        ),
        overlap: int = typer.Option(
            DEFAULT_OVERLAP, "--overlap", min=0, help="Tokens shared by neighbours."
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Accepted for compatibility; chunk always plans and never writes.",
        ),
    ) -> None:
        options = from_context(ctx)
        canonical = _selection(options, symbol=symbol, all_symbols=all_symbols)
        payload = _run(
            options,
            lambda service: service.chunk_preview(
                canonical,
                limit=limit,
                chunk_size=chunk_size,
                overlap=overlap,
            ),
            chunk_size=chunk_size,
            overlap=overlap,
        )
        emit_success(options, payload, meta=Meta(source="vector"))

    @vector.command("embed", help="Chunk, embed and upsert announcements selected from DWD.")
    def embed(
        ctx: typer.Context,
        symbol: str | None = typer.Option(None, "--symbol", help="Limit to one symbol."),
        limit: int = typer.Option(
            50, "--limit", min=1, max=MAX_LIMIT, help="Maximum announcements to embed."
        ),
        pending: bool = typer.Option(
            True,
            "--pending/--no-pending",
            help="Only announcements that have no chunks yet (default: on).",
        ),
        batch_size: int = typer.Option(
            DEFAULT_BATCH_SIZE,
            "--batch-size",
            min=1,
            max=_MAX_BATCH_OPTION,
            help="Inputs per request; the client caps every request at 32.",
        ),
        model: str | None = typer.Option(
            None, "--model", help="Embedding model label (default: FC_EMBEDDING_MODEL)."
        ),
        chunk_size: int = typer.Option(
            DEFAULT_CHUNK_SIZE, "--chunk-size", min=1, help="Tokens per chunk."
        ),
        overlap: int = typer.Option(
            DEFAULT_OVERLAP, "--overlap", min=0, help="Tokens shared by neighbours."
        ),
    ) -> None:
        options = from_context(ctx)
        report = _run(
            options,
            lambda service: service.embed(
                symbol,
                limit=limit,
                pending=pending,
                model=model,
                chunk_size=chunk_size,
                overlap=overlap,
            ),
            chunk_size=chunk_size,
            overlap=overlap,
            batch_size=batch_size,
        )
        payload = report.payload()
        if report.failed:
            emit_failure(
                options,
                PartialFailureError(
                    "some chunks could not be embedded; see details.missing_chunks",
                    details=payload,
                ),
            )
        emit_success(options, payload, meta=Meta(source="vector"))

    @vector.command("search", help="Filter-first semantic search; --symbol is mandatory.")
    def search(
        ctx: typer.Context,
        query: str | None = typer.Argument(None, help="Natural-language query."),
        symbol: str | None = typer.Option(
            None, "--symbol", help="A-share code or US ticker (required)."
        ),
        query_option: str | None = typer.Option(
            None, "--query", help="Natural-language query (alternative to the positional form)."
        ),
        year_range: str | None = typer.Option(
            None, "--year-range", help="Inclusive window as YYYY-YYYY (max 10 years)."
        ),
        top_k: int = typer.Option(10, "--top-k", min=1, max=MAX_TOP_K, help="Maximum hits."),
    ) -> None:
        options = from_context(ctx)
        if query and query_option:
            emit_failure(
                options,
                InvalidArgumentError(
                    "pass the query once: positional or --query",
                    details={"field": "query"},
                ),
            )
        text = query or query_option
        if not text:
            emit_failure(
                options,
                InvalidArgumentError("a query is required", details={"field": "query"}),
            )
        result = _run(
            options,
            lambda service: service.search(
                symbol,
                text,
                year_range=year_range,
                top_k=top_k,
            ),
        )
        emit_success(options, result.data, meta=result.meta(options.trace_id))

    @vector.command("reindex", help="Re-embed stored announcements and rebuild the HNSW index.")
    def reindex(
        ctx: typer.Context,
        symbol: str | None = typer.Option(None, "--symbol", help="Limit to one symbol."),
        limit: int = typer.Option(
            200, "--limit", min=1, max=MAX_LIMIT, help="Maximum announcements to re-embed."
        ),
        model: str | None = typer.Option(
            None, "--model", help="Embedding model label (default: FC_EMBEDDING_MODEL)."
        ),
    ) -> None:
        options = from_context(ctx)
        payload = _run(
            options,
            lambda service: service.reindex(symbol, limit=limit, model=model),
        )
        if payload["failed"]:
            emit_failure(
                options,
                PartialFailureError(
                    "some chunks could not be embedded; see details.missing_chunks",
                    details=payload,
                ),
            )
        emit_success(options, payload, meta=Meta(source="vector"))

    @vector.command("stats", help="Show chunk, embedding and HNSW index statistics.")
    def stats(
        ctx: typer.Context,
        symbol: str | None = typer.Option(None, "--symbol", help="Limit to one symbol."),
    ) -> None:
        options = from_context(ctx)
        payload = _run(options, lambda service: service.stats(symbol))
        emit_success(options, payload, meta=Meta(source="vector"))

    index = add_group(vector, name="index", help_text="HNSW index lifecycle.")

    @index.command("create", help="Create the HNSW index when it is missing.")
    def index_create(ctx: typer.Context) -> None:
        options = from_context(ctx)
        payload = _run(options, lambda service: service.index_create())
        emit_success(options, payload, meta=Meta(source="vector"))

    @index.command("status", help="Show the HNSW index definition.")
    def index_status(ctx: typer.Context) -> None:
        options = from_context(ctx)
        payload = _run(options, lambda service: service.index_status())
        emit_success(options, payload, meta=Meta(source="vector"))

    @index.command("drop", help="Drop the HNSW index; searches fall back to a scan.")
    def index_drop(ctx: typer.Context) -> None:
        options = from_context(ctx)
        payload = _run(options, lambda service: service.index_drop())
        emit_success(options, payload, meta=Meta(source="vector"))
