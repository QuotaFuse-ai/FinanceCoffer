import json

import pytest
from typer.testing import CliRunner

from financecoffer import __version__
from financecoffer.cli import app

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def test_version_flag() -> None:
    result = _invoke("--version")

    assert result.exit_code == 0
    assert result.stdout.strip() == f"fcoffer {__version__}"


def test_help_lists_p0_command_tree() -> None:
    result = _invoke("--help")

    assert result.exit_code == 0
    for command in (
        "init",
        "doctor",
        "config",
        "db",
        "serve",
        "start-all",
        "mcp",
        "etl",
        "ingest",
        "ods",
        "dwd",
        "dws",
        "vector",
        "query",
        "search",
        "metrics",
        "health",
    ):
        assert command in result.stdout, command


@pytest.mark.parametrize(
    ("group", "commands"),
    [
        ("db", ("migrate", "status", "partitions")),
        ("serve", ("api", "admin")),
        ("mcp", ("stdio", "http", "tools")),
        ("etl", ("consume", "status", "pause", "resume", "replay", "dlq")),
        ("ingest", ("event", "backfill", "webhook")),
        ("ods", ("list", "show", "stats", "replay")),
        ("dwd", ("show", "versions", "backfill", "verify")),
        ("dws", ("build", "show", "rebuild")),
        ("vector", ("chunk", "embed", "index", "search", "reindex", "stats")),
        ("query", ("structured", "risk")),
        ("search", ("announcements",)),
    ],
)
def test_subcommand_groups_match_cli_spec(group: str, commands: tuple[str, ...]) -> None:
    result = _invoke(group, "--help")

    assert result.exit_code == 0
    for command in commands:
        assert command in result.stdout, f"{group} missing {command}"


def test_no_arguments_prints_help() -> None:
    result = _invoke()

    assert result.exit_code == 0
    assert "Usage:" in result.stdout


def test_placeholder_returns_not_implemented_envelope() -> None:
    result = _invoke("ods", "list")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "E_NOT_IMPLEMENTED"
    assert payload["error"]["details"]["command"] == "ods list"
    assert payload["meta"]["trace_id"]


def test_placeholder_accepts_documented_options() -> None:
    result = _invoke(
        "query",
        "structured",
        "--symbol",
        "600000",
        "--metric",
        "roe",
        "--year-range",
        "2021-2025",
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "E_NOT_IMPLEMENTED"


def test_invalid_output_format_is_a_usage_error() -> None:
    result = _invoke("--output", "xml", "health")

    assert result.exit_code == 2


def test_invalid_trace_id_is_rejected() -> None:
    result = _invoke("--trace-id", "bad id!", "health")

    assert result.exit_code == 2


def test_quiet_and_verbose_are_mutually_exclusive() -> None:
    result = _invoke("--quiet", "--verbose", "health")

    assert result.exit_code == 2
