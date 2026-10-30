"""Numeric CLI options are rejected through the JSON envelope, not Click usage."""

import json

import pytest
from typer.testing import CliRunner

from financecoffer.archive.service import MAX_SAMPLE
from financecoffer.cli import app
from financecoffer.cli.options import parse_int_option
from financecoffer.core.errors import InvalidArgumentError
from financecoffer.dws import MAX_FISCAL_YEARS
from financecoffer.ods import MAX_LIMIT as ODS_MAX_LIMIT
from financecoffer.query import MAX_RISK_LIMIT, MAX_TOP_K
from financecoffer.vector.service import MAX_LIMIT as VECTOR_MAX_LIMIT

runner = CliRunner()

# One row per option moved off Typer's ``min=`` / ``max=``: argv template, the
# ``details.field`` the envelope must name, and values outside the range.
_INVALID_SPECS: list[tuple[list[str], str, tuple[str, ...]]] = [
    (["ods", "list", "--limit", "{v}"], "limit", ("0", str(ODS_MAX_LIMIT + 1), "abc")),
    (
        ["query", "risk", "--symbol", "600000", "--limit", "{v}"],
        "limit",
        ("0", str(MAX_RISK_LIMIT + 1), "1.5"),
    ),
    (
        ["search", "announcements", "--symbol", "600000", "--query", "x", "--top-k", "{v}"],
        "top_k",
        ("0", str(MAX_TOP_K + 1)),
    ),
    (["vector", "chunk", "--limit", "{v}"], "limit", ("0", str(VECTOR_MAX_LIMIT + 1))),
    (["vector", "chunk", "--chunk-size", "{v}"], "chunk_size", ("0", "abc")),
    (["vector", "chunk", "--overlap", "{v}"], "overlap", ("-1",)),
    (["vector", "embed", "--limit", "{v}"], "limit", ("0", str(VECTOR_MAX_LIMIT + 1))),
    (["vector", "embed", "--chunk-size", "{v}"], "chunk_size", ("0",)),
    (["vector", "embed", "--overlap", "{v}"], "overlap", ("-1",)),
    (["vector", "reindex", "--limit", "{v}"], "limit", ("0", str(VECTOR_MAX_LIMIT + 1))),
    (["archive", "run", "--older-than-years", "{v}"], "older_than_years", ("-1",)),
    (["archive", "run", "--min-bytes", "{v}"], "min_bytes", ("0",)),
    (["archive", "run", "--limit", "{v}"], "limit", ("0",)),
    (
        ["archive", "verify", "--sample", "{v}"],
        "sample",
        ("0", str(MAX_SAMPLE + 1), "abc"),
    ),
    (
        ["dws", "build", "--all", "--years", "{v}"],
        "years",
        ("0", str(MAX_FISCAL_YEARS + 1)),
    ),
    (["dws", "rebuild", "--years", "{v}"], "years", ("0", str(MAX_FISCAL_YEARS + 1))),
    (["dwd", "show", "--symbol", "600000", "--version", "{v}"], "version", ("0",)),
    (["db", "partitions", "create", "--ahead", "{v}"], "ahead", ("-1",)),
]

_BOUNDARY_SPECS: list[tuple[list[str], str, tuple[str, ...]]] = [
    (["ods", "list", "--limit", "{v}"], "limit", ("1", str(ODS_MAX_LIMIT))),
    (
        ["query", "risk", "--symbol", "600000", "--limit", "{v}"],
        "limit",
        ("1", str(MAX_RISK_LIMIT)),
    ),
    (
        ["search", "announcements", "--symbol", "600000", "--query", "x", "--top-k", "{v}"],
        "top_k",
        ("1", str(MAX_TOP_K)),
    ),
    (["vector", "chunk", "--limit", "{v}"], "limit", ("1", str(VECTOR_MAX_LIMIT))),
    (["vector", "chunk", "--chunk-size", "{v}"], "chunk_size", ("1", "100000")),
    (["vector", "chunk", "--overlap", "{v}"], "overlap", ("0", "100000")),
    (["vector", "embed", "--limit", "{v}"], "limit", ("1", str(VECTOR_MAX_LIMIT))),
    (["vector", "embed", "--chunk-size", "{v}"], "chunk_size", ("1", "100000")),
    (["vector", "embed", "--overlap", "{v}"], "overlap", ("0", "100000")),
    (["vector", "reindex", "--limit", "{v}"], "limit", ("1", str(VECTOR_MAX_LIMIT))),
    (["archive", "run", "--dry-run", "--older-than-years", "{v}"], "older_than_years", ("0", "50")),
    (["archive", "run", "--dry-run", "--min-bytes", "{v}"], "min_bytes", ("1", "1048576")),
    (["archive", "run", "--dry-run", "--limit", "{v}"], "limit", ("1", "100000")),
    (["archive", "verify", "--sample", "{v}"], "sample", ("1", str(MAX_SAMPLE))),
    (["dws", "build", "--all", "--years", "{v}"], "years", ("1", str(MAX_FISCAL_YEARS))),
    (["dws", "rebuild", "--years", "{v}"], "years", ("1", str(MAX_FISCAL_YEARS))),
    (["dwd", "show", "--symbol", "600000", "--version", "{v}"], "version", ("1", "99")),
    (["db", "partitions", "create", "--ahead", "{v}"], "ahead", ("0", "24")),
]


