"""Unified response envelope shared by CLI, REST and MCP.

Field names follow ``docs/specs/cli.md`` section 3.12. Success and failure
share the same top-level shape (``ok`` / ``data`` / ``meta`` / ``error``) so
clients can parse one schema:

* success: ``ok=true, data=<payload>, error=null``
* failure: ``ok=false, data=null, error={code, message, details}``

``meta.trace_id`` is always present and is the only field besides ``ok`` that
callers should rely on across every response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from financecoffer.core.errors import FinanceCofferError
from financecoffer.core.trace import new_trace_id, normalize_trace_id


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Meta(BaseModel):
    """Response metadata; every field except ``fetched_at``/``trace_id`` may be null."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, description="dws | dwd | vector | ...")
    fetched_at: datetime = Field(default_factory=_utc_now)
    quality_status: str | None = None
    confidence: str | None = None
    compliance_tier: str | None = None
    trace_id: str = Field(default_factory=new_trace_id, min_length=1, max_length=128)

    @field_serializer("fetched_at")
    def _serialize_fetched_at(self, value: datetime) -> str:
        return _format_timestamp(value)


class ErrorBody(BaseModel):
    """Stable error payload embedded in a failure envelope."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^E_[A-Z0-9_]{2,48}$")
    message: str = Field(min_length=1)
    details: dict[str, Any] | None = None


class Envelope(BaseModel):
    """Response envelope; see the module docstring for the contract."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: Any = None
    meta: Meta = Field(default_factory=Meta)
    error: ErrorBody | None = None

    @model_validator(mode="after")
    def _check_error_consistency(self) -> Envelope:
        if self.ok and self.error is not None:
            raise ValueError("a successful envelope must not carry an error")
        if not self.ok and self.error is None:
            raise ValueError("a failed envelope must carry an error")
        return self

    @classmethod
    def success(
        cls,
        data: Any = None,
        *,
        meta: Meta | None = None,
        trace_id: str | None = None,
    ) -> Envelope:
        response_meta = meta.model_copy() if meta is not None else Meta()
        if trace_id is not None:
            response_meta = response_meta.model_copy(
                update={"trace_id": normalize_trace_id(trace_id)}
            )
        return cls(ok=True, data=data, meta=response_meta, error=None)

    @classmethod
    def from_error(
        cls,
        error: FinanceCofferError,
        *,
        meta: Meta | None = None,
        trace_id: str | None = None,
    ) -> Envelope:
        response_meta = meta.model_copy() if meta is not None else Meta()
        if trace_id is not None:
            response_meta = response_meta.model_copy(
                update={"trace_id": normalize_trace_id(trace_id)}
            )
        return cls(
            ok=False,
            data=None,
            meta=response_meta,
            error=ErrorBody(
                code=error.code.value,
                message=error.message,
                details=error.details,
            ),
        )
