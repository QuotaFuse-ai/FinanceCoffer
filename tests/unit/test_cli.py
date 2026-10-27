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
    result = _invoke("dwd", "show")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "E_NOT_IMPLEMENTED"
    assert payload["error"]["details"]["command"] == "dwd show"
    assert payload["meta"]["trace_id"]


def test_placeholder_accepts_documented_options() -> None:
    result = _invoke("dws", "build", "--symbol", "600000", "--rebuild")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_NOT_IMPLEMENTED"
    assert payload["error"]["details"]["command"] == "dws build"


def test_query_structured_emits_envelope_from_the_service(monkeypatch) -> None:
    from financecoffer.cli.commands import query as query_module
    from financecoffer.core.envelope import Meta

    class _Result:
        data = {"symbol": "600000", "years": []}

        @staticmethod
        def meta(trace_id: str | None = None) -> Meta:
            return Meta(source="dws")

    class _Service:
        def __init__(self, **_: object) -> None:
            pass

        def structured(self, symbol: str, **_: object) -> _Result:
            return _Result()

    monkeypatch.setattr(query_module, "QueryService", _Service)

    result = _invoke("query", "structured", "--symbol", "600000")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["symbol"] == "600000"
    assert payload["meta"]["source"] == "dws"


def test_query_rejects_an_unknown_metric() -> None:
    result = _invoke("query", "structured", "--symbol", "600000", "--metric", "bogus")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"


def test_invalid_output_format_is_a_usage_error() -> None:
    result = _invoke("--output", "xml", "health")

    assert result.exit_code == 2


def test_invalid_trace_id_is_rejected() -> None:
    result = _invoke("--trace-id", "bad id!", "health")

    assert result.exit_code == 2


def test_quiet_and_verbose_are_mutually_exclusive() -> None:
    result = _invoke("--quiet", "--verbose", "health")

    assert result.exit_code == 2


def test_serve_api_refuses_a_non_loopback_bind() -> None:
    result = _invoke("serve", "api", "--host", "0.0.0.0")

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["host"] == "0.0.0.0"


def test_query_risk_forwards_filters_to_the_service(monkeypatch) -> None:
    from financecoffer.cli.commands import query as query_module
    from financecoffer.core.envelope import Meta

    class _Result:
        data = {"symbol": "600000", "count": 0, "events": []}

        @staticmethod
        def meta(trace_id: str | None = None) -> Meta:
            return Meta(source="dwd")

    class _Service:
        calls: list[tuple[str, str, dict[str, object]]] = []

        def __init__(self, **_: object) -> None:
            pass

        def risk(self, symbol: str, **kwargs: object) -> _Result:
            _Service.calls.append(("risk", symbol, kwargs))
            return _Result()

    monkeypatch.setattr(query_module, "QueryService", _Service)

    result = _invoke(
        "query", "risk", "--symbol", "600000", "--since", "2025-01-01", "--limit", "20"
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["symbol"] == "600000"
    assert _Service.calls == [("risk", "600000", {"since": "2025-01-01", "limit": 20})]