def _expand(
    specs: list[tuple[list[str], str, tuple[str, ...]]],
) -> list:
    params = []
    for argv, field, values in specs:
        for value in values:
            label = f"{'-'.join(argv[:2])}-{argv[-2].lstrip('-')}-{value}"
            params.append(
                pytest.param([part.replace("{v}", value) for part in argv], field, id=label)
            )
    return params


INVALID_CASES = _expand(_INVALID_SPECS)
BOUNDARY_CASES = _expand(_BOUNDARY_SPECS)


@pytest.mark.parametrize(("argv", "field"), INVALID_CASES)
def test_out_of_range_value_uses_the_json_envelope(argv: list[str], field: str) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert "Usage:" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["field"] == field
    assert payload["meta"]["trace_id"]


@pytest.mark.parametrize(("argv", "field"), BOUNDARY_CASES)
def test_in_range_boundary_is_not_rejected_by_the_guard(argv: list[str], field: str) -> None:
    result = runner.invoke(app, argv)

    assert "Invalid value for" not in result.stdout
    if result.stdout.lstrip().startswith("{"):
        payload = json.loads(result.stdout)
        error = payload.get("error") or {}
        details = error.get("details") or {}
        assert not (error.get("code") == "E_INVALID_ARGUMENT" and details.get("field") == field), (
            f"boundary value rejected for {field}: {error}"
        )


def test_envelope_shape_under_explicit_json_output() -> None:
    result = runner.invoke(app, ["--output", "json", "archive", "verify", "--sample", "0"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert set(payload) == {"ok", "data", "meta", "error"}
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["field"] == "sample"
    assert payload["error"]["details"]["value"] == 0


@pytest.mark.parametrize(
    ("minimum", "maximum", "accepted"),
    [
        (1, 1000, (1, 500, 1000)),
        (1, None, (1, 10**9)),
        (0, None, (0, 10**9)),
    ],
)
def test_parse_int_option_boundaries(
    minimum: int | None, maximum: int | None, accepted: tuple[int, ...]
) -> None:
    for value in accepted:
        assert (
            parse_int_option(str(value), field="limit", minimum=minimum, maximum=maximum) == value
        )


@pytest.mark.parametrize(("raw", "minimum", "maximum"), [("abc", 1, 10), ("1.5", 0, None)])
def test_parse_int_option_rejects_non_integers(raw: str, minimum: int, maximum: int | None) -> None:
    with pytest.raises(InvalidArgumentError) as info:
        parse_int_option(raw, field="limit", minimum=minimum, maximum=maximum)

    assert info.value.code.value == "E_INVALID_ARGUMENT"
    assert info.value.details["field"] == "limit"


def test_ods_list_forwards_the_parsed_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from financecoffer.cli.commands import ods as ods_module

    seen: dict[str, object] = {}

    class FakeOds:
        def __init__(self, **_: object) -> None:
            pass

        def listing(self, **kwargs: object) -> list[object]:
            seen.update(kwargs)
            return []

    monkeypatch.setattr(ods_module, "load_cli_config", lambda **_: object())
    monkeypatch.setattr(ods_module, "OdsService", FakeOds)

    result = runner.invoke(app, ["ods", "list", "--limit", str(ODS_MAX_LIMIT)])

    assert result.exit_code == 0, result.stdout
    assert seen["limit"] == ODS_MAX_LIMIT


def test_vector_chunk_forwards_the_parsed_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from financecoffer.cli.commands import vector as vector_module

    seen: dict[str, object] = {}

    class FakeVector:
        def __init__(self, **_: object) -> None:
            pass

        def chunk_preview(self, symbol: object, **kwargs: object) -> dict[str, object]:
            seen["symbol"] = symbol
            seen.update(kwargs)
            return {"chunks": 0}

        def close(self) -> None:
            pass

    monkeypatch.setattr(vector_module, "load_cli_config", lambda **_: object())
    monkeypatch.setattr(vector_module, "VectorService", FakeVector)

    result = runner.invoke(
        app, ["vector", "chunk", "--limit", "1", "--chunk-size", "1", "--overlap", "0"]
    )

    assert result.exit_code == 0, result.stdout
    assert seen["limit"] == 1
    assert seen["chunk_size"] == 1
    assert seen["overlap"] == 0
