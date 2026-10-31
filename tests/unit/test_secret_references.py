"""Credential references: grammar, resolution and the loader's hard failure.

QUOT-195. The contract under test is narrow: a credential field may hold
``keyring:`` / ``file:`` / ``env:`` instead of the secret, the loader resolves it
before validation, and every *source* -- ``.env``, the process environment, the
values ``serve api --workers N`` publishes -- keeps the reference, never the
plaintext. A reference that cannot be resolved must stop the load with
``E_CONFIG`` (exit 3) rather than start a service on an empty credential.

Nothing here reads a real credential store: the keyring is a fake backend, files
are temporary, and every value is a sentinel that could not be a real secret.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from financecoffer.config.loader import LoadedConfig, credential_sources, load_config_detailed
from financecoffer.core.errors import ConfigError, ErrorCode, ExitCode
from financecoffer.core.secrets import CREDENTIAL_FIELDS, parse_reference, resolve_reference

FAKE_DSN = "postgresql://fcoffer_sentinel:notasecretpassword@127.0.0.1:5432/fcoffer_sentinel"


class FakeKeyring:
    """A stand-in credential store keyed by ``(service, account)``."""

    name = "fake-keyring"

    def __init__(self, entries: dict[tuple[str, str], str] | None = None) -> None:
        self.entries = dict(entries or {})

    def get(self, service: str, account: str) -> str | None:
        return self.entries.get((service, account))

    def set(self, service: str, account: str, secret: str) -> None:
        self.entries[(service, account)] = secret

    def delete(self, service: str, account: str) -> bool:
        return self.entries.pop((service, account), None) is not None

    def find(self, service: str) -> list[str]:
        return [account for (entry_service, account) in self.entries if entry_service == service]


@pytest.fixture
def sentinels() -> dict[str, str]:
    """One fake secret per credential field, all distinct and all unmistakable."""

    return {
        "db_url": FAKE_DSN,
        "embedding_api_key": "embedding-sentinel-key",
        "minio_access_key": "minio-sentinel-access",
        "minio_secret_key": "minio-sentinel-secret",
        "mcp_token": "mcp-sentinel-token",
    }


# -- grammar ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        FAKE_DSN,
        "postgresql+psycopg://u:p@h/db",
        "redis://127.0.0.1:6379/1",
        "https://user:pw@host/v1",
        "plain-literal-secret",
    ],
)
def test_a_literal_is_not_a_reference(value: str) -> None:
    assert parse_reference(value) is None


@pytest.mark.parametrize(
    ("value", "scheme", "target"),
    [
        ("keyring:financecoffer/db_url", "keyring", "financecoffer/db_url"),
        ("file:C:/secrets/db.txt", "file", "C:/secrets/db.txt"),
        ("file:./relative.txt", "file", "./relative.txt"),
        ("env:FC_QA_DSN", "env", "FC_QA_DSN"),
        ("  keyring:svc/acc  ", "keyring", "svc/acc"),
    ],
)
def test_a_reference_parses_into_scheme_and_target(value: str, scheme: str, target: str) -> None:
    reference = parse_reference(value)
    assert reference is not None
    assert (reference.scheme, reference.target) == (scheme, target)
    assert reference.describe() == target
    assert reference.text == value.strip()


def test_keyring_reference_splits_service_from_account() -> None:
    reference = parse_reference("keyring:financecoffer/minio_secret_key")
    assert reference is not None
    assert (reference.service, reference.account) == ("financecoffer", "minio_secret_key")


@pytest.mark.parametrize(
    "value",
    [
        "keyring:financecoffer",
        "keyring:/db_url",
        "keyring:a/b/c",
        "file:",
        "env:",
        "env:NOT A VARIABLE NAME",
    ],
)
def test_a_malformed_reference_fails_instead_of_becoming_a_literal(value: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_reference(value, field="mcp_token")
    error = excinfo.value
    assert error.code is ErrorCode.E_CONFIG
    assert error.exit_code == int(ExitCode.CONFIG)
    assert "FC_MCP_TOKEN" in error.message
    assert error.details is not None
    assert error.details["field"] == "FC_MCP_TOKEN"


# -- resolution ------------------------------------------------------------


def test_file_reference_reads_the_file(tmp_path: Path, sentinels: dict[str, str]) -> None:
    stored = tmp_path / "db_url.txt"
    stored.write_text(sentinels["db_url"] + "\n", encoding="utf-8")

    resolved = resolve_reference("db_url", f"file:{stored}")

    assert resolved == sentinels["db_url"]


def test_keyring_reference_reads_the_backend() -> None:
    backend = FakeKeyring({("financecoffer", "minio_secret_key"): "minio-sentinel-secret"})

    resolved = resolve_reference(
        "minio_secret_key",
        "keyring:financecoffer/minio_secret_key",
        backend=backend,
    )

    assert resolved == "minio-sentinel-secret"


def test_env_reference_reads_the_named_variable(sentinels: dict[str, str]) -> None:
    resolved = resolve_reference(
        "mcp_token",
        "env:QA_MCP_TOKEN",
        environ={"QA_MCP_TOKEN": sentinels["mcp_token"]},
    )
    assert resolved == sentinels["mcp_token"]


@pytest.mark.parametrize(
    ("value", "missing"),
    [
        ("file:nothing-here.txt", "does not exist"),
        ("keyring:financecoffer/absent", "no fake-keyring entry"),
        ("env:QA_NOT_SET", "is not set"),
    ],
)
def test_an_unresolvable_reference_is_a_hard_config_failure(
    value: str, missing: str, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        resolve_reference("mcp_token", value, environ={}, backend=FakeKeyring())
    error = excinfo.value
    assert error.code is ErrorCode.E_CONFIG
    assert error.exit_code == int(ExitCode.CONFIG)
    assert error.details is not None
    assert error.details["field"] == "FC_MCP_TOKEN"
    assert error.details["reference"] == value.partition(":")[0]
    assert missing in error.message


def test_an_empty_credential_file_is_not_a_blank_secret(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="the file is empty"):
        resolve_reference("embedding_api_key", f"file:{empty}")


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is a Windows facility")
def test_a_dpapi_protected_file_only_yields_its_secret_to_this_user(
    tmp_path: Path, sentinels: dict[str, str]
) -> None:
    from financecoffer.core import wincred

    plain = tmp_path / "plain.txt"
    plain.write_text(sentinels["mcp_token"], encoding="utf-8")
    protected = tmp_path / "protected.dpapi.txt"
    protected.write_bytes(wincred.protect(sentinels["mcp_token"].encode("utf-8")))

    assert protected.read_bytes().startswith(wincred.DPAPI_MARKER.encode("ascii"))
    assert sentinels["mcp_token"].encode() not in protected.read_bytes()
    assert resolve_reference("mcp_token", f"file:{protected}") == sentinels["mcp_token"]
    assert resolve_reference("mcp_token", f"file:{plain}") == sentinels["mcp_token"]


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is a Windows facility")
def test_a_dpapi_file_written_by_another_user_fails_loudly(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.txt"
    corrupt.write_bytes(b"dpapi:v1:not-base64-at-$$")

    with pytest.raises(ConfigError, match="cannot be resolved"):
        resolve_reference("mcp_token", f"file:{corrupt}")


# -- the loader ------------------------------------------------------------


def _env_with_references(
    tmp_path: Path, sentinels: dict[str, str], backend: FakeKeyring
) -> dict[str, str]:
    """Configure every credential field through a reference, none in plaintext."""

    written = {
        "embedding_api_key": tmp_path / "embedding.txt",
        "minio_access_key": tmp_path / "access.txt",
    }
    written["embedding_api_key"].write_text(sentinels["embedding_api_key"], encoding="utf-8")
    written["minio_access_key"].write_text(sentinels["minio_access_key"], encoding="utf-8")
    backend.set("financecoffer", "db_url", sentinels["db_url"])
    return {
        "FC_DB_URL": "keyring:financecoffer/db_url",
        "FC_EMBEDDING_API_KEY": f"file:{written['embedding_api_key']}",
        "FC_MINIO_ACCESS_KEY": f"file:{written['minio_access_key']}",
        "FC_MINIO_SECRET_KEY": "env:QA_MINIO_SECRET",
        "FC_MCP_TOKEN": "keyring:financecoffer/mcp_token",
        "QA_MINIO_SECRET": sentinels["minio_secret_key"],
    }


@pytest.fixture
def configured(tmp_path: Path, sentinels: dict[str, str]) -> LoadedConfig:
    backend = FakeKeyring(
        {
            ("financecoffer", "db_url"): sentinels["db_url"],
            ("financecoffer", "mcp_token"): sentinels["mcp_token"],
        }
    )
    env = _env_with_references(tmp_path, sentinels, backend)
    resolved = load_config_detailed(env=env, backend=backend)
    return resolved


def test_the_loader_resolves_every_credential_field(configured: LoadedConfig) -> None:
    config = configured.config
    assert config.db_url is not None
    assert configured.config.embedding_api_key is not None
    assert config.minio_access_key is not None
    assert config.minio_secret_key is not None
    assert config.mcp_token is not None


def test_the_loader_keeps_the_references_for_the_fork_export(
    configured: LoadedConfig,
) -> None:
    sources = configured.sources

    assert set(sources) == set(CREDENTIAL_FIELDS)
    assert all(raw.startswith(("keyring:", "file:", "env:")) for raw in sources.values())


def test_no_plaintext_appears_in_the_exported_sources(
    configured: LoadedConfig, sentinels: dict[str, str]
) -> None:
    joined = "\n".join(configured.sources.values())
    for secret in sentinels.values():
        assert secret not in joined


def test_config_show_and_repr_stay_redacted(
    configured: LoadedConfig, sentinels: dict[str, str]
) -> None:
    shown = str(configured.config.redacted_dict()) + repr(configured.config)
    for secret in sentinels.values():
        assert secret not in shown


def test_the_dsn_reaches_the_driver_after_resolution(configured: LoadedConfig) -> None:
    """A referenced DSN is validated as a DSN, not as the string ``keyring:...``."""

    assert configured.config.db_url == FAKE_DSN


def test_a_literal_still_loads_as_a_literal() -> None:
    resolved = load_config_detailed(env={"FC_MCP_TOKEN": "literal-token"}, backend=FakeKeyring())
    assert resolved.config.mcp_token is not None
    assert resolved.config.mcp_token.get_secret_value() == "literal-token"
    assert resolved.sources["mcp_token"] == "literal-token"


def test_an_unresolvable_reference_fails_the_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config_detailed(env={"FC_MCP_TOKEN": f"file:{tmp_path / 'missing.txt'}"})
    error = excinfo.value
    assert error.code is ErrorCode.E_CONFIG
    assert error.exit_code == int(ExitCode.CONFIG)
    assert "FC_MCP_TOKEN" in error.message


def test_credential_sources_reports_references_without_resolving_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from financecoffer.config import loader

    env_file = tmp_path / ".env"
    env_file.write_text("FC_MCP_TOKEN=keyring:financecoffer/mcp_token\n", encoding="utf-8")
    monkeypatch.setattr(loader, "default_env_file", lambda *_: env_file)

    sources = credential_sources()

    assert sources == {"mcp_token": "keyring:financecoffer/mcp_token"}
