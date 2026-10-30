"""C-group numeric options: one fixed semantics per option, one envelope path.

Unlike the earlier batch, these options sit next to service-layer clamping, so
each one had to be decided before it could be moved: keep rejecting, keep
clamping, or unify two different bounds. ``docs/decisions/DEC-0030`` records the
outcome; the tests here pin the CLI half of it.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from financecoffer.cli import app
from financecoffer.cli.commands import db as db_module
from financecoffer.cli.commands import etl as etl_module
from financecoffer.cli.commands import vector as vector_module
from financecoffer.core.errors import ConfigError, InvalidArgumentError
from financecoffer.db.migrations import MigrationRunner
from financecoffer.etl import replay as replay_module
from financecoffer.ods.service import MAX_REPLAY
from financecoffer.vector.embeddings import MAX_BATCH_SIZE

runner = CliRunner()

# argv, the ``details.field`` the envelope must name, and the value it must echo.
_INVALID_CASES: list[tuple[list[str], str, Any]] = [
    (["--timeout", "0"], "timeout", 0),
    (["--timeout", "abc"], "timeout", "abc"),
    (["--retries", "-1"], "retries", -1),
    (["--retries", "1.5"], "retries", "1.5"),
    (["vector", "embed", "--batch-size", "0"], "batch_size", 0),
    (["vector", "embed", "--batch-size", "33"], "batch_size", 33),
    (["vector", "embed", "--batch-size", "64"], "batch_size", 64),
    (["vector", "embed", "--batch-size", "abc"], "batch_size", "abc"),
    (["etl", "replay", "--from-ods", "--limit", "0"], "limit", 0),
    (["etl", "replay", "--from-ods", "--limit", "abc"], "limit", "abc"),
    (["etl", "consume", "--once", "--batch-size", "0"], "batch_size", 0),
    (["etl", "consume", "--once", "--batch-size", "1001"], "batch_size", 1001),
    (["etl", "consume", "--once", "--batch-size", "abc"], "batch_size", "abc"),
    (["etl", "consume", "--once", "--block-ms", "-1"], "block_ms", -1),
    (["etl", "consume", "--once", "--block-ms", "60001"], "block_ms", 60001),
    (["etl", "consume", "--once", "--claim-idle-ms", "-1"], "claim_idle_ms", -1),
    (["etl", "consume", "--once", "--claim-idle-ms", "0.5"], "claim_idle_ms", "0.5"),
    (["db", "migrate", "--ahead", "-1"], "ahead", -1),
    (["db", "migrate", "--ahead", "abc"], "ahead", "abc"),
    (["db", "migrate", "--sql", "--ahead", "-1"], "ahead", -1),
]

# Values on the inclusive boundary must survive the guard. The commands may
# still stop later for a missing database or Redis; only a rejection naming the
# field is a failure here.
_BOUNDARY_CASES: list[tuple[list[str], str]] = [
    (["--timeout", "1"], "timeout"),
    (["--retries", "0"], "retries"),
    (["vector", "embed", "--batch-size", "1"], "batch_size"),
    (["vector", "embed", "--batch-size", str(MAX_BATCH_SIZE)], "batch_size"),
    (["etl", "replay", "--from-ods", "--limit", "1"], "limit"),
    (["etl", "replay", "--from-ods", "--limit", str(MAX_REPLAY)], "limit"),
    (["etl", "consume", "--once", "--batch-size", "1"], "batch_size"),
    (["etl", "consume", "--once", "--batch-size", "1000"], "batch_size"),
    (["etl", "consume", "--once", "--block-ms", "0"], "block_ms"),
    (["etl", "consume", "--once", "--block-ms", "60000"], "block_ms"),
    (["etl", "consume", "--once", "--claim-idle-ms", "0"], "claim_idle_ms"),
    (["db", "migrate", "--ahead", "0"], "ahead"),
]


def _payload(result: Any) -> dict[str, Any] | None:
    text = result.stdout.lstrip()
    return json.loads(text) if text.startswith("{") else None


def _rejected_field(payload: dict[str, Any] | None) -> str | None:
    error = (payload or {}).get("error") or {}
    if error.get("code") != "E_INVALID_ARGUMENT":
        return None
    return (error.get("details") or {}).get("field")


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop accepted values at config loading instead of dialling Redis or PG."""

    def _offline(**_kwargs: object) -> Any:
        raise ConfigError("offline unit test")

    monkeypatch.setattr(vector_module, "load_cli_config", _offline)
    monkeypatch.setattr(etl_module, "load_cli_config", _offline)
    monkeypatch.setattr(db_module, "resolve_cli_dsn", lambda *_args, **_kwargs: _offline())


