"""Event contract validation (T-004).

Positive samples for the five payloads, precision and error-shape checks, and the
DEC-0010 compliance switch in both positions. ``ingest event --validate-only``
is exercised end to end because it is the documented contract-check entry point.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from typer.testing import CliRunner

from financecoffer import events
from financecoffer.cli import app
from financecoffer.core.errors import InvalidArgumentError, NotImplementedYetError
from financecoffer.events import models, validation

PROVIDER = "eastmoney"
SYMBOL = "600000"
BIZ_KEY = "2026Q3-600000"
EVENT_ID = "evt_b9cf5f8f48dcab33e59c5095cc81bc19"

CONTENT: dict[str, dict[str, Any]] = {
    "financial": {
        "quality_metrics": {"quality_status": "normal", "confidence": "high"},
        "structured_fields": {
            "report_date": "2026-09-30",
            "report_type": "Q3",
            "amount_revenue": "1234567890123.4567",
            "amount_net_profit": "-5.5000",
        },
    },
    "announcement": {
        "quality_metrics": {"quality_status": "normal"},
        "raw_html": "<p>Q3 revenue grew</p>",
        "clean_content": "Q3 revenue grew",
        "structured_fields": {"title": "Q3 earnings"},
    },
    "kline": {
        "quality_metrics": {"quality_status": "normal"},
        "structured_fields": {
            "trade_date": "2026-10-20",
            "open": "10.01",
            "high": "12.50",
            "low": "9.99",
            "close": "11.11",
            "volume": "1000000",
            "amount": "11110000",
            "adjust": "qfq",
        },
    },
    "quote": {
        "quality_metrics": {"quality_status": "normal"},
        "structured_fields": {
            "trade_date": "2026-10-20",
            "close": "11.11",
            "total_market_cap": "123456789.01",
            "pe_ttm": "15.2",
        },
    },
    "risk": {
        "quality_metrics": {"quality_status": "suspicious"},
        "structured_fields": {
            "risk_type": "litigation",
            "occur_date": "2026-10-01",
            "severity": "high",
            "summary": "material lawsuit disclosed",
        },
    },
    "research": {},
}


def payload(data_type: str = "financial", **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": "1.0",
        "event_id": events.compute_event_id(
            provider=PROVIDER, data_type=data_type, symbol=SYMBOL, biz_key=BIZ_KEY
        ),
        "biz_key": BIZ_KEY,
        "event_time": "2026-10-27T10:00:00+08:00",
        "data_type": data_type,
        "symbol": SYMBOL,
        "market": "CN",
        "source_info": {
            "provider": PROVIDER,
            "crawl_url": "https://example.com/ann/1",
            "crawl_time": "2026-10-27T02:00:00Z",
        },
        "content": deepcopy(CONTENT[data_type]),
        "trace_id": "trace-t004",
    }
    event.update(overrides)
    return event


def problems(event: dict[str, Any], **kwargs: Any) -> tuple[list[dict[str, str]], Any]:
    with pytest.raises(InvalidArgumentError) as caught:
        events.validate_event(event, **kwargs)
    assert caught.value.details is not None
    return caught.value.details["errors"], caught.value.details


def test_compute_event_id_is_the_contract_sha256() -> None:
    assert (
        events.compute_event_id(
            provider=PROVIDER, data_type="financial", symbol=SYMBOL, biz_key=BIZ_KEY
        )
        == EVENT_ID
    )
    assert (
        events.compute_event_id(
            provider=PROVIDER,
            data_type=models.EventDataType.FINANCIAL,
            symbol=SYMBOL,
            biz_key=BIZ_KEY,
        )
        == EVENT_ID
    )
    # A null symbol folds to the empty string, exactly as the contract says.
    assert (
        events.compute_event_id(
            provider=PROVIDER, data_type="northbound", symbol=None, biz_key=BIZ_KEY
        )
        == events.compute_event_id(
            provider=PROVIDER, data_type="northbound", symbol="", biz_key=BIZ_KEY
        )
    )


@pytest.mark.parametrize("data_type", ["financial", "announcement", "kline", "quote", "risk"])
def test_valid_payloads_are_normalized(data_type: str) -> None:
    event = events.validate_event(payload(data_type))

    assert event.data_type.value == data_type
    assert event.event_id == payload(data_type)["event_id"]
    assert event.provider == PROVIDER
    assert event.symbol == SYMBOL
    assert event.event_time == datetime(2026, 10, 27, 2, 0, tzinfo=UTC)
    assert event.summary()["event_time"] == "2026-10-27T02:00:00Z"
    assert event.summary()["quality_status"] in {"normal", "suspicious"}
    assert event.compliance is None


def test_amounts_stay_exact_decimals() -> None:
    event = events.validate_event(payload())
    fields = event.content.structured_fields  # type: ignore[attr-defined]

    assert isinstance(fields.amount_revenue, Decimal)
    assert fields.amount_revenue == Decimal("1234567890123.4567")
    assert fields.amount_net_profit == Decimal("-5.5000")


def test_quality_status_is_required_and_never_defaulted() -> None:
    event = payload()
    del event["content"]["quality_metrics"]["quality_status"]

    errors, details = problems(event)

    assert details["reason"] == "schema"
    assert any(
        error["field"] == "content.quality_metrics.quality_status"
        and "required" in error["reason"]
        for error in errors
    )


def test_json_numbers_are_rejected_for_money_fields() -> None:
    event = payload()
    event["content"]["structured_fields"]["amount_revenue"] = 123.45

    errors, _ = problems(event)

    assert errors[0]["field"] == "content.structured_fields.amount_revenue"
    assert "decimal string" in errors[0]["reason"]
    assert not errors[0]["field"].startswith("body.")


def test_extra_amount_fields_must_also_be_strings() -> None:
    event = payload()
    event["content"]["structured_fields"]["amount_eps"] = 0.5

    errors, _ = problems(event)

    assert any(
        "structured_fields.amount_eps must be a decimal string" in error["reason"]
        for error in errors
    )


def test_unknown_schema_version_is_rejected() -> None:
    errors, _ = problems(payload(schema_version="2.0"))

    assert errors[0]["field"] == "schema_version"
    assert "unsupported schema_version" in errors[0]["reason"]


def test_event_id_must_match_the_deterministic_digest() -> None:
    errors, details = problems(payload(event_id="evt_deliberately_wrong"))

    assert details["reason"] == "event_id"
    assert [error["field"] for error in errors] == ["event_id"]
    assert "sha256" in errors[0]["reason"]


@pytest.mark.parametrize(
    ("symbol", "needle"),
    [
        (None, "symbol is required"),
        ("60000", "6-digit A-share code"),
        ("600000 ", "canonical form"),
    ],
)
def test_symbols_must_be_present_and_canonical(symbol: str | None, needle: str) -> None:
    errors, _ = problems(payload(symbol=symbol))

    assert errors[0]["field"] == "symbol"
    assert needle in errors[0]["reason"]


def test_oversized_payload_requires_a_minio_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation, "MAX_EVENT_BYTES", 256)
    event = payload("announcement")
    event["content"]["raw_html"] = "<p>" + "x" * 512 + "</p>"

    errors, _ = problems(event)

    assert errors[0]["field"] == "content"
    assert "object_ref" in errors[0]["reason"]

    event["content"]["object_ref"] = "minio://financecoffer/ann/600000.html"
    assert events.validate_event(event).event_id == event["event_id"]


def test_announcement_requires_a_body() -> None:
    event = payload("announcement")
    event["content"] = {"quality_metrics": {"quality_status": "normal"}}

    errors, _ = problems(event)

    assert errors[0]["field"] == "content"
    assert "raw_html or clean_content" in errors[0]["reason"]


def test_unmodelled_data_types_are_reported_as_not_implemented() -> None:
    with pytest.raises(NotImplementedYetError) as caught:
        events.validate_event(payload("research"))

    assert caught.value.details["data_type"] == "research"


def test_compliance_fields_are_ignored_when_the_switch_is_off() -> None:
    event = payload()
    event["source_info"].update({"compliance_tier": "not-a-tier", "commercial_use": "yes"})

    validated = events.validate_event(event, compliance_enabled=False)

    assert validated.compliance is None
    assert "compliance" not in validated.summary()


def test_compliance_mark_is_returned_when_enabled() -> None:
    event = payload()
    event["source_info"].update({"compliance_tier": "s", "commercial_use": True})

    validated = events.validate_event(event, compliance_enabled=True)

    assert validated.compliance is not None
    assert validated.compliance.as_dict() == {"compliance_tier": "S", "commercial_use": True}
    assert validated.summary()["compliance"] == {
        "compliance_tier": "S",
        "commercial_use": True,
    }


def test_enabled_compliance_rejects_a_missing_tier() -> None:
    event = payload()
    event["source_info"]["commercial_use"] = True

    errors, details = problems(event, compliance_enabled=True)

    assert details["reason"] == "compliance"
    assert errors[0]["field"] == "source_info.compliance_tier"


def test_enabled_compliance_rejects_a_non_boolean_flag() -> None:
    event = payload()
    event["source_info"].update({"compliance_tier": "S", "commercial_use": "true"})

    errors, details = problems(event, compliance_enabled=True)

    assert details["reason"] == "compliance"
    assert errors[0]["field"] == "source_info.commercial_use"


def test_ingest_event_validate_only_emits_the_normalized_event(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FC_COMPLIANCE_ENABLED", raising=False)
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(payload()), encoding="utf-8")

    result = CliRunner().invoke(
        app, ["ingest", "event", "--file", str(event_file), "--validate-only"]
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert body["data"]["valid"] is True
    assert body["data"]["event_id"] == EVENT_ID
    assert body["data"]["quality_status"] == "normal"
    assert body["meta"]["source"] == "etl"


def test_ingest_event_reports_contract_violations(tmp_path: Any) -> None:
    invalid = payload()
    del invalid["content"]["quality_metrics"]["quality_status"]
    event_file = tmp_path / "bad.json"
    event_file.write_text(json.dumps(invalid), encoding="utf-8")

    result = CliRunner().invoke(
        app, ["ingest", "event", "--file", str(event_file), "--validate-only"]
    )

    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert body["error"]["code"] == "E_INVALID_ARGUMENT"
    assert body["error"]["details"]["errors"][0]["field"].startswith("content.")
