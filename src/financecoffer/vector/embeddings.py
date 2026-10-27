"""OpenAI-compatible batch embedding client for the local bge-m3 service.

One request carries at most :data:`MAX_BATCH_SIZE` (32) inputs; larger batches
are split so a slow CPU encoder never receives an unbounded request
(``docs/specs/cli.md`` section 3.10). Upstream 5xx and timeout failures are
retried a bounded number of times, then reported per input instead of aborting
the whole run: the caller counts them in ``fc_missing_chunk_total`` and keeps
processing other announcements.

Every vector is validated before it can reach PostgreSQL - finite components, a
non-zero norm and exactly ``FC_EMBEDDING_DIM`` values. Vectors are returned
L2-normalized so stored and query vectors share one scale.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from financecoffer.core.config import Config
from financecoffer.core.errors import DependencyError, InvalidArgumentError
from financecoffer.core.redaction import redact_text

MAX_BATCH_SIZE = 32
DEFAULT_BATCH_SIZE = 32
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF = 0.25

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def bounded_batch_size(value: int) -> int:
    """Clamp ``value`` to ``1..32``; larger requests mean more, smaller batches."""

    if value < 1:
        raise InvalidArgumentError(
            "batch_size must be at least 1",
            details={"field": "batch_size", "value": value},
        )
    return min(int(value), MAX_BATCH_SIZE)


@dataclass(frozen=True)
class EmbeddingOutcome:
    """Per-input vectors (``None`` means the input failed) plus failure detail."""

    vectors: tuple[list[float] | None, ...] = ()
    requests: int = 0
    failures: int = 0
    reasons: Mapping[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failures == 0


class EmbeddingClient:
    """Batched client for an OpenAI-compatible ``/v1/embeddings`` endpoint."""

    def __init__(
        self,
        config: Config,
        *,
        timeout: int = 30,
        batch_size: int = DEFAULT_BATCH_SIZE,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        model: str | None = None,
        transport: object | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self.batch_size = bounded_batch_size(batch_size)
        self.retries = max(int(retries), 0)
        self.backoff = max(float(backoff), 0.0)
        self.model = model or config.embedding_model
        self.transport = transport
        self._sleep = sleeper

    def embed(self, texts: Sequence[str]) -> EmbeddingOutcome:
        """Embed every input, one bounded request at a time."""

        if not texts:
            return EmbeddingOutcome()

        vectors: list[list[float] | None] = []
        reasons: dict[str, int] = {}
        errors: list[str] = []
        requests = 0

        with self._client() as client:
            for offset in range(0, len(texts), self.batch_size):
                batch = list(texts[offset : offset + self.batch_size])
                requests += 1
                parsed, reason, error = self._request(client, batch)
                vectors.extend(parsed)
                if reason is not None:
                    failed = sum(1 for item in parsed if item is None)
                    reasons[reason] = reasons.get(reason, 0) + failed
                if error is not None and error not in errors:
                    errors.append(error)

        failures = sum(1 for item in vectors if item is None)
        return EmbeddingOutcome(
            vectors=tuple(vectors),
            requests=requests,
            failures=failures,
            reasons=reasons,
            errors=tuple(errors),
        )

    def embed_query(self, text: str) -> list[float]:
        """Embed one search query; any failure is a hard ``E_DEPENDENCY``."""

        outcome = self.embed([text])
        vector = outcome.vectors[0] if outcome.vectors else None
        if vector is None:
            detail = outcome.errors[0] if outcome.errors else "no vector returned"
            raise DependencyError(
                "embedding service is unavailable",
                details={"url": self.config.embedding_url, "error": detail},
            )
        return vector

    # -- internals ----------------------------------------------------------

    def _client(self) -> object:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise DependencyError(
                "httpx is required for embeddings",
                details={"hint": "install financecoffer[vector]"},
            ) from exc
        host = urlsplit(self.config.embedding_url).hostname or ""
        return httpx.Client(
            timeout=self.timeout,
            trust_env=host not in _LOOPBACK_HOSTS,
            transport=self.transport,  # type: ignore[arg-type]
        )

    def _request(
        self, client: object, batch: list[str]
    ) -> tuple[list[list[float] | None], str | None, str | None]:
        """Embed one batch; return vectors, a failure reason and an error text."""

        import httpx

        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                response = client.post(  # type: ignore[attr-defined]
                    self.config.embedding_url,
                    json={"model": self.model, "input": batch},
                    headers=self._headers(),
                )
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                last_error = redact_text(f"HTTP {status}")
                if status < 500:
                    break
            except httpx.HTTPError as exc:
                last_error = redact_text(str(exc).strip() or type(exc).__name__)
            except ValueError:
                last_error = "embedding service returned invalid JSON"
                break
            else:
                parsed, problem = self._parse(body, len(batch))
                if problem is not None:
                    last_error = problem
                    break
                reason = "invalid_vector" if any(item is None for item in parsed) else None
                return parsed, reason, None

            if attempt < self.retries:
                self._sleep(self.backoff * (2**attempt))

        return [None] * len(batch), "embedding_unavailable", last_error

    def _headers(self) -> dict[str, str]:
        key = self.config.embedding_api_key
        if key is None:
            return {}
        return {"Authorization": f"Bearer {key.get_secret_value()}"}

    def _parse(
        self, body: object, expected: int
    ) -> tuple[list[list[float] | None], str | None]:
        if not isinstance(body, Mapping):
            return [None] * expected, "embedding service returned an unexpected payload"
        items = body.get("data")
        if not isinstance(items, list) or len(items) != expected:
            length = len(items) if isinstance(items, list) else "no"
            return [None] * expected, f"expected {expected} embeddings, got {length}"

        vectors: list[list[float] | None] = [None] * expected
        for position, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            try:
                index = int(item.get("index", position))
                values = [float(value) for value in item["embedding"]]  # type: ignore[union-attr]
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if 0 <= index < expected:
                vectors[index] = self._validate(values)
        return vectors, None

    def _validate(self, values: list[float]) -> list[float] | None:
        if len(values) != self.config.embedding_dim:
            return None
        if not all(math.isfinite(value) for value in values):
            return None
        norm = math.sqrt(math.fsum(value * value for value in values))
        if not math.isfinite(norm) or norm == 0.0:
            return None
        return [value / norm for value in values]


__all__ = [
    "DEFAULT_BACKOFF",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_RETRIES",
    "MAX_BATCH_SIZE",
    "EmbeddingClient",
    "EmbeddingOutcome",
    "bounded_batch_size",
]
