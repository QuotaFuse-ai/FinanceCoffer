"""Deterministic, token-aware chunking for announcement text.

Chunks are 512 tokens wide with a 50-token overlap (``financecoffer.md``
section 5). Boundaries follow the embedding model's tokenizer so a chunk never
exceeds what bge-m3 can encode in one window. Token offsets are normalized to be
monotonic: byte-level tokenizers occasionally report overlapping spans, and a
character slice must never run backwards.

The tokenizer is located through ``FC_TOKENIZER_PATH`` or
``FC_EMBEDDING_MODEL_DIR`` (the same variable the local embedding launcher
uses), falling back to ``var/models/bge-m3``. When no tokenizer file is
available the deterministic approximate tokenizer keeps the CLI usable without
pretending to be exact.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from financecoffer.core.errors import InvalidArgumentError

DEFAULT_CHUNK_SIZE = 512
DEFAULT_OVERLAP = 50

_TOKENIZER_FILE = "tokenizer.json"
_DEFAULT_MODEL_DIR = Path("var") / "models" / "bge-m3"
_APPROX_TOKEN = re.compile(
    r"\s+|[\u3400-\u9fff\uf900-\ufaff]|[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u3400-\u9fff\uf900-\ufaff]"
)

_log = logging.getLogger(__name__)


class Tokenizer(Protocol):
    """Minimal interface the chunker needs from a tokenizer."""

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Return one ``(start, end)`` character span per token."""


@dataclass(frozen=True)
class Chunk:
    """One chunk plus the token window it was cut from."""

    index: int
    text: str
    token_count: int
    start_token: int
    end_token: int
    start_char: int
    end_char: int


@dataclass(frozen=True)
class TokenizerSelection:
    """The chunker a process ended up with, and why.

    ``degraded`` is the flag callers must disclose: token counts are then
    approximate, so a 512-token window is not exactly what bge-m3 encodes.
    """

    tokenizer: Tokenizer
    name: str
    degraded: bool
    reason: str | None


def tokenizer_metadata(selection: TokenizerSelection) -> dict[str, object]:
    """The ``tokenizer_name`` / ``degraded`` / ``degraded_reason`` disclosure."""

    return {
        "tokenizer_name": selection.name,
        "degraded": selection.degraded,
        "degraded_reason": selection.reason,
    }


class HuggingFaceTokenizer:
    """Wrapper around ``tokenizers.Tokenizer`` with normalized offsets."""

    def __init__(self, path: str | Path) -> None:
        from tokenizers import Tokenizer as _HFTokenizer

        self.path = Path(path)
        self._tokenizer = _HFTokenizer.from_file(str(self.path))

    def spans(self, text: str) -> list[tuple[int, int]]:
        if not text:
            return []
        encoding = self._tokenizer.encode(text, add_special_tokens=False)
        raw = [(int(start), int(end)) for start, end in encoding.offsets]
        return _monotonic_offsets(raw, len(text))


class ApproximateTokenizer:
    """Small deterministic tokenizer used when the model tokenizer is absent.

    It treats every CJK character, Latin/number run, punctuation mark and
    whitespace run as one token. Counts are close enough to keep chunk sizes
    bounded; exact 512-token windows require the model tokenizer.
    """

    def spans(self, text: str) -> list[tuple[int, int]]:
        return [(match.start(), match.end()) for match in _APPROX_TOKEN.finditer(text)]


