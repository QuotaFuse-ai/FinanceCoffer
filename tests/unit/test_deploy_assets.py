"""Static checks for the deployment assets.

These tests never invoke Docker. They parse the compose file, the Prometheus
configuration and the Dockerfile, and assert the safety properties the
deployment contract depends on: pinned images, health checks, startup order,
loopback-only publishing, no plaintext secrets and documented metric names.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from financecoffer.core.config import SECRET_FIELDS, Config

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SCRIPTS = ROOT / "scripts"

SERVICES = {"postgres", "redis", "minio", "migrate", "api", "worker"}
LONG_RUNNING = {"postgres", "redis", "minio", "api", "worker"}

# Metrics named in docs/specs/cli.md section 3.14.
METRICS = {
    "fc_etl_consume_lag_seconds",
    "fc_etl_events_total",
    "fc_dirty_data_total",
    "fc_missing_chunk_total",
    "fc_vector_search_seconds",
    "fc_archive_job_total",
}

_INTERPOLATION = re.compile(r"^\$\{[A-Z0-9_]+(?::[-?][^}]*)?\}$")
_SENSITIVE_KEY = re.compile(r"(PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY|_KEY$|DB_URL)")


def load_compose() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))


def load_rules() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "prometheus" / "rules.yml").read_text(encoding="utf-8"))


def all_rules() -> list[dict[str, Any]]:
    return [rule for group in load_rules()["groups"] for rule in group["rules"]]


def test_compose_declares_expected_services() -> None:
    compose = load_compose()
    assert compose["name"] == "financecoffer"
    assert set(compose["services"]) == SERVICES


def test_images_are_pinned() -> None:
    services = load_compose()["services"]
    assert services["postgres"]["image"].startswith("pgvector/pgvector:pg16")
    for name in ("postgres", "redis", "minio"):
        image = services[name]["image"]
        assert ":" in image, f"{name} image has no tag"
        assert not image.endswith(":latest"), f"{name} must not use a floating tag"


def test_healthchecks_cover_the_documented_endpoints() -> None:
    services = load_compose()["services"]
    for name in LONG_RUNNING:
        assert "healthcheck" in services[name], f"{name} has no healthcheck"
    assert "pg_isready" in " ".join(services["postgres"]["healthcheck"]["test"])
    assert "redis-cli" in " ".join(services["redis"]["healthcheck"]["test"])
    assert "/minio/health/live" in " ".join(services["minio"]["healthcheck"]["test"])
    assert "/health" in " ".join(services["api"]["healthcheck"]["test"])
    assert services["migrate"]["healthcheck"]["disable"] is True


def test_worker_healthcheck_reads_the_consumer_liveness_key() -> None:
    """A reachable Redis is not a running consumer: the probe reads the key the
    consume loop refreshes, not ``PING``."""

    worker = load_compose()["services"]["worker"]["healthcheck"]["test"]
    command = " ".join(worker)

    assert "fc:etl:live:" in command
    assert "FC_EVENT_STREAM" in command
    assert ".ping()" not in command


def test_startup_order() -> None:
    services = load_compose()["services"]

    def conditions(name: str) -> dict[str, str]:
        return {
            dependency: spec["condition"]
            for dependency, spec in services[name]["depends_on"].items()
        }

    assert conditions("migrate") == {"postgres": "service_healthy"}
    api = conditions("api")
    assert api["postgres"] == "service_healthy"
    assert api["redis"] == "service_healthy"
    assert api["minio"] == "service_healthy"
    assert api["migrate"] == "service_completed_successfully"
    worker = conditions("worker")
    assert worker["migrate"] == "service_completed_successfully"
    assert worker["postgres"] == "service_healthy"
    assert worker["redis"] == "service_healthy"


def test_only_api_publishes_a_loopback_port() -> None:
    services = load_compose()["services"]
    publishers = {name for name, spec in services.items() if spec.get("ports")}
    assert publishers == {"api"}
    for entry in services["api"]["ports"]:
        assert isinstance(entry, str) and entry.startswith("127.0.0.1:"), entry


def test_api_container_uses_the_app_factory() -> None:
    entrypoint = load_compose()["services"]["api"]["entrypoint"]
    assert "financecoffer.api.app:create_app" in entrypoint
    assert "--factory" in entrypoint
    assert "0.0.0.0" in entrypoint


def test_compose_has_no_plaintext_secrets() -> None:
    for name, service in load_compose()["services"].items():
        for key, value in (service.get("environment") or {}).items():
            if not _SENSITIVE_KEY.search(key):
                continue
            assert isinstance(value, str), f"{name}.{key} is not a string"
            assert "${" in value, f"{name}.{key} is a literal credential"

    db_url = load_compose()["services"]["api"]["environment"]["FC_DB_URL"]
    userinfo = db_url.split("://", 1)[1].split("@", 1)[0]
    # Removing the interpolations must leave nothing but the user/password colon.
    assert re.sub(r"\$\{[^}]*\}", "", userinfo) == ":"


def test_env_example_ships_defaults_but_no_credentials() -> None:
    entries: dict[str, str] = {}
    for raw in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        entries[key.strip()] = value.strip()

    fc_keys = {key for key in entries if key.startswith("FC_")}
    assert fc_keys == {f"FC_{field.upper()}" for field in Config.model_fields}
    assert {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"} <= set(entries)

    # CHANGE_ME means "the operator must supply this": credentials plus the
    # deployment identity (role, database name, DSN). Everything else ships a
    # value Config already accepts.
    placeholder_keys = {f"FC_{name.upper()}" for name in SECRET_FIELDS} | {
        "FC_DB_URL",
        "FC_REDIS_URL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    }
    for key, value in entries.items():
        if key in placeholder_keys:
            assert value == "CHANGE_ME", f"{key} must stay a placeholder"
        else:
            assert value != "CHANGE_ME", f"{key} must ship a working default"

    # Non-placeholder settings must already satisfy Config, so a template copy
    # can never fail on an enum, boolean or integer again.
    Config(
        **{
            key.lower()[3:]: value
            for key, value in entries.items()
            if key in fc_keys - placeholder_keys
        }
    )

    for secret in SECRET_FIELDS:
        assert f"FC_{secret.upper()}" in entries
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_every_env_example_key_reaches_the_containers() -> None:
    """A template key that compose never forwards is silently dead.

    ``x-app-env`` is the single anchor every application service merges, so it
    is the only place where a key added to ``.env.example`` can silently fail to
    arrive in the containers: the operator edits ``.env``, the process keeps
    running on the ``Config`` default, and nothing reports the mismatch. Reading
    the per-service ``environment`` maps instead would hide exactly that case.
    """

    template_keys: set[str] = set()
    for raw in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("FC_"):
            template_keys.add(key)

    app_env = load_compose()["x-app-env"]
    missing = template_keys - set(app_env)
    assert not missing, f"deploy/.env.example keys absent from x-app-env: {sorted(missing)}"

    # The ETL tuning keys arrived after the first template, so env files written
    # from the old one do not carry them: they must fall back to the Config
    # default instead of making ``docker compose`` refuse to start.
    for key in sorted(name for name in template_keys if name.startswith("FC_ETL_")):
        assert ":-" in app_env[key], f"{key} must fall back to a default, not fail"


def test_gitignore_covers_local_overrides() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.local.cmd" in ignore
    assert "*.local.sh" in ignore


def test_prometheus_scrapes_the_api() -> None:
    config = yaml.safe_load((DEPLOY / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
    assert config["rule_files"] == ["rules.yml"]
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    job = jobs["financecoffer-api"]
    assert job["metrics_path"] == "/metrics"
    assert job["static_configs"][0]["targets"] == ["api:8100"]


def test_alert_rules_reference_known_metrics() -> None:
    rules = all_rules()
    for rule in rules:
        referenced = set(re.findall(r"\bfc_[a-z0-9_]+", rule["expr"]))
        assert referenced <= METRICS, f"{rule['alert']} references unknown metrics: {referenced}"

    expressions = {rule["alert"]: rule["expr"] for rule in rules}
    assert expressions["FinanceCofferEtlConsumeLagHigh"] == "fc_etl_consume_lag_seconds > 60"
    assert (
        expressions["FinanceCofferEtlEventsFailed"]
        == 'increase(fc_etl_events_total{status="failed"}[5m]) > 0'
    )
    assert expressions["FinanceCofferMissingChunk"] == "fc_missing_chunk_total > 0"
    assert (
        expressions["FinanceCofferArchiveJobFailed"]
        == 'increase(fc_archive_job_total{status="failed"}[5m]) > 0'
    )


def test_every_alert_metric_is_explained_in_the_readme() -> None:
    """An alert on a metric the operator manual never mentions is a silent alert.

    The README's 监控 section is the only place that says which of these
    families the API actually exports and which have no sampling point in this
    topology, so every name used by a rule has to be there.
    """

    text = (DEPLOY / "README.md").read_text(encoding="utf-8")
    for rule in all_rules():
        for name in re.findall(r"\bfc_[a-z0-9_]+", rule["expr"]):
            assert name in text, f"{rule['alert']} alerts on an undocumented metric: {name}"


def test_readme_lists_the_exported_metric_sources() -> None:
    text = (DEPLOY / "README.md").read_text(encoding="utf-8")
    for name in (
        "fc_api_requests_total",
        "fc_etl_events_total",
        "fc_dirty_data_total",
        "fc_archive_job_total",
        "fc_etl_consume_lag_seconds",
    ):
        assert name in text, f"deploy/README.md does not list {name}"


def test_dockerfile_is_multistage_nonroot_and_healthy() -> None:
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert text.count("\nFROM ") >= 2
    assert "AS builder" in text and "AS runtime" in text
    assert '".[api,db,etl,vector,s3,mcp]"' in text
    assert "useradd" in text and "USER 10001:10001" in text
    assert "HEALTHCHECK" in text and "/health" in text
    assert 'ENTRYPOINT ["fcoffer"]' in text


def test_s3_extra_is_declared() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]
    assert any(requirement.startswith("boto3") for requirement in extras["s3"])


@pytest.mark.parametrize("verb", ["start", "stop", "status"])
@pytest.mark.parametrize("suffix", [".cmd", ".sh"])
def test_local_launcher_wrapper_exists(verb: str, suffix: str) -> None:
    script = SCRIPTS / f"{verb}-local{suffix}"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert f"{verb}" in text
    assert "local_runner.py" in text
    assert "Administrator" not in text
    assert not re.search(r"[A-Za-z]:\\", text), "machine-specific path in committed script"
    if suffix == ".sh":
        assert "set -eu" in text


def test_local_runner_supports_all_commands() -> None:
    text = (SCRIPTS / "local_runner.py").read_text(encoding="utf-8")
    for fragment in ('"start"', '"stop"', '"status"', "api.pid", "FC_STATE_DIR"):
        assert fragment in text


def test_deploy_readme_documents_the_required_sections() -> None:
    text = (DEPLOY / "README.md").read_text(encoding="utf-8")
    for heading in ("依赖顺序", "环境变量", "首次启动", "升级与回滚", "常见故障"):
        assert heading in text
