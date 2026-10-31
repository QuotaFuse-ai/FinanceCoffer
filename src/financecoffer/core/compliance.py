"""Compliance marking gated by ``FC_COMPLIANCE_ENABLED`` (DEC-0010).

The switch is off by default. Off means the compliance fields are not
considered at all: callers get ``None`` back and must store/return nothing.
On means every ingested record is marked with the source's tier and the
``commercial_use`` flag; a source that cannot be marked is rejected instead of
being stored unmarked, so an enabled deployment never holds a silent gap. The
one exception is ``heartbeat``: contract 3.8 keeps it out of every table that
has these columns, so ``validate_event`` never asks for its mark.

Tiers come from ``docs/specs/event-contract.md`` section 7: S/B/C/D/X.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from financecoffer.core.errors import InvalidArgumentError

COMPLIANCE_TIERS: frozenset[str] = frozenset({"S", "B", "C", "D", "X"})

_TIER_FIELD = "source_info.compliance_tier"
_USE_FIELD = "source_info.commercial_use"


@dataclass(frozen=True)
class ComplianceMark:
    """The two G-11 columns carried by one stored record."""

    tier: str
    commercial_use: bool

    def as_dict(self) -> dict[str, Any]:
        return {"compliance_tier": self.tier, "commercial_use": self.commercial_use}


def extract_mark(
    source_info: Mapping[str, Any] | None,
    *,
    enabled: bool,
) -> ComplianceMark | None:
    """Return the mark for ``source_info``, or ``None`` when the switch is off."""

    if not enabled:
        return None

    info = source_info or {}
    tier = info.get("compliance_tier")
    normalized = str(tier).strip().upper() if tier is not None else ""
    if normalized not in COMPLIANCE_TIERS:
        raise InvalidArgumentError(
            "source_info.compliance_tier is missing or not one of S/B/C/D/X",
            details={"field": _TIER_FIELD, "value": normalized},
        )

    commercial_use = info.get("commercial_use")
    if not isinstance(commercial_use, bool):
        raise InvalidArgumentError(
            "source_info.commercial_use must be a boolean when compliance marking is enabled",
            details={"field": _USE_FIELD},
        )

    return ComplianceMark(tier=normalized, commercial_use=commercial_use)


def columns(mark: ComplianceMark | None) -> dict[str, Any]:
    """Column values for an INSERT: both ``NULL`` when marking is off."""

    if mark is None:
        return {"compliance_tier": None, "commercial_use": None}
    return mark.as_dict()