def _monotonic_offsets(
    offsets: Sequence[tuple[int, int]], length: int
) -> list[tuple[int, int]]:
    """Force non-overlapping, in-bounds spans while preserving order."""

    spans: list[tuple[int, int]] = []
    previous_end = 0
    for raw_start, raw_end in offsets:
        start = max(int(raw_start), previous_end, 0)
        if start >= length:
            start = length - 1
        end = min(max(int(raw_end), start), length)
        if end <= start:
            end = min(start + 1, length)
        spans.append((start, end))
        previous_end = end
    return spans


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("FC_TOKENIZER_PATH", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    model_dir = os.environ.get("FC_EMBEDDING_MODEL_DIR", "").strip()
    if model_dir:
        candidates.append(Path(model_dir) / _TOKENIZER_FILE)
    candidates.append(_DEFAULT_MODEL_DIR / _TOKENIZER_FILE)
    candidates.append(
        Path(__file__).resolve().parents[3] / _DEFAULT_MODEL_DIR / _TOKENIZER_FILE
    )
    return candidates


def resolve_tokenizer_path() -> Path | None:
    """Return the first configured tokenizer file that exists."""

    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def default_tokenizer_selection() -> TokenizerSelection:
    """Load the model tokenizer once per process, disclosing any fallback."""

    path = resolve_tokenizer_path()
    if path is None:
        reason = (
            "tokenizer.json not found in FC_TOKENIZER_PATH, FC_EMBEDDING_MODEL_DIR "
            "or var/models/bge-m3"
        )
        _log.warning("bge-m3 tokenizer not found; using approximate tokens: %s", reason)
        return TokenizerSelection(ApproximateTokenizer(), "approximate", True, reason)
    try:
        return TokenizerSelection(HuggingFaceTokenizer(path), "bge-m3", False, None)
    except Exception as exc:  # noqa: BLE001 - any load failure falls back
        _log.warning("cannot load the bge-m3 tokenizer; using approximate tokens: %s", exc)
        return TokenizerSelection(
            ApproximateTokenizer(), "approximate", True, f"cannot load {path}: {exc}"
        )


def default_tokenizer() -> Tokenizer:
    """The chunker for this process: the model tokenizer, or the fallback."""

    return default_tokenizer_selection().tokenizer


def validate_chunk_settings(chunk_size: int, overlap: int) -> None:
    """Reject settings that cannot produce non-empty, forward-moving chunks."""

    if chunk_size < 1:
        raise InvalidArgumentError(
            "chunk_size must be at least 1",
            details={"field": "chunk_size", "value": chunk_size},
        )
    if overlap < 0:
        raise InvalidArgumentError(
            "overlap must not be negative",
            details={"field": "overlap", "value": overlap},
        )
    if overlap >= chunk_size:
        raise InvalidArgumentError(
            "overlap must be smaller than chunk_size",
            details={"field": "overlap", "value": overlap, "chunk_size": chunk_size},
        )


def chunk_text(
    text: str,
    *,
    tokenizer: Tokenizer,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Split ``text`` into token windows of ``chunk_size`` with ``overlap``.

    The same text and tokenizer always produce byte-identical chunks. The last
    chunk always reaches the end of the text, so no tail is dropped.
    """

    validate_chunk_settings(chunk_size, overlap)
    if not text:
        return []

    spans = tokenizer.spans(text)
    if not spans:
        return []

    total = len(spans)
    step = chunk_size - overlap
    chunks: list[Chunk] = []
    start = 0
    while True:
        end = min(start + chunk_size, total)
        start_char = spans[start][0]
        end_char = len(text) if end == total else spans[end - 1][1]
        if end_char <= start_char:
            end_char = min(start_char + 1, len(text))
        chunks.append(
            Chunk(
                index=len(chunks),
                text=text[start_char:end_char],
                token_count=end - start,
                start_token=start,
                end_token=end,
                start_char=start_char,
                end_char=end_char,
            )
        )
        if end == total:
            break
        start += step
    return chunks


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_OVERLAP",
    "ApproximateTokenizer",
    "Chunk",
    "HuggingFaceTokenizer",
    "Tokenizer",
    "TokenizerSelection",
    "chunk_text",
    "default_tokenizer",
    "default_tokenizer_selection",
    "resolve_tokenizer_path",
    "tokenizer_metadata",
    "validate_chunk_settings",
]
