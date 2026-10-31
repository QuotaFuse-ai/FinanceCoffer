"""Configuration model: defaults, validation and redacted dumps."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
)

from financecoffer.core.redaction import REDACTED, redact_text

SECRET_FIELDS: frozenset[str] = frozenset(
    {"embedding_api_key", "minio_access_key", "minio_secret_key", "mcp_token"}
)

#: The literal older deployment templates used to mean "the operator has not
#: filled this in yet". A credential that keeps it would be a publicly known
#: secret, so it is refused on load instead of reaching a live deployment; the
#: current template ships empty values instead (DEC-0027) and this check stays
#: as a backstop for files written against the old convention. Only this exact
#: spelling is refused: look-alikes are plausible real secrets and stay accepted.
PLACEHOLDER_SECRET = "CHANGE_ME"

LOG_LEVELS: frozenset[str] = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})

_POSTGRES_URL = re.compile(r"^postgres(?:ql)?(?:\+[a-z0-9_]+)?://", re.IGNORECASE)
_REDIS_URL = re.compile(r"^rediss?://", re.IGNORECASE)


class Config(BaseModel):
    """Runtime configuration. Immutable once constructed."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    # Storage
    db_url: str | None = None
    redis_url: str = "redis://127.0.0.1:6379/1"

    # Embedding (DEC-0005: local bge-m3, 1024 dimensions)
    embedding_dim: int = Field(default=1024, gt=0)
    embedding_provider: Literal["local", "openai"] = "local"
    embedding_model: str = "BAAI/bge-m3"
    embedding_url: str = "http://127.0.0.1:8080/v1/embeddings"
    embedding_api_key: SecretStr | None = None

    # MinIO
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: SecretStr | None = None
    minio_secret_key: SecretStr | None = None
    minio_bucket: str = "financecoffer"

    # MinIO process lifecycle (DEC-0028). Autostart is off by default: a
    # deployed stack runs the store as its own service, and the archive degrades
    # without it rather than failing the read path. Turning it on makes a launch
    # ensure a loopback instance is up. The paths name a binary that is kept
    # outside the repository, so they carry no default -- an unset path is a
    # clear error only when something actually tries to start the server.
    minio_autostart: bool = False
    minio_binary: str = ""
    minio_data_dir: str = ""
    minio_log_file: str = ""
    minio_console_endpoint: str = ""

    # Event ingestion
    event_consumer: Literal["redis", "kafka", "webhook"] = "redis"
    event_stream: str = "fc:stockdata:events"
    event_group: str = "fc-etl"

    # ETL consumer. An empty ``etl_consumer_name`` means "use the hostname", so
    # a restart on the same host reclaims its own pending entries first.
    etl_consumer_name: str = ""
    etl_dlq_stream: str = "fc:stockdata:dlq"
    etl_control_key: str = "fc:etl:control"
    etl_batch_size: int = Field(default=50, ge=1, le=1000)
    etl_block_ms: int = Field(default=5000, ge=0, le=60_000)
    etl_max_attempts: int = Field(default=3, ge=1, le=100)
    etl_backoff_ms: int = Field(default=1000, ge=0, le=60_000)
    etl_claim_idle_ms: int = Field(default=60_000, ge=0)

    # DWS build-time default valuation basis. A build writes it into
    # ``metrics_json.valuation.default_basis`` unless ``--valuation-basis``
    # overrides it; the read side still lets each caller pick a basis per
    # request. The closed set is a ``Literal`` on purpose: an unknown value must
    # fail the load, because ``loader._extract_known`` would otherwise have to
    # guess between "typo" and "unset".
    metrics_profile: Literal["snapshot", "reference"] = "snapshot"

    # Compliance marking (DEC-0010). Off means the fields are not considered at
    # all: nothing is written to, read from or returned from the marking columns.
    compliance_enabled: bool = False

    # REST request limiting (S-07-2). In-process token bucket per client IP;
    # `FC_RATE_LIMIT_ENABLED=false` disables it for local debugging.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = Field(default=120, ge=1, le=10_000)

    # MCP / logging / archive
    mcp_token: SecretStr | None = None
    log_level: str = "INFO"
    cold_after_years: int = Field(default=3, ge=0)

    @field_validator("db_url")
    @classmethod
    def _validate_db_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not _POSTGRES_URL.match(value):
            raise ValueError("FC_DB_URL must use a postgresql:// or postgresql+psycopg:// DSN")
        return value

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, value: str) -> str:
        if not _REDIS_URL.match(value):
            raise ValueError("FC_REDIS_URL must use a redis:// or rediss:// URL")
        return value

    @field_validator(
        "embedding_api_key",
        "minio_access_key",
        "minio_secret_key",
        "mcp_token",
        mode="after",
    )
    @classmethod
    def _reject_placeholder_secret(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        """Refuse the template placeholder; an absent or empty secret is valid.

        ``None`` and the empty string are legitimate states, not "unfilled": a
        loopback-only deployment sets no MCP token and the ``local`` embedding
        provider needs no API key. Only the literal placeholder is rejected, and
        the message names the variable without echoing the value.
        """

        if value is None:
            return None
        if value.get_secret_value() != PLACEHOLDER_SECRET:
            return value
        raise ValueError(f"FC_{info.field_name.upper()} must not keep the CHANGE_ME placeholder")

    @field_validator("embedding_url")
    @classmethod
    def _validate_embedding_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("FC_EMBEDDING_URL must be an http(s) URL")
        return value

    @field_validator("metrics_profile", mode="before")
    @classmethod
    def _normalize_metrics_profile(cls, value: Any) -> Any:
        """Match the reader's tolerance for case and surrounding spaces."""

        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"FC_LOG_LEVEL must be one of {sorted(LOG_LEVELS)}")
        return level

    @field_validator("event_stream", "event_group", "etl_dlq_stream", "etl_control_key")
    @classmethod
    def _validate_redis_key(cls, value: str) -> str:
        if not value:
            raise ValueError("Redis stream/group/key names must not be empty")
        return value

    def redacted_dict(self) -> dict[str, Any]:
        """Return plain data with credentials masked, for ``fcoffer config show``."""

        data = self.model_dump(mode="json")
        for name in SECRET_FIELDS:
            if data.get(name) is not None:
                data[name] = REDACTED
        for name in ("db_url", "redis_url"):
            if data.get(name):
                data[name] = redact_text(str(data[name]))
        return data

    def __repr_args__(self) -> Any:
        """Repr and str carry the redacted view, never a DSN password.

        ``repr()`` is what an unhandled exception prints for every object in the
        traceback, and ``db_url`` is a plain ``str`` -- so without this the
        credential would reach a log or a crash report even though
        ``config show`` masks it.
        """

        shown = self.redacted_dict()
        return [(name, shown.get(name)) for name in type(self).model_fields]
