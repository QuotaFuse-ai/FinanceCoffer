"""Configuration model: defaults, validation and redacted dumps."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from financecoffer.core.redaction import REDACTED, redact_text

SECRET_FIELDS: frozenset[str] = frozenset(
    {"embedding_api_key", "minio_access_key", "minio_secret_key", "mcp_token"}
)

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

    # Event ingestion
    event_consumer: Literal["redis", "kafka", "webhook"] = "redis"
    event_stream: str = "fc:stockdata:events"
    event_group: str = "fc-etl"

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

    @field_validator("embedding_url")
    @classmethod
    def _validate_embedding_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("FC_EMBEDDING_URL must be an http(s) URL")
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"FC_LOG_LEVEL must be one of {sorted(LOG_LEVELS)}")
        return level

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
