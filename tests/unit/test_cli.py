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
        "archive",
        "vector",
        "query",
        "search",
        "metrics",
        "health",
        "tool",
        "skill",
    ):
        assert command in result.stdout, command


@pytest.mark.parametrize(
    ("group", "commands"),
    [
        ("db", ("migrate", "status", "partitions")),
        ("config", ("show", "check")),
        ("serve", ("api", "admin")),
        ("mcp", ("stdio", "http", "tools")),
        ("etl", ("consume", "status", "pause", "resume", "replay", "dlq")),
        ("ingest", ("event", "backfill", "webhook")),
        ("ods", ("list", "show", "stats", "replay")),
        ("dwd", ("show", "versions", "backfill", "verify")),
        ("dws", ("build", "show", "rebuild")),
        ("archive", ("run", "status", "restore", "verify")),
        ("vector", ("chunk", "embed", "index", "search", "reindex", "stats")),
        ("query", ("structured", "risk")),
        ("search", ("announcements",)),
        ("tool", ("list", "call")),
        ("skill", ("install",)),
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


def _placeholder_app(*args: str):
    import typer

    from financecoffer.cli.placeholder import add_placeholder

    app = typer.Typer()
    add_placeholder(app, "search", path="future search", summary="Future search")
    return CliRunner().invoke(app, list(args))


def test_placeholder_returns_not_implemented_envelope() -> None:
    result = _placeholder_app("search")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "E_NOT_IMPLEMENTED"
    assert payload["error"]["details"]["command"] == "future search"
    assert payload["meta"]["trace_id"]


def test_placeholder_accepts_documented_options() -> None:
    result = _placeholder_app("search", "--symbol", "600000", "--dry-run")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_NOT_IMPLEMENTED"
    assert payload["error"]["details"]["command"] == "future search"


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


# -- bootstrap: init / doctor / config check --------------------------------

_DSN = "postgresql://localhost/financecoffer"


def test_db_status_hands_the_resolved_dsn_to_the_runner(monkeypatch) -> None:
    """``db status`` must resolve the DSN before constructing the runner.

    ``MigrationRunner`` falls back to the process environment, so passing
    ``options.db_url`` (usually ``None``) straight through ignored both the repo
    ``.env`` and the documented library/CLI split.
    """

    from financecoffer.cli.commands import db as db_module

    seen: list[str] = []

    class _Report:
        def as_dict(self) -> dict[str, object]:
            return {"current_version": 0, "head_version": 0, "pending": [], "applied": []}

    class _Runner:
        def __init__(self, dsn: str, **_kwargs: object) -> None:
            seen.append(dsn)

        def status(self) -> _Report:
            return _Report()

    monkeypatch.setattr(db_module, "MigrationRunner", _Runner)

    result = _invoke(
        "--db-url",
        "postgresql+psycopg://app:secret@db.local:5433/warehouse",
        "db",
        "status",
    )

    assert result.exit_code == 0, result.stdout
    assert seen == ["postgresql://app:secret@db.local:5433/warehouse"]


def test_db_status_rejects_a_malformed_db_url_as_config_error() -> None:
    # QA-A-01: a bad --db-url must fail with the envelope (E_CONFIG, exit 3),
    # never a psycopg traceback.
    result = _invoke("--db-url", "not-a-url", "db", "status")

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_CONFIG"
    assert "Traceback" not in result.stdout


def _config(**overrides: object):
    from financecoffer.core.config import Config

    values: dict[str, object] = {"db_url": _DSN}
    values.update(overrides)
    return Config(**values)


def test_init_dry_run_plans_steps_without_a_database() -> None:
    result = _invoke("init", "--dry-run", "--with-extensions", "--db-url", _DSN)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["dry_run"] is True
    assert data["with_extensions"] is True
    steps = [step["step"] for step in data["steps"]]
    assert steps[0] == "extension"
    assert {"migration", "partition", "index"} <= set(steps)
    assert data["database"]["database"] == "financecoffer"


def test_config_check_names_missing_environment_variables(monkeypatch) -> None:
    from financecoffer.cli.commands import bootstrap as bootstrap_module
    from financecoffer.core.config import Config

    monkeypatch.setattr(bootstrap_module, "load_cli_config", lambda **_: Config())

    result = _invoke("config", "check")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    data = payload["data"]
    assert set(data["missing_env"]) == {
        "FC_DB_URL",
        "FC_MINIO_ACCESS_KEY",
        "FC_MINIO_SECRET_KEY",
    }
    assert {item["field"] for item in data["missing"]} == {
        "db_url",
        "minio_access_key",
        "minio_secret_key",
    }
    assert data["db_url_configured"] is False
    assert data["minio_configured"] is False
    assert any("FC_DB_URL" in warning for warning in data["warnings"])


def test_config_check_passes_when_required_values_are_set(monkeypatch) -> None:
    from financecoffer.cli.commands import bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "load_cli_config",
        lambda **_: _config(minio_access_key="key", minio_secret_key="secret"),
    )

    result = _invoke("config", "check")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["valid"] is True
    assert payload["data"]["missing_env"] == []
    assert payload["data"]["warnings"] == []
    assert payload["data"]["db_url_configured"] is True
    assert payload["data"]["minio_configured"] is True


def test_doctor_reports_ok_and_redacts_probe_details(monkeypatch) -> None:
    from financecoffer.cli.commands import bootstrap as bootstrap_module

    def probe(_context: object) -> dict[str, object]:
        return {
            "status": "ok",
            "reason": "connected",
            "details": {
                "dsn": "postgresql://app:hunter2@db:5432/financecoffer",
                "password": "hunter2",
                "access_key": "AKIAEXAMPLE",
            },
        }

    monkeypatch.setattr(bootstrap_module, "load_cli_config", lambda **_: _config())
    monkeypatch.setattr(bootstrap_module, "_probes", lambda: {"db": probe})

    result = _invoke("doctor", "--check", "db")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["checks"][0]["status"] == "ok"
    assert "hunter2" not in result.stdout
    assert "AKIAEXAMPLE" not in result.stdout
    assert "***" in result.stdout


def test_doctor_exits_non_zero_when_a_probe_fails(monkeypatch) -> None:
    from financecoffer.cli.commands import bootstrap as bootstrap_module

    monkeypatch.setattr(bootstrap_module, "load_cli_config", lambda **_: _config())
    monkeypatch.setattr(
        bootstrap_module,
        "_probes",
        lambda: {"db": lambda _context: {"status": "fail", "reason": "connection refused"}},
    )

    result = _invoke("doctor", "--check", "db")

    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_DEPENDENCY"
    assert payload["error"]["details"]["failed"] == ["db"]


def test_doctor_rejects_an_unknown_check() -> None:
    result = _invoke("doctor", "--check", "nope")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"


# -- monitor: metrics / health ----------------------------------------------


def test_metrics_without_a_running_api_is_a_dependency_error(monkeypatch) -> None:
    from financecoffer.cli.commands import monitor as monitor_module
    from financecoffer.core.errors import DependencyError

    def scrape(_url: str, _timeout: int) -> str:
        raise DependencyError("the API is not reachable", details={"hint": "start it"})

    monkeypatch.setattr(monitor_module, "_scrape", scrape)

    result = _invoke("metrics")

    assert result.exit_code == 5
    assert json.loads(result.stdout)["error"]["code"] == "E_DEPENDENCY"


def test_metrics_parses_prometheus_text_as_json(monkeypatch) -> None:
    from financecoffer.cli.commands import monitor as monitor_module

    monkeypatch.setattr(
        monitor_module,
        "_scrape",
        lambda _url, _timeout: "# TYPE fc_events_total counter\nfc_events_total 7\n",
    )

    result = _invoke("metrics", "--format", "json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["metric_count"] == 1
    assert payload["data"]["metrics"][0]["name"] == "fc_events_total"


def test_health_reports_degraded_dependencies(monkeypatch) -> None:
    from financecoffer.cli.commands import monitor as monitor_module

    monkeypatch.setattr(monitor_module, "load_cli_config", lambda **_: _config())
    monkeypatch.setattr(
        monitor_module,
        "health_probes",
        lambda: {
            "db": lambda _context: {"status": "ok", "reason": "connected"},
            "etl": lambda _context: {"status": "warn", "reason": "consumer not attached"},
        },
    )

    result = _invoke("health", "--check", "db,etl")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "degraded"
    assert [check["name"] for check in payload["data"]["checks"]] == ["db", "etl"]


# -- dwd --------------------------------------------------------------------


class _FakeDwd:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __init__(self, **_: object) -> None:
        self.closed = False

    def show(self, symbol: str, **kwargs: object) -> dict[str, object]:
        _FakeDwd.calls.append(("show", (symbol,), kwargs))
        return {"symbol": symbol, "version": 2}

    def versions(self, symbol: str, *, report_date: str, report_type: str) -> dict[str, object]:
        _FakeDwd.calls.append(
            ("versions", (symbol,), {"report_date": report_date, "report_type": report_type})
        )
        return {"symbol": symbol, "versions": [2, 1]}

    def backfill(self, **kwargs: object) -> dict[str, object]:
        _FakeDwd.calls.append(("backfill", (), kwargs))
        return {"invalid": 0, "failed": 0, "written": 1}

    def verify(self, **kwargs: object) -> dict[str, object]:
        _FakeDwd.calls.append(("verify", (), kwargs))
        return {"failed": 1, "failures": [{"symbol": "600000"}]}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_dwd(monkeypatch):
    from financecoffer.cli.commands import warehouse as warehouse_module

    _FakeDwd.calls = []
    monkeypatch.setattr(warehouse_module, "load_cli_config", lambda **_: _config())
    monkeypatch.setattr(warehouse_module, "DwdService", _FakeDwd)
    return warehouse_module


def test_dwd_show_defaults_to_the_latest_version(fake_dwd) -> None:
    result = _invoke("dwd", "show", "--symbol", "600000")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["symbol"] == "600000"
    assert payload["meta"]["source"] == "dwd"
    assert _FakeDwd.calls == [
        ("show", ("600000",), {"report_date": None, "report_type": None, "version": None})
    ]


def test_dwd_show_requires_a_symbol() -> None:
    result = _invoke("dwd", "show")

    assert result.exit_code == 2


def test_dwd_versions_forwards_the_report_key(fake_dwd) -> None:
    result = _invoke(
        "dwd",
        "versions",
        "--symbol",
        "600000",
        "--report-date",
        "2026-09-30",
        "--report-type",
        "Q3",
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["versions"] == [2, 1]
    assert _FakeDwd.calls == [
        ("versions", ("600000",), {"report_date": "2026-09-30", "report_type": "Q3"})
    ]


def test_dwd_backfill_forwards_dry_run(fake_dwd) -> None:
    result = _invoke("dwd", "backfill", "--from-ods", "--symbol", "600000", "--dry-run")

    assert result.exit_code == 0
    assert _FakeDwd.calls == [
        (
            "backfill",
            (),
            {"from_ods": True, "symbol": "600000", "since": None, "dry_run": True},
        )
    ]


def test_dwd_verify_surfaces_a_partial_failure(fake_dwd) -> None:
    result = _invoke("dwd", "verify", "--symbol", "600000")

    assert result.exit_code == 8
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_PARTIAL_FAILURE"
    assert payload["error"]["details"]["failed"] == 1


# -- dws: build-time default basis (QUOT-244) --------------------------------


class _FakeDws:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __init__(self, **_: object) -> None:
        self.closed = False

    def build(self, symbols, **kwargs: object):
        _FakeDws.calls.append((tuple(symbols), kwargs))

        class _Report:
            failed = False

            def payload(self) -> dict[str, object]:
                return {"counts": {"built": len(symbols), "skipped": 0, "failed": 0}}

        return _Report()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_dws(monkeypatch):
    from financecoffer.cli.commands import warehouse as warehouse_module

    _FakeDws.calls = []
    monkeypatch.setattr(warehouse_module, "load_cli_config", lambda **_: _config())
    monkeypatch.setattr(warehouse_module, "DwsService", _FakeDws)
    return warehouse_module


def test_dws_build_forwards_the_valuation_basis(fake_dws) -> None:
    result = _invoke("dws", "build", "--symbol", "600000", "--valuation-basis", "reference")

    assert result.exit_code == 0
    assert _FakeDws.calls[0][1]["default_basis"] == "reference"


def test_dws_build_without_the_flag_leaves_the_choice_to_config(fake_dws) -> None:
    result = _invoke("dws", "build", "--symbol", "600000")

    assert result.exit_code == 0
    assert _FakeDws.calls[0][1]["default_basis"] is None


def test_dws_rebuild_forwards_the_valuation_basis(fake_dws) -> None:
    result = _invoke("dws", "rebuild", "--valuation-basis", "snapshot")

    assert result.exit_code == 0
    assert _FakeDws.calls[0][0] == ()
    assert _FakeDws.calls[0][1]["default_basis"] == "snapshot"
    assert _FakeDws.calls[0][1]["build_all"] is True


def test_dws_build_rejects_an_unknown_basis_before_connecting(monkeypatch) -> None:
    from financecoffer.cli.commands import warehouse as warehouse_module

    # Real DwsService, empty config: the flag must fail before any DB checkout.
    monkeypatch.setattr(warehouse_module, "load_cli_config", lambda **_: _config())

    result = _invoke("dws", "build", "--symbol", "600000", "--valuation-basis", "vibe")

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["field"] == "valuation_basis"


def test_dws_build_fails_fast_on_an_unknown_metrics_profile(monkeypatch) -> None:
    monkeypatch.setenv("FC_METRICS_PROFILE", "recommended")

    result = _invoke("dws", "build", "--symbol", "600000")

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_CONFIG"
    assert payload["error"]["details"]["errors"][0]["field"] == "metrics_profile"


# -- serve: admin / start-all -----------------------------------------------


def test_serve_admin_refuses_a_non_loopback_bind() -> None:
    result = _invoke("serve", "admin", "--host", "0.0.0.0")

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["host"] == "0.0.0.0"


def test_serve_admin_warehouse_summary_normalizes_the_driver_prefix(monkeypatch) -> None:
    from financecoffer.cli.commands import serve as serve_module

    seen: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> bool:
            return False

        def execute(self, _sql: str) -> None:
            pass

        def fetchall(self) -> list[object]:
            return []

        def fetchone(self) -> None:
            return None

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> bool:
            return False

        def cursor(self) -> _Cursor:
            return _Cursor()

    def fake_connect(dsn: str, *, timeout: int = 10) -> _Connection:
        seen.append(dsn)
        return _Connection()

    monkeypatch.setattr(serve_module, "connect", fake_connect)

    summary = serve_module._warehouse_summary(
        "postgresql+psycopg://app:pw@db:5432/financecoffer", timeout=5
    )

    assert seen == ["postgresql://app:pw@db:5432/financecoffer"]
    assert summary["dlq"]["available"] is True


def test_serve_admin_warehouse_summary_degrades_instead_of_raising(monkeypatch) -> None:
    from financecoffer.cli.commands import serve as serve_module

    def boom(_dsn: str, *, timeout: int = 10) -> None:
        raise RuntimeError("driver exploded")

    monkeypatch.setattr(serve_module, "connect", boom)

    summary = serve_module._warehouse_summary("postgresql://db/financecoffer", timeout=5)

    assert summary["dlq"]["available"] is False
    assert "driver exploded" in summary["dlq"]["reason"]
    assert summary["archive"]["available"] is False


def test_start_all_runs_the_api_and_supervises_the_consumer(monkeypatch) -> None:
    from financecoffer.cli.commands import serve as serve_module

    seen: dict[str, object] = {}

    def serve_all(**kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(serve_module, "load_cli_config", lambda **_: _config())
    monkeypatch.setattr(serve_module, "_serve_all", serve_all)

    result = _invoke("start-all")

    assert result.exit_code == 0
    assert seen["consumer"] == "redis"
    assert seen["with_etl"] is True
    assert seen["host"] == "127.0.0.1"


def test_serve_api_workers_export_the_resolved_configuration(monkeypatch) -> None:
    """QUOT-179 / S-03-1: a worker must inherit what the CLI resolved.

    ``--workers N`` hands uvicorn an import string, so each worker re-runs
    ``create_app()`` and resolves from the process environment alone. The
    resolved configuration -- including ``.env`` values and ``--db-url`` -- is
    published before the fork.

    QUOT-195 changes what a *credential* is published as: the reference the
    operator wrote, never the secret it resolves to. The worker resolves for
    itself, so the plaintext never enters an environment block a forked child
    inherits and someone else can read.
    """

    import os

    import uvicorn

    from financecoffer.cli.commands import serve as serve_module
    from financecoffer.config.loader import LoadedConfig

    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(
        serve_module,
        "load_cli_config_detailed",
        lambda **_: LoadedConfig(
            config=_config(minio_secret_key="s3cr3t-key", rate_limit_per_minute=7),
            sources={
                "db_url": "keyring:financecoffer/db_url",
                "minio_secret_key": "file:secrets/minio.txt",
            },
        ),
    )
    isolated = {key: value for key, value in os.environ.items() if not key.startswith("FC_")}
    monkeypatch.setattr(os, "environ", isolated)

    result = _invoke("serve", "api", "--workers", "2")

    assert result.exit_code == 0
    assert captured["kwargs"]["workers"] == 2
    assert captured["kwargs"]["factory"] is True
    assert isolated["FC_DB_URL"] == "keyring:financecoffer/db_url"
    assert isolated["FC_MINIO_SECRET_KEY"] == "file:secrets/minio.txt"
    assert isolated["FC_RATE_LIMIT_PER_MINUTE"] == "7"
    # Unset values stay unset instead of being published as empty strings.
    assert "FC_MCP_TOKEN" not in isolated
    assert "FC_ETL_CONSUMER_NAME" not in isolated
    # The resolved plaintext of a referenced credential is never published.
    assert "s3cr3t-key" not in "".join(f"{k}={v}" for k, v in isolated.items())


def test_serve_api_single_worker_passes_the_built_app_and_exports_nothing(monkeypatch) -> None:
    """The fix is scoped to the multi-worker branch; one worker is unchanged."""

    import os

    import uvicorn

    from financecoffer.cli.commands import serve as serve_module
    from financecoffer.config.loader import LoadedConfig

    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(
        serve_module,
        "load_cli_config_detailed",
        lambda **_: LoadedConfig(config=_config(), sources={"db_url": _DSN}),
    )
    isolated = {key: value for key, value in os.environ.items() if not key.startswith("FC_")}
    monkeypatch.setattr(os, "environ", isolated)

    result = _invoke("serve", "api")

    assert result.exit_code == 0
    assert "workers" not in captured["kwargs"]  # in-process app, no import string
    assert hasattr(captured["args"][0], "routes")  # the FastAPI instance, not an import string
    assert "FC_DB_URL" not in isolated


def test_config_environment_publishes_references_not_plaintext() -> None:
    """A credential field is exported as the reference the operator wrote (QUOT-195).

    Everything else is exported as its resolved value, because a worker needs the
    value; a credential needs only a pointer it can resolve itself.
    """

    from financecoffer.cli.commands import serve as serve_module

    exported = serve_module._config_environment(
        _config(
            minio_secret_key="s3cr3t-key",
            embedding_api_key="emb-key",
            mcp_token="literal-token",
        ),
        {
            "db_url": "keyring:financecoffer/db_url",
            "minio_secret_key": "file:secrets/minio.txt",
            "embedding_api_key": "env:FC_EMB_KEY",
            "mcp_token": "literal-token",
        },
    )

    assert exported["FC_DB_URL"] == "keyring:financecoffer/db_url"
    assert exported["FC_MINIO_SECRET_KEY"] == "file:secrets/minio.txt"
    assert exported["FC_EMBEDDING_API_KEY"] == "env:FC_EMB_KEY"
    # A literal credential stays a literal: nothing here can invent a reference.
    assert exported["FC_MCP_TOKEN"] == "literal-token"
    assert all(name.startswith("FC_") for name in exported)


def test_config_environment_omits_a_credential_with_no_source() -> None:
    """A credential field the loader never saw is left unset, not published empty.

    Every credential field defaults to ``None``, so a worker that inherits nothing
    reaches the same conclusion the CLI did -- and an ``FC_X=`` in the environment
    would mean "unset" to the loader anyway, just noisily.
    """

    from financecoffer.cli.commands import serve as serve_module

    exported = serve_module._config_environment(_config(), {})

    assert "FC_DB_URL" not in exported
    assert "FC_MINIO_SECRET_KEY" not in exported
    assert "FC_MCP_TOKEN" not in exported


def test_start_all_rejects_an_unknown_consumer(monkeypatch) -> None:
    from financecoffer.cli.commands import serve as serve_module

    monkeypatch.setattr(serve_module, "load_cli_config", lambda **_: _config())

    result = _invoke("start-all", "--consumer", "bogus")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"


def test_start_all_refuses_a_non_loopback_bind() -> None:
    result = _invoke("start-all", "--host", "0.0.0.0")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["details"]["host"] == "0.0.0.0"


def test_consumer_supervisor_stops_after_three_fast_crashes() -> None:
    from financecoffer.cli.commands.serve import ConsumerSupervisor

    class _Process:
        def poll(self) -> int:
            return 1

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

    clock = iter([0.0, 1.0, 2.0])
    alerts: list[str] = []
    spawns: list[int] = []

    def spawn() -> _Process:
        spawns.append(len(spawns))
        return _Process()

    supervisor = ConsumerSupervisor(
        spawn=spawn,
        consumer="redis",
        clock=lambda: next(clock),
        alert=alerts.append,
    )
    supervisor.start()

    results = [supervisor.poll() for _ in range(3)]

    assert results == ["restarted", "restarted", "stopped"]
    assert supervisor.stopped is True
    assert len(spawns) == 3
    assert len(alerts) == 1
    assert "keeps serving" in alerts[0]


# -- tool -------------------------------------------------------------------


def test_tool_list_matches_the_mcp_catalogue() -> None:
    result = _invoke("tool", "list")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    tools = {tool["name"]: tool for tool in payload["data"]["tools"]}
    assert set(tools) == {
        "query_structured_data",
        "search_announcements_semantically",
        "get_risk_history",
    }
    assert payload["data"]["count"] == 3
    assert tools["get_risk_history"]["signature"] == "get_risk_history(symbol)"
    assert tools["get_risk_history"]["layer"] == "risk"


def test_tool_list_filters_by_layer() -> None:
    result = _invoke("tool", "list", "--layer", "vector")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["layer"] == "vector"
    assert [tool["name"] for tool in payload["data"]["tools"]] == [
        "search_announcements_semantically"
    ]


def test_tool_list_rejects_an_unknown_layer() -> None:
    result = _invoke("tool", "list", "--layer", "bogus")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"


def test_tool_call_dispatches_with_the_mcp_signature(monkeypatch) -> None:
    from financecoffer.cli.commands import tool as tool_module
    from financecoffer.core.envelope import Meta

    calls: list[tuple[str, object, dict[str, object]]] = []

    class _Result:
        def __init__(self, data: dict[str, object]) -> None:
            self.data = data

        def meta(self, trace_id: str | None = None) -> Meta:
            return Meta(source="dws")

    class _Service:
        def __init__(self, **_: object) -> None:
            pass

        def structured(self, symbol: str, **kwargs: object) -> _Result:
            calls.append(("structured", symbol, kwargs))
            return _Result({"symbol": symbol, "years": [2025], "revenue": "1.0"})

    monkeypatch.setattr(tool_module, "load_cli_config", lambda **_: _config())
    monkeypatch.setattr(tool_module, "QueryService", _Service)

    result = _invoke(
        "tool",
        "call",
        "query_structured_data",
        "--params",
        '{"symbol": "600000", "metric": "revenue", "year_range": "2024-2025"}',
        "--fields",
        "symbol,years",
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"] == {"symbol": "600000", "years": [2025]}
    assert calls == [("structured", "600000", {"metric": "revenue", "year_range": "2024-2025"})]


def test_tool_call_rejects_unknown_parameters() -> None:
    result = _invoke(
        "tool", "call", "query_structured_data", "--params", '{"symbol": "600000", "bogus": 1}'
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["unknown"] == ["bogus"]


def test_tool_call_rejects_invalid_json_params() -> None:
    result = _invoke("tool", "call", "query_structured_data", "--params", "not-json")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["details"]["field"] == "params"


def test_tool_call_rejects_an_out_of_enum_metric() -> None:
    result = _invoke(
        "tool",
        "call",
        "query_structured_data",
        "--params",
        '{"symbol": "600000", "metric": "bogus"}',
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["parameter"] == "metric"


def test_tool_call_rejects_an_unknown_tool() -> None:
    result = _invoke("tool", "call", "no_such_tool")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"


# -- ingest: backfill / webhook ---------------------------------------------


def _event_payload(
    *, provider: str = "tickhub-cn", crawl_time: str = "2026-10-27T02:00:00Z"
) -> dict[str, object]:
    from financecoffer import events

    return {
        "schema_version": "1.0",
        "event_id": events.compute_event_id(
            provider=provider, data_type="financial", symbol="600000", biz_key="2026Q3-600000"
        ),
        "biz_key": "2026Q3-600000",
        "event_time": "2026-10-27T10:00:00+08:00",
        "data_type": "financial",
        "symbol": "600000",
        "market": "CN",
        "source_info": {
            "provider": provider,
            "crawl_url": "https://example.com/ann/1",
            "crawl_time": crawl_time,
        },
        "content": {
            "quality_metrics": {"quality_status": "normal", "confidence": "high"},
            "structured_fields": {
                "report_date": "2026-09-30",
                "report_type": "Q3",
                "amount_revenue": "1234567890123.4567",
                "amount_net_profit": "-5.5000",
            },
        },
        "trace_id": "trace-cli",
    }


def test_ingest_backfill_dry_run_filters_local_events(tmp_path, monkeypatch) -> None:
    from financecoffer.cli.commands import ingest as ingest_module

    (tmp_path / "event.json").write_text(json.dumps(_event_payload()), encoding="utf-8")
    monkeypatch.setattr(ingest_module, "load_cli_config", lambda **_: _config())

    result = _invoke(
        "ingest",
        "backfill",
        "--source",
        "tickhub-cn",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-31",
        "--input",
        str(tmp_path),
        "--dry-run",
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["scanned"] == 1
    assert data["selected"] == 1
    assert data["landed"] == 0
    assert data["would_write"] == 1
    assert data["dry_run"] is True


def test_ingest_backfill_writes_through_the_event_log(tmp_path, monkeypatch) -> None:
    from financecoffer.cli.commands import ingest as ingest_module

    (tmp_path / "event.json").write_text(json.dumps(_event_payload()), encoding="utf-8")
    persisted: list[dict[str, object]] = []

    def persist(_config, validated, _raw, *, source=None):
        persisted.append({"event_id": validated.event_id, "source": source})
        return {"duplicate": False, "event_id": validated.event_id}

    monkeypatch.setattr(ingest_module, "load_cli_config", lambda **_: _config())
    monkeypatch.setattr(ingest_module, "_persist", persist)

    result = _invoke(
        "ingest",
        "backfill",
        "--source",
        "tickhub-cn",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-31",
        "--input",
        str(tmp_path),
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["landed"] == 1
    assert data["duplicates"] == 0
    assert persisted[0]["source"] == "backfill"


def test_ingest_backfill_rejects_an_inverted_window() -> None:
    result = _invoke(
        "ingest",
        "backfill",
        "--source",
        "tickhub-cn",
        "--from",
        "2026-11-01",
        "--to",
        "2026-10-01",
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"


def test_ingest_backfill_rejects_an_unknown_source() -> None:
    result = _invoke(
        "ingest", "backfill", "--source", "bogus", "--from", "2026-10-01", "--to", "2026-10-31"
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["details"]["field"] == "source"


def test_ingest_webhook_requires_a_token_off_loopback() -> None:
    result = _invoke("ingest", "webhook", "--host", "0.0.0.0")

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["host"] == "0.0.0.0"


def test_webhook_rejects_requests_without_the_bearer_token(monkeypatch) -> None:
    from starlette.testclient import TestClient

    from financecoffer.cli.commands import ingest as ingest_module

    webhook = ingest_module.build_webhook_app(_config(), token="s3cret")
    response = TestClient(webhook).post("/events", json={"data_type": "financial"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E_AUTH"


def test_webhook_enforces_the_host_allowlist(monkeypatch) -> None:
    from starlette.testclient import TestClient

    from financecoffer.cli.commands import ingest as ingest_module

    webhook = ingest_module.build_webhook_app(
        _config(), token="s3cret", allowed_hosts=("api.internal",)
    )
    response = TestClient(webhook).post(
        "/",
        json={},
        headers={"Authorization": "Bearer s3cret", "host": "evil.example"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["host"] == "evil.example"


def test_webhook_lands_a_valid_payload(monkeypatch) -> None:
    from starlette.testclient import TestClient

    from financecoffer.cli.commands import ingest as ingest_module

    monkeypatch.setattr(
        ingest_module,
        "_ingest_event",
        lambda _config, _payload, _text: {"event_id": "evt_test", "status": "landed"},
    )
    webhook = ingest_module.build_webhook_app(_config(), token="s3cret")
    response = TestClient(webhook).post(
        "/events",
        json={"data_type": "financial"},
        headers={"Authorization": "Bearer s3cret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["event_id"] == "evt_test"


# -- skill ------------------------------------------------------------------


def test_skill_install_copies_the_bundle(tmp_path) -> None:
    result = _invoke("skill", "install", "--path", str(tmp_path))

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    destination = tmp_path / "financecoffer"
    assert (destination / "SKILL.md").is_file()
    assert data["overwritten"] == 0
    assert "SKILL.md" in data["files"]


def test_skill_install_requires_force_to_overwrite(tmp_path) -> None:
    assert _invoke("skill", "install", "--path", str(tmp_path)).exit_code == 0

    blocked = _invoke("skill", "install", "--path", str(tmp_path))

    assert blocked.exit_code == 2
    payload = json.loads(blocked.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["hint"] == "--force"

    forced = _invoke("skill", "install", "--path", str(tmp_path), "--force")

    assert forced.exit_code == 0
    assert json.loads(forced.stdout)["data"]["overwritten"] > 0


def test_skill_install_requires_a_target_or_path() -> None:
    result = _invoke("skill", "install")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"


def test_skill_install_rejects_a_path_that_is_not_a_directory(tmp_path) -> None:
    # B5-N3: a regular file passed to --path crashed with a bare traceback;
    # it must surface as the usual invalid-argument envelope.
    target = tmp_path / "iam_a_file"
    target.write_text("x", encoding="utf-8")

    result = _invoke("skill", "install", "--path", str(target))

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert "Traceback" not in result.stdout


def test_skill_install_maps_a_write_failure_to_config_error(tmp_path, monkeypatch) -> None:
    from financecoffer.cli.commands import skill as skill_module

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(skill_module.shutil, "copy2", _boom)

    result = _invoke("skill", "install", "--path", str(tmp_path))

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_CONFIG"
    assert "Traceback" not in result.stdout


def test_skill_install_help_documents_created_directories() -> None:
    # B5-06: --help must say the install root is created when missing.
    result = _invoke("skill", "install", "--help")

    assert result.exit_code == 0
    text = " ".join(result.stdout.split())
    assert "missing directories are created automatically" in text
    assert "created if missing" in text


# -- archive: scope filters and the --yes gate ------------------------------


class _FakeArchive:
    """Just enough of ``ArchiveService`` to observe what the CLI asks for."""

    instances: list["_FakeArchive"] = []

    def __init__(self, **_: object) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        _FakeArchive.instances.append(self)

    def count_candidates(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("count_candidates", kwargs))
        return {"candidates": 4, "tables": []}

    def count_restore(self, symbol: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("count_restore", {"symbol": symbol, **kwargs}))
        return {"writes": 2}

    def run(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("run", kwargs))
        return {"failed": 0, "archived": 0, "dry_run": kwargs["dry_run"]}

    def restore(self, symbol: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("restore", {"symbol": symbol, **kwargs}))
        return {"failed": 0, "restored": 0, "dry_run": kwargs.get("dry_run")}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_archive(monkeypatch):
    from financecoffer.cli.commands import archive as archive_module

    _FakeArchive.instances = []
    monkeypatch.setattr(archive_module, "load_cli_config", lambda **_: _config())
    monkeypatch.setattr(archive_module, "ArchiveService", _FakeArchive)
    return _FakeArchive


def test_archive_run_without_yes_is_refused_before_any_write(fake_archive) -> None:
    result = _invoke("archive", "run")

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["field"] == "yes"
    assert payload["error"]["details"]["hint"] == "--yes"
    assert payload["error"]["details"]["candidates"] == 4
    assert "--yes" in payload["error"]["message"]
    assert "4 candidate rows in scope" in payload["error"]["message"]

    # Only the read-only count ran; nothing was archived.
    assert [name for name, _ in fake_archive.instances[0].calls] == ["count_candidates"]
    assert all(instance.closed for instance in fake_archive.instances)


def test_archive_run_with_yes_forwards_the_scope(fake_archive) -> None:
    result = _invoke(
        "archive",
        "run",
        "--yes",
        "--symbol",
        "600000",
        "--table",
        "stock_announcement",
    )

    assert result.exit_code == 0, result.stdout
    calls = fake_archive.instances[0].calls
    assert [name for name, _ in calls] == ["run"]
    assert calls[0][1] == {
        "older_than_years": None,
        "min_bytes": 8192,
        "dry_run": False,
        "limit": None,
        "symbols": ["600000"],
        "tables": ["stock_announcement"],
    }


def test_archive_run_dry_run_needs_no_confirmation(fake_archive) -> None:
    result = _invoke("archive", "run", "--dry-run")

    assert result.exit_code == 0, result.stdout
    calls = fake_archive.instances[0].calls
    assert [name for name, _ in calls] == ["run"]
    assert calls[0][1]["dry_run"] is True


def test_archive_run_still_refuses_when_the_scope_cannot_be_counted(
    monkeypatch, fake_archive
) -> None:
    from financecoffer.core.errors import StorageError

    def uncountable(self, **_: object) -> dict[str, object]:
        raise StorageError("warehouse is offline", details={})

    monkeypatch.setattr(_FakeArchive, "count_candidates", uncountable)

    result = _invoke("archive", "run")

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["candidates"] is None
    assert "candidate rows" not in payload["error"]["message"]


def test_archive_run_rejects_an_unknown_table(monkeypatch, fake_archive) -> None:
    from financecoffer.core.errors import InvalidArgumentError

    def reject(self, **_: object) -> dict[str, object]:
        raise InvalidArgumentError(
            "unknown archive table: 'bogus'", details={"field": "table", "table": "bogus"}
        )

    monkeypatch.setattr(_FakeArchive, "count_candidates", reject)

    result = _invoke("archive", "run", "--table", "bogus")

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["field"] == "table"
    assert all(instance.closed for instance in fake_archive.instances)


def test_archive_restore_without_yes_is_refused(fake_archive) -> None:
    result = _invoke("archive", "restore", "--symbol", "600000")

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["field"] == "yes"
    assert payload["error"]["details"]["candidates"] == 2
    assert "2 announcement rows to write back" in payload["error"]["message"]
    assert [name for name, _ in fake_archive.instances[0].calls] == ["count_restore"]
    assert fake_archive.instances[0].calls[0][1] == {
        "symbol": "600000",
        "since": None,
        "until": None,
    }


def test_archive_restore_dry_run_needs_no_confirmation(fake_archive) -> None:
    result = _invoke("archive", "restore", "--symbol", "600000", "--dry-run")

    assert result.exit_code == 0, result.stdout
    calls = fake_archive.instances[0].calls
    assert [name for name, _ in calls] == ["restore"]
    assert calls[0][1]["dry_run"] is True
