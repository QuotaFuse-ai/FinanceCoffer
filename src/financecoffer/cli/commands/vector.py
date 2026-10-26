"""Vector layer commands."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_group, add_placeholder


def register(app: typer.Typer) -> None:
    vector = add_group(app, name="vector", help_text="Chunking, embeddings and HNSW search.")
    add_placeholder(vector, "chunk", path="vector chunk", summary="Split announcements into chunks")
    add_placeholder(vector, "embed", path="vector embed", summary="Embed pending chunks")
    add_placeholder(vector, "search", path="vector search", summary="Filtered vector search")
    add_placeholder(vector, "reindex", path="vector reindex", summary="Rebuild vector indexes")
    add_placeholder(vector, "stats", path="vector stats", summary="Show vector-layer statistics")

    index = add_group(vector, name="index", help_text="HNSW index lifecycle.")
    add_placeholder(index, "create", path="vector index create", summary="Create the HNSW index")
    add_placeholder(index, "status", path="vector index status", summary="Show index status")
    add_placeholder(index, "drop", path="vector index drop", summary="Drop the HNSW index")
