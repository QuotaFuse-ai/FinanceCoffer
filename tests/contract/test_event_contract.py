"""Contract tests for ``docs/specs/event-contract.md`` (v0.3).

Every example event from section 3 must validate, the three negative buckets
(missing field / wrong type / unknown ``data_type``) must map to the documented
error code and reason, and ``event_id`` must obey the deterministic
``sha256(provider|data_type|symbol|biz_key)`` rule. The examples for
``research`` / ``northbound`` / ``heartbeat`` only have envelope + content
documented; their payload models have not landed yet, so validation must report
``E_NOT_IMPLEMENTED`` instead of guessing (contract section 7, gap G-02).
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest

from financecoffer.core.errors import InvalidArgumentError, NotImplementedYetError
from financecoffer.events import EventEnvelope, compute_event_id, validate_event
from financecoffer.events.models import MAX_EVENT_BYTES

_EVENT_ID_PATTERN = re.compile(r"^evt_[0-9a-f]{32}$")


def _event(
    *,
    data_type: str,
    symbol: str | None,
    provider: str,
    event_time: str,
    biz_key: str,
    content: dict[str, Any],
    market: str = "CN",
) -> dict[str, Any]:
    """Build the full envelope around a documented section-3 ``content`` block."""

    return {
        "schema_version": "1.0",
        "event_id": compute_event_id(
            provider=provider,
            data_type=data_type,
            symbol=symbol,
            biz_key=biz_key,
        ),
        "biz_key": biz_key,
        "event_time": event_time,
        "data_type": data_type,
        "symbol": symbol,
        "market": market,
        "source_info": {"provider": provider},
        "content": content,
    }


# -- section 3 examples -------------------------------------------------------


def _announcement() -> dict[str, Any]:
    return _event(
        data_type="announcement",
        symbol="600000",
        provider="eastmoney",
        event_time="2023-10-25T10:00:00Z",
        biz_key="ann-2023-1024-001",
        content={
            "raw_html": "<html><body>2023年第三季度报告</body></html>",
            "clean_content": "公司前三季度经营情况说明。",
            "structured_fields": {"ann_type": "解禁", "publish_date": "2023-10-24"},
            "quality_metrics": {"confidence": "high", "quality_status": "normal"},
        },
    )


def _financial() -> dict[str, Any]:
    return _event(
        data_type="financial",
        symbol="600000",
        provider="eastmoney",
        event_time="2023-10-25T10:00:00Z",
        biz_key="2023-09-30|Q3",
        content={
            "raw_html": "<html><body>2023 三季报</body></html>",
            "structured_fields": {
                "report_date": "2023-09-30",
                "report_type": "Q3",
                "amount_revenue": "123456789.0000",
                "amount_net_profit": "9876543.2100",
            },
            "quality_metrics": {
                "confidence": "high",
                "quality_status": "normal",
                "balance_check_result": {"assets": True, "liabilities": True},
            },
        },
    )


def _kline() -> dict[str, Any]:
    return _event(
        data_type="kline",
        symbol="600000",
        provider="eastmoney",
        event_time="2023-10-24T15:00:00Z",
        biz_key="2023-10-24|day|none",
        content={
            "structured_fields": {
                "trade_date": "2023-10-24",
                "open": "7.1200",
                "high": "7.3000",
                "low": "7.0500",
                "close": "7.2500",
                "volume": "12345600",
                "amount": "89000000.0000",
                "adjust": "none",
            },
            "quality_metrics": {"confidence": "high", "quality_status": "normal"},
        },
    )


def _quote() -> dict[str, Any]:
    return _event(
        data_type="quote",
        symbol="600000",
        provider="eastmoney",
        event_time="2023-10-24T15:00:00Z",
        biz_key="2023-10-24|15:00:00",
        content={
            "structured_fields": {
                "trade_date": "2023-10-24",
                "close": "7.2500",
                "total_market_cap": "212000000000.0000",
                "pe_ttm": "5.1200",
            },
            "quality_metrics": {"confidence": "high", "quality_status": "normal"},
        },
    )


def _risk() -> dict[str, Any]:
    return _event(
        data_type="risk",
        symbol="600000",
        provider="eastmoney",
        event_time="2023-04-28T18:00:00Z",
        biz_key="2023-04-28|goodwill_impairment|dwd:stock_announcement:12345",
        content={
            "structured_fields": {
                "risk_type": "goodwill_impairment",
                "occur_date": "2023-04-28",
                "severity": "high",
                "summary": "计提商誉减值 3.2 亿元",
            },
            "source_announcement_ref": "dwd:stock_announcement:12345",
            "quality_metrics": {"confidence": "medium", "quality_status": "suspicious"},
        },
    )


def _research() -> dict[str, Any]:
    return _event(
        data_type="research",
        symbol="600000",
        provider="eastmoney",
        event_time="2026-10-24T08:00:00Z",
        biz_key="R20261024001",
        content={
            "structured_fields": {
                "report_id": "R20261024001",
                "title": "三季度点评：息差企稳",
                "institution": "某证券",
                "analyst": "张三",
                "rating": "买入",
                "target_price": "12.5000",
                "publish_date": "2026-10-24",
                "pdf_url": "https://example.com/report.pdf",
            },
            "quality_metrics": {"confidence": "medium", "quality_status": "normal"},
        },
    )


def _northbound() -> dict[str, Any]:
    return _event(
        data_type="northbound",
        symbol=None,
        provider="eastmoney_datacenter",
        event_time="2026-10-24T08:00:00Z",
        biz_key="2026-10-24|沪股通",
        content={
            "structured_fields": {
                "trade_date": "2026-10-24",
                "channel": "沪股通",
                "net_buy_amount": "123456789.0000",
                "buy_amount": "987654321.0000",
                "sell_amount": "864197532.0000",
                "currency": "CNY",
            },
            "quality_metrics": {"confidence": "high", "quality_status": "normal"},
        },
    )


def _heartbeat() -> dict[str, Any]:
    return _event(
        data_type="heartbeat",
        symbol=None,
        provider="tickhub-cn",
        event_time="2026-10-26T07:35:12Z",
        biz_key="cn-20261026-1535",
        content={
            "structured_fields": {
                "run_id": "cn-20261026-1535",
                "status": "ok",
                "items_collected": 128,
                "started_at": "2026-10-26T07:30:00Z",
                "finished_at": "2026-10-26T07:35:10Z",
                "next_run_at": "2026-10-26T13:00:00Z",
            },
            "quality_metrics": {"confidence": "high", "quality_status": "normal"},
        },
    )


SUPPORTED_EXAMPLES = {
    "announcement": (_announcement, "normal"),
    "financial": (_financial, "normal"),
    "kline": (_kline, "normal"),
    "quote": (_quote, "normal"),
    "risk": (_risk, "suspicious"),
}

PENDING_EXAMPLES = {
    "research": _research,
    "northbound": _northbound,
    "heartbeat": _heartbeat,
}


@pytest.mark.parametrize("name", sorted(SUPPORTED_EXAMPLES))
def test_documented_examples_pass_validation(name: str) -> None:
    build, quality = SUPPORTED_EXAMPLES[name]
    payload = build()

    event = validate_event(payload)

    assert event.data_type.value == name
    assert event.event_id == payload["event_id"]
    assert event.biz_key == payload["biz_key"]
    assert event.symbol == payload["symbol"]
    assert event.content.quality_metrics.quality_status.value == quality


@pytest.mark.parametrize("name", sorted(PENDING_EXAMPLES))
def test_pending_examples_are_envelope_valid_and_report_not_implemented(name: str) -> None:
    payload = PENDING_EXAMPLES[name]()

    envelope = EventEnvelope.model_validate(payload)
    assert envelope.data_type.value == name
    assert envelope.event_id == compute_event_id(
        provider=envelope.source_info.provider,
        data_type=envelope.data_type,
        symbol=envelope.symbol,
        biz_key=envelope.biz_key,
    )

    with pytest.raises(NotImplementedYetError) as raised:
        validate_event(payload)

    assert raised.value.code.value == "E_NOT_IMPLEMENTED"
    assert raised.value.details["data_type"] == name


# -- negative cases: missing field / wrong type / unknown data_type ----------


@pytest.mark.parametrize(
    "field",
    ["schema_version", "event_id", "biz_key", "event_time", "data_type", "market", "source_info", "content"],
)
def test_a_missing_envelope_field_is_a_schema_error(field: str) -> None:
    payload = _announcement()
    payload.pop(field)

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.code.value == "E_INVALID_ARGUMENT"
    assert raised.value.details["reason"] == "schema"
    assert field in {problem["field"] for problem in raised.value.details["errors"]}


def test_a_null_symbol_is_a_schema_error_for_data_events() -> None:
    payload = _announcement()
    payload["symbol"] = None

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.details["reason"] == "schema"
    assert raised.value.details["errors"] == [
        {"field": "symbol", "reason": "symbol is required for data_type=announcement"}
    ]


def test_amounts_sent_as_json_numbers_are_rejected() -> None:
    payload = _financial()
    payload["content"]["structured_fields"]["amount_revenue"] = 123456789.0

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.details["reason"] == "schema"
    assert "content.structured_fields.amount_revenue" in {
        problem["field"] for problem in raised.value.details["errors"]
    }
    assert "JSON numbers are rejected" in " ".join(
        problem["reason"] for problem in raised.value.details["errors"]
    )


def test_prices_sent_as_json_numbers_are_rejected() -> None:
    payload = _kline()
    payload["content"]["structured_fields"]["open"] = 7.12

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.details["reason"] == "schema"
    assert "content.structured_fields.open" in {
        problem["field"] for problem in raised.value.details["errors"]
    }


def test_an_unknown_quality_status_is_not_defaulted() -> None:
    payload = _announcement()
    payload["content"]["quality_metrics"]["quality_status"] = "excellent"

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.details["reason"] == "schema"
    assert "content.quality_metrics.quality_status" in {
        problem["field"] for problem in raised.value.details["errors"]
    }


def test_a_missing_quality_status_is_not_invented() -> None:
    payload = _announcement()
    payload["content"]["quality_metrics"].pop("quality_status")

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.details["reason"] == "schema"
    assert "content.quality_metrics.quality_status" in {
        problem["field"] for problem in raised.value.details["errors"]
    }


def test_event_time_without_a_timezone_is_rejected() -> None:
    payload = _announcement()
    payload["event_time"] = "2023-10-25T10:00:00"

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.details["reason"] == "schema"
    assert "event_time" in {problem["field"] for problem in raised.value.details["errors"]}


def test_an_unknown_data_type_is_a_schema_error() -> None:
    payload = _announcement()
    payload["data_type"] = "sentiment"

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.code.value == "E_INVALID_ARGUMENT"
    assert raised.value.details["reason"] == "schema"
    assert "data_type" in {problem["field"] for problem in raised.value.details["errors"]}


def test_an_unknown_schema_version_is_rejected_not_guessed() -> None:
    payload = _announcement()
    payload["schema_version"] = "2.0"

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.details["reason"] == "schema"
    assert "schema_version" in {problem["field"] for problem in raised.value.details["errors"]}


def test_a_non_canonical_symbol_is_rejected() -> None:
    payload = _event(
        data_type="announcement",
        symbol="aapl",
        provider="sec_edgar",
        event_time="2023-10-25T10:00:00Z",
        biz_key="ann-us-001",
        content={
            "clean_content": "10-K filing.",
            "quality_metrics": {"confidence": "high", "quality_status": "normal"},
        },
        market="US",
    )

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.details["reason"] == "schema"
    assert raised.value.details["errors"][0]["field"] == "symbol"
    assert "AAPL" in raised.value.details["errors"][0]["reason"]


def test_a_mismatched_event_id_is_an_event_id_error() -> None:
    payload = _announcement()
    payload["event_id"] = "evt_" + "0" * 32

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.code.value == "E_INVALID_ARGUMENT"
    assert raised.value.details["reason"] == "event_id"
    assert raised.value.details["errors"][0]["field"] == "event_id"


def test_an_oversized_event_requires_a_minio_reference() -> None:
    payload = _announcement()
    payload["content"]["clean_content"] = "x" * MAX_EVENT_BYTES

    with pytest.raises(InvalidArgumentError) as raised:
        validate_event(payload)

    assert raised.value.details["reason"] == "schema"
    assert "content" in {problem["field"] for problem in raised.value.details["errors"]}

    payload["content"]["structured_fields"]["object_ref"] = (
        "minio://financecoffer/raw/ann-2023-1024-001.html"
    )

    event = validate_event(payload)
    assert event.event_id == payload["event_id"]


# -- event_id rule -----------------------------------------------------------


def test_event_id_is_a_stable_sha256_prefix() -> None:
    first = compute_event_id(
        provider="eastmoney", data_type="announcement", symbol="600000", biz_key="ann-1"
    )
    second = compute_event_id(
        provider="eastmoney", data_type="announcement", symbol="600000", biz_key="ann-1"
    )

    assert first == second
    assert _EVENT_ID_PATTERN.fullmatch(first)


def test_event_id_uses_an_empty_string_for_a_null_symbol() -> None:
    assert compute_event_id(
        provider="eastmoney", data_type="northbound", symbol=None, biz_key="2026-10-24|沪股通"
    ) == compute_event_id(
        provider="eastmoney", data_type="northbound", symbol="", biz_key="2026-10-24|沪股通"
    )


def test_event_id_changes_with_every_participating_field() -> None:
    base = {
        "provider": "eastmoney",
        "data_type": "announcement",
        "symbol": "600000",
        "biz_key": "ann-1",
    }
    ids = {
        compute_event_id(**base),
        compute_event_id(**{**base, "provider": "sec_edgar"}),
        compute_event_id(**{**base, "data_type": "financial"}),
        compute_event_id(**{**base, "symbol": "600519"}),
        compute_event_id(**{**base, "biz_key": "ann-2"}),
    }

    assert len(ids) == 5


def test_a_resent_business_fact_keeps_the_same_event_id() -> None:
    payload = _announcement()
    resend = copy.deepcopy(payload)
    resend["trace_id"] = "retry-2"
    resend["event_time"] = "2023-10-26T00:00:00Z"

    assert validate_event(resend).event_id == validate_event(payload).event_id


# -- validation is a check, not a rewrite ------------------------------------


def test_validation_returns_the_payload_untouched() -> None:
    payload = _financial()
    snapshot = copy.deepcopy(payload)

    event = validate_event(payload)

    assert event.raw == snapshot
    assert snapshot == payload
    assert "version" not in event.content.structured_fields.model_dump()


def test_a_bad_quality_status_is_stored_not_rejected() -> None:
    payload = _financial()
    payload["content"]["quality_metrics"]["quality_status"] = "bad"

    event = validate_event(payload)

    assert event.content.quality_metrics.quality_status.value == "bad"


def test_event_time_is_normalized_to_utc() -> None:
    payload = _announcement()
    payload["event_time"] = "2023-10-25T18:00:00+08:00"

    event = validate_event(payload)

    assert event.event_time.isoformat() == "2023-10-25T10:00:00+00:00"
