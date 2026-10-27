"""DWD writer unit tests (T-006): dispatch, dedupe material and precision guards.

Only the pure helpers and the dispatch table are exercised here; the repository
and constraint behaviour is covered against a real PostgreSQL in
``tests/integration/test_dwd_integration.py``.
"""

from __future__ import annotations

import ast
import hashlib
import re
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from financecoffer import events
from financecoffer.core.errors import InvalidArgumentError
from financecoffer.dwd import WriteOutcome, write_event, writer
from financecoffer.events.models import AnnouncementContent, EventDataType, QualityStatus

PROVIDER = "eastmoney"
SYMBOL = "600000"
BIZ_KEY = "2026Q3-600000"

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
        "structured_fields": {"title": "Q3 earnings", "ann_type": "periodic"},
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
}


def payload(
    data_type: str = "financial",
    *,
    biz_key: str = BIZ_KEY,
    content: dict[str, Any] | None = None,
    source_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": "1.0",
        "event_id": events.compute_event_id(
            provider=PROVIDER, data_type=data_type, symbol=SYMBOL, biz_key=biz_key
        ),
        "biz_key": biz_key,
        "event_time": "2026-10-27T10:00:00+08:00",
        "data_type": data_type,
        "symbol": SYMBOL,
        "market": "CN",
        "source_info": {"provider": PROVIDER, "crawl_time": "2026-10-27T02:00:00Z"},
        "content": deepcopy(content if content is not None else CONTENT[data_type]),
        "trace_id": "trace-t006",
    }
    if source_info is not None:
        body["source_info"].update(source_info)
    return body


def validated(data_type: str = "financial", **kwargs: Any):  # type: ignore[no-untyped-def]
    enabled = kwargs.pop("compliance_enabled", False)
    return events.validate_event(payload(data_type, **kwargs), compliance_enabled=enabled)


@pytest.mark.parametrize(
    "data_type",
    ["financial", "announcement", "kline", "quote", "risk"],
)
def test_write_event_dispatches_by_data_type(
    data_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, Any, dict[str, Any]]] = []
    expected = WriteOutcome(
        status="inserted", table=f"dwd.{data_type}", event_id="evt_x", version=1, row_id=7
    )

    def handler(session: Any, event: Any, compliance: Any) -> WriteOutcome:
        calls.append((session, event, dict(compliance)))
        return expected

    monkeypatch.setattr(writer, "_WRITERS", {EventDataType(data_type): handler})
    event = validated(data_type)
    session = object()

    outcome = write_event(session, event, compliance_enabled=False)

    assert outcome is expected
    assert calls == [
        (session, event, {"compliance_tier": None, "commercial_use": None})
    ]


def test_unknown_data_type_is_rejected_with_the_supported_set() -> None:
    event = replace(validated(), data_type=EventDataType.RESEARCH)

    with pytest.raises(InvalidArgumentError) as caught:
        write_event(object(), event, compliance_enabled=False)

    details = caught.value.details
    assert details is not None
    assert details["field"] == "data_type"
    assert details["value"] == "research"
    assert details["supported"] == ["announcement", "financial", "kline", "quote", "risk"]


def test_missing_symbol_is_rejected() -> None:
    event = replace(validated(), symbol=None)

    with pytest.raises(InvalidArgumentError) as caught:
        writer._require_symbol(event)

    assert caught.value.details == {"field": "symbol"}


def test_write_outcome_as_dict_is_stable() -> None:
    outcome = WriteOutcome(
        status="inserted",
        table="dwd.stock_quote_snapshot",
        event_id="evt_1",
        version=2,
        row_id=11,
    )

    assert outcome.inserted is True
    assert outcome.as_dict() == {
        "status": "inserted",
        "table": "dwd.stock_quote_snapshot",
        "event_id": "evt_1",
        "version": 2,
        "id": 11,
    }

    skipped = WriteOutcome(status="skipped", table="dwd.stock_quote_snapshot", event_id="evt_1")
    assert skipped.inserted is False
    assert skipped.version is None
    assert skipped.row_id is None


class TestExactNumeric:
    def test_the_acceptance_amount_is_returned_unchanged(self) -> None:
        value = Decimal("1234567890123456.1234")

        result = writer._exact_numeric(
            value, precision=20, field_name="content.structured_fields.amount_revenue"
        )

        assert result is value
        assert result == Decimal("1234567890123456.1234")
        assert not isinstance(result, float)

    @pytest.mark.parametrize(
        "value",
        [Decimal("0"), Decimal("-5.5000"), Decimal("9999999999999999.9999")],
    )
    def test_values_that_fit_are_returned_unchanged(self, value: Decimal) -> None:
        assert writer._exact_numeric(value, precision=20, field_name="amount") == value

    def test_none_stays_none(self) -> None:
        assert writer._exact_numeric(None, precision=20, field_name="amount") is None

    def test_more_than_four_decimals_is_rejected_not_rounded(self) -> None:
        with pytest.raises(InvalidArgumentError) as caught:
            writer._exact_numeric(Decimal("1.00001"), precision=20, field_name="amount")

        assert caught.value.details is not None
        assert caught.value.details["numeric"] == "NUMERIC(20, 4)"
        assert "more than 4 decimal places" in caught.value.message

    def test_integer_digits_must_fit_the_precision(self) -> None:
        with pytest.raises(InvalidArgumentError):
            writer._exact_numeric(Decimal("10000000000000000"), precision=20, field_name="amount")

        # The same value fits the 24-digit columns used for volume and turnover.
        value = Decimal("10000000000000000")
        assert writer._exact_numeric(value, precision=24, field_name="volume") == value

    @pytest.mark.parametrize("value", [1.5, "1.5", 0])
    def test_non_decimal_input_is_rejected(self, value: Any) -> None:
        with pytest.raises(InvalidArgumentError) as caught:
            writer._exact_numeric(value, precision=20, field_name="amount")

        assert caught.value.details is not None
        assert caught.value.details["field"] == "amount"

    def test_infinity_is_rejected(self) -> None:
        with pytest.raises(InvalidArgumentError):
            writer._exact_numeric(Decimal("Infinity"), precision=20, field_name="amount")


