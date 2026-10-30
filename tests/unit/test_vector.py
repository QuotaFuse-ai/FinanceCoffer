"""Vector layer unit tests: chunking, embedding client, service guards and CLI.

Everything here runs without PostgreSQL. The database writer and the embedding
endpoint are replaced by fakes, so the tests pin the arithmetic (512/50 windows),
the request bounds (32 inputs), failure accounting (``fc_missing_chunk_total``)
and the CLI error codes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from financecoffer.cli import app
from financecoffer.core.config import Config
from financecoffer.core.errors import InvalidArgumentError
from financecoffer.query.service import QueryResult
from financecoffer.vector.chunking import chunk_text, validate_chunk_settings
from financecoffer.vector.embeddings import (
    MAX_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingOutcome,
    bounded_batch_size,
)
from financecoffer.vector.metrics import VECTOR_METRICS, VectorMetrics
from financecoffer.vector.service import AnnouncementSource, EmbedReport, VectorService
from financecoffer.vector.writer import UpsertCounts

runner = CliRunner()

URL = "http://127.0.0.1:8001/v1/embeddings"


class CharTokenizer:
    """One token per character, so the 512/50 arithmetic is easy to assert."""

    def spans(self, text: str) -> list[tuple[int, int]]:
        return [(index, index + 1) for index in range(len(text))]


def config(**overrides: Any) -> Config:
    values: dict[str, Any] = {
        "embedding_url": URL,
        "embedding_dim": 4,
        "embedding_model": "BAAI/bge-m3",
    }
    values.update(overrides)
    return Config(**values)


@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    VECTOR_METRICS.reset()
    yield
    VECTOR_METRICS.reset()


# -- chunking ---------------------------------------------------------------


def test_long_text_is_split_into_overlapping_windows() -> None:
    text = "".join(chr(0x4E00 + index % 500) for index in range(1200))

    chunks = chunk_text(text, tokenizer=CharTokenizer(), chunk_size=512, overlap=50)

    assert [chunk.token_count for chunk in chunks] == [512, 512, 276]
    assert [chunk.index for chunk in chunks] == [0, 1, 2]
    assert chunks[1].start_token == 512 - 50
    for previous, current in zip(chunks[:-1], chunks[1:], strict=True):
        assert previous.end_token - current.start_token == 50
    assert chunks[-1].end_char == len(text)
    assert "".join(chunk.text for chunk in chunks[:2]) != text  # overlap duplicates a window
    assert all(chunk.token_count > 0 for chunk in chunks)


def test_short_text_is_one_chunk_and_tail_is_not_truncated() -> None:
    text = "a" * 300

    chunks = chunk_text(text, tokenizer=CharTokenizer(), chunk_size=512, overlap=50)

    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].token_count == 300
    assert chunks[0].end_char == len(text)


def test_chunking_is_deterministic_and_empty_text_has_no_chunks() -> None:
    text = "b" * 700

    first = chunk_text(text, tokenizer=CharTokenizer(), chunk_size=512, overlap=50)
    second = chunk_text(text, tokenizer=CharTokenizer(), chunk_size=512, overlap=50)

    assert first == second
    assert chunk_text("", tokenizer=CharTokenizer()) == []


@pytest.mark.parametrize(
    ("size", "overlap", "message"),
    [
        (0, 0, "chunk_size must be at least 1"),
        (512, -1, "overlap must not be negative"),
        (512, 512, "overlap must be smaller than chunk_size"),
    ],
)
def test_invalid_chunk_settings_are_rejected(size: int, overlap: int, message: str) -> None:
    with pytest.raises(InvalidArgumentError) as caught:
        validate_chunk_settings(size, overlap)

    assert caught.value.message == message


# -- embedding client -------------------------------------------------------


def test_batch_size_is_capped_at_the_upstream_limit() -> None:
    assert bounded_batch_size(64) == MAX_BATCH_SIZE
    assert bounded_batch_size(1) == 1
    with pytest.raises(InvalidArgumentError):
        bounded_batch_size(0)


def _embedding_response(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    inputs = payload["input"]
    assert len(inputs) <= MAX_BATCH_SIZE
    data = [{"index": index, "embedding": [1.0, 0.0, 0.0, 0.0]} for index in range(len(inputs))]
    return httpx.Response(200, json={"data": data})


def test_large_inputs_are_split_into_bounded_requests() -> None:
    batches: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        batches.append(len(json.loads(request.content)["input"]))
        return _embedding_response(request)

    client = EmbeddingClient(config(), batch_size=64, transport=httpx.MockTransport(handler))

    outcome = client.embed([f"text {index}" for index in range(70)])

    assert batches == [32, 32, 6]
    assert outcome.requests == 3
    assert outcome.failures == 0
    assert outcome.ok
    assert len(outcome.vectors) == 70
    assert all(vector is not None for vector in outcome.vectors)


def test_upstream_5xx_is_retried_with_exponential_backoff() -> None:
    sleeps: list[float] = []
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503)
        return _embedding_response(request)

    client = EmbeddingClient(
        config(),
        retries=2,
        backoff=0.25,
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    outcome = client.embed(["one"])

    assert outcome.ok
    assert len(attempts) == 3
    assert sleeps == [0.25, 0.5]


def test_timeout_is_retried_then_reported_per_input() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out", request=request)

    client = EmbeddingClient(
        config(),
        retries=2,
        backoff=0.25,
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    outcome = client.embed(["one", "two"])

    assert not outcome.ok
    assert outcome.failures == 2
    assert outcome.reasons == {"embedding_unavailable": 2}
    assert outcome.errors
    assert sleeps == [0.25, 0.5]


def test_client_errors_are_not_retried() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"error": "bad input"})

    client = EmbeddingClient(
        config(),
        retries=2,
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    outcome = client.embed(["one"])

    assert len(attempts) == 1
    assert sleeps == []
    assert outcome.failures == 1
    assert outcome.reasons == {"embedding_unavailable": 1}


def test_invalid_vectors_are_rejected_and_valid_ones_are_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            '{"data": ['
            '{"index": 0, "embedding": [0.0, 0.0, 0.0, 0.0]},'
            '{"index": 1, "embedding": [1.0, 0.0]},'
            '{"index": 2, "embedding": [NaN, 0.0, 0.0, 0.0]},'
            '{"index": 3, "embedding": [3.0, 4.0, 0.0, 0.0]}'
            "]}"
        )
        return httpx.Response(200, content=body.encode())

    client = EmbeddingClient(config(), transport=httpx.MockTransport(handler))

    outcome = client.embed(["a", "b", "c", "d"])

    assert outcome.vectors[:3] == (None, None, None)
    assert outcome.vectors[3] == pytest.approx([0.6, 0.8, 0.0, 0.0])
    assert outcome.failures == 3
    assert outcome.reasons == {"invalid_vector": 3}
    assert outcome.errors == ()


def test_mismatched_embedding_count_fails_the_whole_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}]})

    client = EmbeddingClient(config(), transport=httpx.MockTransport(handler))

    outcome = client.embed(["a", "b"])

    assert outcome.vectors == (None, None)
    assert outcome.reasons == {"embedding_unavailable": 2}


def test_embedding_nothing_returns_an_empty_outcome() -> None:
    outcome = EmbeddingClient(config()).embed([])

    assert outcome == EmbeddingOutcome()


# -- service ----------------------------------------------------------------


def _service(**overrides: Any) -> VectorService:
    return VectorService(config=config(), tokenizer=CharTokenizer(), **overrides)


def test_an_injected_exact_tokenizer_is_not_flagged_degraded() -> None:
    service = _service()

    assert service.tokenizer_name == "CharTokenizer"
    assert service.tokenizer_degraded is False
    assert service.tokenizer_metadata() == {
        "tokenizer_name": "CharTokenizer",
        "degraded": False,
        "degraded_reason": None,
    }


def test_missing_tokenizer_is_disclosed_in_the_embed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import financecoffer.vector.chunking as chunking

    monkeypatch.setattr(chunking, "resolve_tokenizer_path", lambda: None)
    chunking.default_tokenizer_selection.cache_clear()
    try:
        service = VectorService(config=config())
        payload = service.embed_sources([]).payload()
    finally:
        chunking.default_tokenizer_selection.cache_clear()

    assert payload["tokenizer_name"] == "approximate"
    assert payload["degraded"] is True
    assert "tokenizer.json" in payload["degraded_reason"]


def test_missing_tokenizer_is_disclosed_in_the_chunk_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import financecoffer.vector.chunking as chunking

    monkeypatch.setattr(chunking, "resolve_tokenizer_path", lambda: None)
    monkeypatch.setattr(
        VectorService,
        "_sources",
        lambda self, symbol, *, limit, pending: [],
    )
    chunking.default_tokenizer_selection.cache_clear()
    try:
        preview = VectorService(config=config()).chunk_preview(None, limit=5)
    finally:
        chunking.default_tokenizer_selection.cache_clear()

    assert preview["tokenizer_name"] == "approximate"
    assert preview["degraded"] is True
    assert preview["degraded_reason"] is not None


def test_search_requires_a_symbol_before_any_connection() -> None:
    service = _service()

    with pytest.raises(InvalidArgumentError) as caught:
        service.search(None, "any question")

    assert caught.value.details is not None
    assert caught.value.details["field"] == "symbol"
    assert service._database is None
    assert service._query_service is None


def test_search_rejects_top_k_above_the_cap_before_connecting() -> None:
    service = _service()

    with pytest.raises(InvalidArgumentError):
        service.search("600000", "question", top_k=51)

    assert service._query_service is None


def test_search_delegates_and_records_latency() -> None:
    class _Query:
        calls: list[tuple[str, str, str | None, int]] = []

        def search_announcements(
            self,
            symbol: str,
            text: str,
            *,
            year_range: str | None = None,
            top_k: int = 10,
        ) -> QueryResult:
            self.calls.append((symbol, text, year_range, top_k))
            return QueryResult(data=[{"symbol": symbol}], source="vector")

    query = _Query()
    service = _service(query_service=query)

    result = service.search("600000", "revenue", year_range="2023-2026", top_k=5)

    assert result.data == [{"symbol": "600000"}]
    assert query.calls == [("600000", "revenue", "2023-2026", 5)]
    assert VECTOR_METRICS.snapshot()["search_count"] == 1


class _FakeDatabase:
    def __init__(self) -> None:
        self.sessions = 0

    @contextmanager
    def session_scope(self) -> Iterator[object]:
        self.sessions += 1
        yield object()


class _FakeWriter:
    def __init__(self, store: list[dict[str, Any]]) -> None:
        self.store = store

    def replace(self, **kwargs: Any) -> UpsertCounts:
        self.store.append(kwargs)
        count = len(kwargs["chunks"])
        return UpsertCounts(inserted=count, updated=0, deleted=0, embedded=count)


class _ScriptedEmbedding:
    """Return one scripted outcome per call, in order."""

    def __init__(self, outcomes: list[EmbeddingOutcome]) -> None:
        self.outcomes = outcomes
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> EmbeddingOutcome:
        self.batches.append(list(texts))
        return self.outcomes.pop(0)


def _vector() -> list[float]:
    return [1.0, 0.0, 0.0, 0.0]


def test_embed_sources_continues_after_a_failed_chunk_and_counts_it() -> None:
    store: list[dict[str, Any]] = []
    database = _FakeDatabase()
    embedding = _ScriptedEmbedding(
        [
            EmbeddingOutcome(vectors=(_vector(),), requests=1, failures=0),
            EmbeddingOutcome(
                vectors=(_vector(), None, _vector()),
                requests=1,
                failures=1,
                reasons={"invalid_vector": 1},
            ),
        ]
    )
    service = _service(embedding_client=embedding)
    service._database = database
    service._writer_factory = lambda _session: _FakeWriter(store)
    sources = [
        AnnouncementSource(
            dwd_ref_id=1,
            symbol="600001",
            ann_date=date(2026, 10, 1),
            ann_type="periodic",
            title="short",
            content="a" * 300,
        ),
        AnnouncementSource(
            dwd_ref_id=2,
            symbol="600002",
            ann_date=date(2026, 10, 2),
            ann_type="periodic",
            title="long",
            content="b" * 1200,
        ),
    ]

    report = service.embed_sources(sources)

    assert (report.announcements, report.chunks, report.failed, report.missing) == (2, 4, 1, 1)
    assert (report.written, report.inserted, report.updated) == (3, 3, 0)
    assert [len(call["chunks"]) for call in store] == [1, 2]
    assert [call["chunk_total"] for call in store] == [1, 3]
    assert database.sessions == 2

    metrics = VECTOR_METRICS.snapshot()
    assert metrics["missing_chunk"] == {"invalid_vector": 1}
    assert metrics["embeddings"] == {"ok": 3, "rejected": 1}
    assert metrics["chunks"] == {"written": 3}


def test_embed_sources_skips_the_write_when_every_chunk_fails() -> None:
    store: list[dict[str, Any]] = []
    database = _FakeDatabase()
    embedding = _ScriptedEmbedding(
        [
            EmbeddingOutcome(
                vectors=(None, None),
                requests=1,
                failures=2,
                reasons={"embedding_unavailable": 2},
                errors=("service down",),
            )
        ]
    )
    service = _service(embedding_client=embedding)
    service._database = database
    service._writer_factory = lambda _session: _FakeWriter(store)
    source = AnnouncementSource(
        dwd_ref_id=3,
        symbol="600003",
        ann_date=date(2026, 10, 3),
        ann_type=None,
        title="down",
        content="c" * 600,
    )

    report = service.embed_sources([source])

    assert store == []
    assert database.sessions == 0
    assert report.failed == 1
    assert report.missing == 2
    assert report.written == 0
    assert report.results[0].error == "service down"
    assert VECTOR_METRICS.snapshot()["missing_chunk"] == {"embedding_unavailable": 2}


# -- metrics ----------------------------------------------------------------


def test_metrics_render_counters_and_a_cumulative_histogram() -> None:
    metrics = VectorMetrics()
    metrics.missing_chunk("invalid_vector", 2)
    metrics.chunk("written", 3)
    metrics.chunk("deleted", 1)
    metrics.embedding("ok", 3)
    metrics.embedding("rejected", 2)
    metrics.observe_search(0.02)
    metrics.observe_search(0.3)

    text = metrics.render()

    assert 'fc_missing_chunk_total{reason="invalid_vector"} 2' in text
    assert 'fc_vector_chunks_total{status="written"} 3' in text
    assert 'fc_vector_chunks_total{status="deleted"} 1' in text
    assert 'fc_vector_embeddings_total{status="rejected"} 2' in text
    assert "# TYPE fc_vector_search_seconds histogram" in text
    assert 'fc_vector_search_seconds_bucket{le="0.025"} 1' in text
    assert 'fc_vector_search_seconds_bucket{le="0.5"} 2' in text
    assert 'fc_vector_search_seconds_bucket{le="+Inf"} 2' in text
    assert "fc_vector_search_seconds_sum 0.320000" in text
    assert "fc_vector_search_seconds_count 2" in text


# -- CLI --------------------------------------------------------------------


class _FakeService:
    instances: list[_FakeService] = []
    payload: dict[str, Any] = {}
    report: EmbedReport | None = None
    error: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        type(self).instances.append(self)

    def close(self) -> None:
        self.closed = True

    def _raise(self) -> None:
        if type(self).error is not None:
            raise type(self).error

    def chunk_preview(self, symbol: str | None, **kwargs: Any) -> dict[str, Any]:
        self.canonical = symbol
        self.options = kwargs
        self._raise()
        return type(self).payload

    def embed(self, symbol: str | None, **kwargs: Any) -> EmbedReport:
        self.canonical = symbol
        self._raise()
        assert type(self).report is not None
        return type(self).report

    def search(self, symbol: str | None, text: str, **kwargs: Any) -> QueryResult:
        self.options = kwargs
        if not symbol:
            raise InvalidArgumentError(
                "vector search requires --symbol",
                details={"field": "symbol"},
            )
        return QueryResult(data=[{"symbol": symbol}], source="vector")

    def index_status(self) -> dict[str, Any]:
        self._raise()
        return type(self).payload

    def stats(self, symbol: str | None) -> dict[str, Any]:
        return type(self).payload


@pytest.fixture()
def fake_service(monkeypatch: pytest.MonkeyPatch) -> type[_FakeService]:
    from financecoffer.cli.commands import vector as vector_module

    _FakeService.instances = []
    _FakeService.payload = {}
    _FakeService.report = None
    _FakeService.error = None
    monkeypatch.setattr(vector_module, "load_cli_config", lambda **_: config())
    monkeypatch.setattr(vector_module, "VectorService", _FakeService)
    return _FakeService


def test_chunk_defaults_to_every_announcement(fake_service: type[_FakeService]) -> None:
    fake_service.payload = {"announcements": 1, "chunks": 1, "items": []}

    result = runner.invoke(app, ["vector", "chunk", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["meta"]["source"] == "vector"
    assert fake_service.instances[0].canonical is None
    assert fake_service.instances[0].closed is True


def test_chunk_rejects_symbol_and_all_together() -> None:
    result = runner.invoke(app, ["vector", "chunk", "--symbol", "600000", "--all"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"


def test_search_without_a_symbol_is_an_invalid_argument(fake_service: type[_FakeService]) -> None:
    result = runner.invoke(app, ["vector", "search", "revenue growth"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["field"] == "symbol"


def test_search_rejects_two_forms_of_the_query(fake_service: type[_FakeService]) -> None:
    result = runner.invoke(app, ["vector", "search", "one", "--query", "two", "--symbol", "600000"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"


def test_search_requires_a_query(fake_service: type[_FakeService]) -> None:
    result = runner.invoke(app, ["vector", "search", "--symbol", "600000"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"


@pytest.mark.parametrize("value", ["0", "51", "abc"])
def test_search_rejects_an_invalid_top_k_with_the_standard_envelope(
    fake_service: type[_FakeService], value: str
) -> None:
    result = runner.invoke(app, ["vector", "search", "q", "--symbol", "600000", "--top-k", value])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["field"] == "top_k"
    assert payload["meta"]["trace_id"]


def test_search_passes_a_valid_top_k_through(fake_service: type[_FakeService]) -> None:
    result = runner.invoke(app, ["vector", "search", "q", "--symbol", "600000", "--top-k", "3"])

    assert result.exit_code == 0, result.stdout
    assert fake_service.instances[0].options["top_k"] == 3


def test_index_status_returns_an_envelope(fake_service: type[_FakeService]) -> None:
    fake_service.payload = {"name": "idx_ann_chunk_embedding", "exists": True}

    result = runner.invoke(app, ["vector", "index", "status"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["data"]["exists"] is True
    assert payload["meta"]["source"] == "vector"


def test_embed_partial_failure_exits_with_eight(fake_service: type[_FakeService]) -> None:
    fake_service.report = EmbedReport(
        model="BAAI/bge-m3",
        announcements=1,
        failed=1,
        chunks=2,
        written=1,
        inserted=1,
        missing=1,
    )

    result = runner.invoke(app, ["vector", "embed", "--batch-size", "32"])

    assert result.exit_code == 8
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_PARTIAL_FAILURE"
    assert payload["error"]["details"]["missing_chunks"] == 1
    assert fake_service.instances[0].kwargs["batch_size"] == 32


def test_database_errors_become_storage_errors(fake_service: type[_FakeService]) -> None:
    fake_service.error = OperationalError("SELECT 1", {}, Exception("connection refused"))

    result = runner.invoke(app, ["vector", "chunk"])

    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_STORAGE"