@pytest.mark.parametrize(
    ("argv", "field", "value"),
    [
        pytest.param(argv, field, value, id=f"{argv[0]}-{field}-{value}")
        for argv, field, value in _INVALID_CASES
    ],
)
def test_out_of_range_value_uses_the_json_envelope(
    argv: list[str], field: str, value: Any
) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert "Usage:" not in result.stdout
    payload = _payload(result)
    assert payload is not None
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["field"] == field
    assert payload["error"]["details"]["value"] == value
    assert payload["meta"]["trace_id"]


@pytest.mark.parametrize(
    ("argv", "field"),
    [pytest.param(argv, field, id=f"{argv[0]}-{field}") for argv, field in _BOUNDARY_CASES],
)
def test_in_range_boundary_is_not_rejected_by_the_guard(
    argv: list[str], field: str, offline: None
) -> None:
    result = runner.invoke(app, argv)

    assert "Invalid value for" not in result.stdout
    assert _rejected_field(_payload(result)) != field, f"boundary value rejected for {field}"


def test_global_options_still_default_and_forward() -> None:
    result = runner.invoke(app, ["--timeout", "1", "--retries", "0"])

    assert result.exit_code == 0, result.stdout


def test_embed_batch_size_has_a_single_cap_of_32(offline: None) -> None:
    """33..64 used to pass the CLI and were silently clamped by the client."""

    accepted = runner.invoke(app, ["vector", "embed", "--batch-size", str(MAX_BATCH_SIZE)])
    assert _rejected_field(_payload(accepted)) != "batch_size"

    for value in ("33", "64"):
        rejected = runner.invoke(app, ["vector", "embed", "--batch-size", value])
        assert rejected.exit_code == 2
        assert _payload(rejected)["error"]["code"] == "E_INVALID_ARGUMENT"
        assert _payload(rejected)["error"]["details"]["value"] == int(value)


def test_embed_forwards_the_parsed_batch_size_and_global_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from financecoffer.cli.commands import vector as vector_module

    seen: dict[str, object] = {}

    class FakeVector:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        def embed(self, *args: object, **kwargs: object) -> Any:
            return SimpleNamespace(payload=lambda: {"announcements": 0}, failed=False)

        def close(self) -> None:
            pass

    monkeypatch.setattr(vector_module, "load_cli_config", lambda **_: object())
    monkeypatch.setattr(vector_module, "VectorService", FakeVector)

    result = runner.invoke(
        app,
        ["--timeout", "9", "--retries", "1", "vector", "embed", "--batch-size", "16"],
    )

    assert result.exit_code == 0, result.stdout
    assert seen["batch_size"] == 16
    assert seen["timeout"] == 9
    assert seen["retries"] == 1


def test_replay_limit_above_the_cap_is_still_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """--limit 5000 keeps meaning 1000, exactly as before the envelope change."""

    seen: dict[str, object] = {}

    class FakeRepository:
        def __init__(self, session: object) -> None:
            pass

        def search(self, **kwargs: object) -> list[object]:
            seen.update(kwargs)
            return []

    class FakeDatabase:
        @contextmanager
        def session_scope(self) -> Iterator[None]:
            yield None

    monkeypatch.setattr(replay_module, "RawDataRepository", FakeRepository)

    data = replay_module.replay_from_ods(database=FakeDatabase(), limit=5000, dry_run=True)

    assert seen["limit"] == MAX_REPLAY
    assert data["dry_run"] is True


def test_migrate_rejects_a_negative_ahead_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard moved in front of the migration loop, not just the CLI parser."""

    monkeypatch.setenv("FC_DB_URL", "postgresql://unit:unit@127.0.0.1:1/financecoffer")
    runner_ = MigrationRunner()

    def _must_not_connect() -> Any:
        raise AssertionError("connected before validating --ahead")

    monkeypatch.setattr(runner_, "_connect", _must_not_connect)

    with pytest.raises(InvalidArgumentError) as caught:
        runner_.apply(revision="head", ahead_months=-1)

    assert caught.value.code.value == "E_INVALID_ARGUMENT"
    assert caught.value.details["field"] == "ahead"
    assert caught.value.details["value"] == -1