class TestQualityAndCompliance:
    def test_quality_is_copied_verbatim(self) -> None:
        content = deepcopy(CONTENT["financial"])
        content["quality_metrics"] = {
            "quality_status": "bad",
            "confidence": "low",
            "balance_check_result": {"balanced": False, "diff": "0.01"},
        }
        event = events.validate_event(payload(content=content))

        assert writer._quality_values(event) == {"quality_status": "bad", "confidence": "low"}
        assert writer._balance_check_result(event) == {"balanced": False, "diff": "0.01"}
        assert event.content.quality_metrics.quality_status is QualityStatus.BAD

    def test_confidence_may_be_absent(self) -> None:
        content = deepcopy(CONTENT["financial"])
        content["quality_metrics"] = {"quality_status": "suspicious"}
        event = events.validate_event(payload(content=content))

        assert writer._quality_values(event) == {
            "quality_status": "suspicious",
            "confidence": None,
        }

    def test_switch_off_writes_both_columns_as_null(self) -> None:
        event = events.validate_event(
            payload(source_info={"compliance_tier": "S", "commercial_use": True}),
            compliance_enabled=False,
        )

        assert event.compliance is None
        assert writer._compliance_values(event, False) == {
            "compliance_tier": None,
            "commercial_use": None,
        }

    def test_switch_on_copies_the_event_mark(self) -> None:
        event = events.validate_event(
            payload(source_info={"compliance_tier": "s", "commercial_use": True}),
            compliance_enabled=True,
        )

        assert writer._compliance_values(event, True) == {
            "compliance_tier": "S",
            "commercial_use": True,
        }

    def test_switch_on_without_a_mark_is_rejected_instead_of_inferred(self) -> None:
        event = events.validate_event(payload(), compliance_enabled=False)

        with pytest.raises(InvalidArgumentError) as caught:
            writer._compliance_values(event, True)

        assert caught.value.details == {"field": "source_info.compliance_tier"}


class TestAnnouncementHelpers:
    def test_content_hash_prefers_clean_content(self) -> None:
        content = AnnouncementContent.model_validate(deepcopy(CONTENT["announcement"]))

        assert writer._content_hash(content) == hashlib.sha256(
            b"Q3 revenue grew"
        ).hexdigest()

    def test_content_hash_falls_back_to_raw_html(self) -> None:
        body = deepcopy(CONTENT["announcement"])
        body.pop("clean_content")
        content = AnnouncementContent.model_validate(body)

        assert writer._content_hash(content) == hashlib.sha256(
            b"<p>Q3 revenue grew</p>"
        ).hexdigest()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-10-27", date(2026, 10, 27)),
            (date(2026, 10, 27), date(2026, 10, 27)),
            (datetime(2026, 10, 27, 9, 30), date(2026, 10, 27)),
        ],
    )
    def test_publish_date_forms(self, raw: Any, expected: date) -> None:
        assert writer._announcement_date({"publish_date": raw}) == expected

    @pytest.mark.parametrize("raw", [None, "27/10/2026", 20261027])
    def test_bad_publish_date_is_rejected(self, raw: Any) -> None:
        with pytest.raises(InvalidArgumentError) as caught:
            writer._announcement_date({"publish_date": raw})

        assert caught.value.details is not None
        assert caught.value.details["field"] == "content.structured_fields.publish_date"

    def test_optional_text(self) -> None:
        assert writer._optional_text(None, field_name="f") is None
        assert writer._optional_text("Q3", field_name="f") == "Q3"
        with pytest.raises(InvalidArgumentError):
            writer._optional_text(3, field_name="f")


def test_dwd_write_path_contains_no_update_or_delete() -> None:
    """Static guard: history is append-only, corrections insert a new version."""

    package = Path(writer.__file__).parent
    files = sorted(package.glob("*.py")) + [package.parent / "db" / "repositories" / "dwd.py"]
    forbidden_sql = re.compile(r"\b(?:UPDATE|DELETE\s+FROM)\s+dwd\.\w+", re.IGNORECASE)
    forbidden_imports = {"update", "delete"}

    for path in files:
        code = path.read_text(encoding="utf-8")
        assert forbidden_sql.search(code) is None, path
        tree = ast.parse(code, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
                imported = {alias.name for alias in node.names}
                assert not imported & forbidden_imports, path

