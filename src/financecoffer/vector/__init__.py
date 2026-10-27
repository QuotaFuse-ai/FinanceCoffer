"""Vector layer: token-aware chunking, batch embeddings and pgvector writes."""

from financecoffer.vector.chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    Chunk,
    Tokenizer,
    chunk_text,
    default_tokenizer,
)
from financecoffer.vector.embeddings import EmbeddingClient
from financecoffer.vector.metrics import VECTOR_METRICS, VectorMetrics
from financecoffer.vector.service import VectorService
from financecoffer.vector.writer import ChunkWriter, UpsertCounts

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_OVERLAP",
    "VECTOR_METRICS",
    "Chunk",
    "ChunkWriter",
    "EmbeddingClient",
    "Tokenizer",
    "UpsertCounts",
    "VectorMetrics",
    "VectorService",
    "chunk_text",
    "default_tokenizer",
]
