import json

import pytest
from pydantic import ValidationError

from financecoffer.core.envelope import Envelope, ErrorBody, Meta
from financecoffer.core.errors import InvalidArgumentError


def test_success_envelope_is_isomorphic_with_failure() -> None:
    envelope = Envelope.success({"revenue": "123.45"})
    payload = json.loads(envelope.model_dump_json())

    assert set(payload) == {"ok", "data", "meta", "error"}
    assert payload["ok"] is True
    assert payload["data"] == {"revenue": "123.45"}
    assert payload["error"] is None
    assert payload["meta"]["trace_id"]


def test_failure_envelope_carries_stable_code_and_details() -> None:
    error = InvalidArgumentError("symbol is required", details={"param": "symbol"})
    envelope = Envelope.from_error(error)
    payload = json.loads(envelope.model_dump_json())

    assert set(payload) == {"ok", "data", "meta", "error"}
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"] == {
        "code": "E_INVALID_ARGUMENT",
        "message": "symbol is required",
        "details": {"param": "symbol"},
    }
    assert payload["meta"]["trace_id"]


def test_meta_serializes_timestamp_with_utc_suffix() -> None:
    payload = json.loads(Envelope.success([]).model_dump_json())

    assert payload["meta"]["fetched_at"].endswith("Z")
    assert payload["meta"]["quality_status"] is None
    assert payload["meta"]["confidence"] is None
    assert payload["meta"]["compliance_tier"] is None


def test_trace_id_is_propagated_and_normalized() -> None:
    envelope = Envelope.success(None, trace_id="trace-abc.123")
    assert envelope.meta.trace_id == "trace-abc.123"

    blank = Envelope.success(None, trace_id="  ")
    assert blank.meta.trace_id != ""


def test_meta_accepts_quality_fields() -> None:
    envelope = Envelope.success(
        {"x": 1},
        meta=Meta(
            source="dwd",
            quality_status="normal",
            confidence="high",
            compliance_tier="B",
        ),
    )

    assert envelope.meta.source == "dwd"
    assert envelope.meta.quality_status == "normal"
    assert envelope.meta.confidence == "high"
    assert envelope.meta.compliance_tier == "B"


def test_success_envelope_must_not_carry_error() -> None:
    with pytest.raises(ValidationError):
        Envelope(ok=True, data={}, error=ErrorBody(code="E_INTERNAL", message="boom"))


def test_failure_envelope_must_carry_error() -> None:
    with pytest.raises(ValidationError):
        Envelope(ok=False, data=None)


def test_error_code_pattern_is_enforced() -> None:
    with pytest.raises(ValidationError):
        ErrorBody(code="NOT_A_CODE", message="boom")
