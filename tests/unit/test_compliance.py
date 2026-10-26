import pytest

from financecoffer.core.compliance import (
    COMPLIANCE_TIERS,
    ComplianceMark,
    columns,
    extract_mark,
)
from financecoffer.core.errors import InvalidArgumentError


def test_switch_off_ignores_source_info_entirely() -> None:
    assert extract_mark(None, enabled=False) is None
    assert extract_mark({"compliance_tier": "Z"}, enabled=False) is None
    assert columns(None) == {"compliance_tier": None, "commercial_use": None}


def test_switch_on_returns_the_normalized_mark() -> None:
    mark = extract_mark({"compliance_tier": " c ", "commercial_use": False}, enabled=True)

    assert mark == ComplianceMark(tier="C", commercial_use=False)
    assert mark is not None
    assert mark.as_dict() == {"compliance_tier": "C", "commercial_use": False}
    assert columns(mark) == {"compliance_tier": "C", "commercial_use": False}


def test_every_documented_tier_is_accepted() -> None:
    assert COMPLIANCE_TIERS == frozenset({"S", "B", "C", "D", "X"})
    for tier in COMPLIANCE_TIERS:
        mark = extract_mark({"compliance_tier": tier, "commercial_use": True}, enabled=True)
        assert mark is not None
        assert mark.tier == tier


@pytest.mark.parametrize(
    "source_info",
    [
        None,
        {},
        {"commercial_use": True},
        {"compliance_tier": "Z", "commercial_use": True},
        {"compliance_tier": "", "commercial_use": True},
    ],
)
def test_enabled_ingest_rejects_a_missing_or_invalid_tier(source_info) -> None:
    with pytest.raises(InvalidArgumentError) as excinfo:
        extract_mark(source_info, enabled=True)

    assert excinfo.value.code.value == "E_INVALID_ARGUMENT"
    assert excinfo.value.details is not None
    assert excinfo.value.details["field"] == "source_info.compliance_tier"


@pytest.mark.parametrize("commercial_use", [None, "true", 1, 0])
def test_enabled_ingest_requires_a_boolean_commercial_use(commercial_use) -> None:
    with pytest.raises(InvalidArgumentError) as excinfo:
        extract_mark(
            {"compliance_tier": "C", "commercial_use": commercial_use},
            enabled=True,
        )

    assert excinfo.value.details is not None
    assert excinfo.value.details["field"] == "source_info.commercial_use"
