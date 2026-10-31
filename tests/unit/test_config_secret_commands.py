"""``fcoffer config secret set|list|delete`` (QUOT-195).

The three commands are the operator's only way to move a credential *into* a
store, so the contract that matters is: the secret arrives through an interactive
prompt (never argv), and no command ever echoes a value -- ``list`` reports the
store and the target, ``set`` reports the reference to paste into ``.env``,
``delete`` refuses to destroy anything without ``--confirm``.

The credential store is always a fake: these tests must not touch the machine's
real Credential Manager.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from financecoffer.cli import app

RUNNER = CliRunner()

ACCESS = "sentinel-access-key"
SECRET = "sentinel-secret-key"
TOKEN = "sentinel-mcp-token"


class FakeKeyring:
    """Stand-in credential store; the same shape ``core.secrets`` expects."""

    name = "fake-keyring"

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get(self, service: str, account: str) -> str | None:
        return self.entries.get((service, account))

    def set(self, service: str, account: str, secret: str) -> None:
        self.entries[(service, account)] = secret

    def delete(self, service: str, account: str) -> bool:
        return self.entries.pop((service, account), None) is not None

    def find(self, service: str) -> list[str]:
        return [account for (name, account) in self.entries if name == service]


@pytest.fixture
def keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    from financecoffer.cli.commands import config_secret

    backend = FakeKeyring()
    monkeypatch.setattr(config_secret, "default_keyring_backend", lambda: backend)
    return backend


def _payload(result: Result) -> dict[str, Any]:
    """Read the envelope, skipping the interactive prompt the runner captured."""

    return json.loads(result.stdout[result.stdout.index("{") :])


def _data(result: Result) -> dict[str, Any]:
    return _payload(result)["data"]


def _assert_no_secret(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text


# -- set --------------------------------------------------------------------


def test_set_file_writes_the_file_and_prints_only_the_reference(
    tmp_path, keyring: FakeKeyring
) -> None:
    target = tmp_path / "minio_secret.txt"

    result = RUNNER.invoke(
        app,
        ["config", "secret", "set", "minio_secret_key", "--store", "file", "--file", str(target)],
        input=f"{SECRET}\n{SECRET}\n",
    )

    assert result.exit_code == 0, result.stdout
    data = _data(result)
    assert data["reference"] == f"file:{target}"
    assert target.read_text(encoding="utf-8") == SECRET
    _assert_no_secret(result.stdout, SECRET)


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is a Windows facility")
def test_set_file_dpapi_stores_an_encrypted_blob(
    monkeypatch: pytest.MonkeyPatch, tmp_path, keyring: FakeKeyring
) -> None:
    from financecoffer.core.wincred import DPAPI_MARKER

    target = tmp_path / "mcp_token.dpapi.txt"

    result = RUNNER.invoke(
        app,
        [
            "config",
            "secret",
            "set",
            "mcp_token",
            "--store",
            "file",
            "--file",
            str(target),
            "--dpapi",
        ],
        input=f"{TOKEN}\n{TOKEN}\n",
    )

    assert result.exit_code == 0, result.stdout
    stored = target.read_bytes()
    assert stored.startswith(DPAPI_MARKER.encode("ascii"))
    _assert_no_secret(stored.decode("utf-8", errors="replace") + result.stdout, TOKEN)

    # What the operator does next: point the field at the file. Listing resolves
    # it, so a successful entry is proof the protected blob decrypts.
    monkeypatch.setenv("FC_MCP_TOKEN", f"file:{target}")
    listed = RUNNER.invoke(app, ["config", "secret", "list"])
    assert listed.exit_code == 0, listed.stdout
    entry = next(item for item in _data(listed)["fields"] if item["env"] == "FC_MCP_TOKEN")
    assert entry["status"] == "ok"
    _assert_no_secret(listed.stdout, TOKEN)


def test_set_keyring_stores_under_service_and_account(tmp_path, keyring: FakeKeyring) -> None:
    result = RUNNER.invoke(
        app,
        ["config", "secret", "set", "minio_access_key", "--service", "financecoffer"],
        input=f"{ACCESS}\n{ACCESS}\n",
    )

    assert result.exit_code == 0, result.stdout
    data = _data(result)
    assert data["reference"] == "keyring:financecoffer/minio_access_key"
    assert keyring.entries == {("financecoffer", "minio_access_key"): ACCESS}
    _assert_no_secret(result.stdout, ACCESS)


def test_set_refuses_an_unknown_field(tmp_path, keyring: FakeKeyring) -> None:
    result = RUNNER.invoke(app, ["config", "secret", "set", "redis_url"], input="x\nx\n")

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_INVALID_ARGUMENT"
    assert payload["error"]["details"]["valid"] == [
        "FC_DB_URL",
        "FC_EMBEDDING_API_KEY",
        "FC_MINIO_ACCESS_KEY",
        "FC_MINIO_SECRET_KEY",
        "FC_MCP_TOKEN",
    ]


def test_set_requires_file_for_the_file_store(tmp_path, keyring: FakeKeyring) -> None:
    result = RUNNER.invoke(
        app, ["config", "secret", "set", "mcp_token", "--store", "file"], input="x\nx\n"
    )

    assert result.exit_code == 2
    assert "file" in json.loads(result.stdout)["error"]["message"]


def test_set_rejects_a_non_credential_store(tmp_path, keyring: FakeKeyring) -> None:
    result = RUNNER.invoke(
        app, ["config", "secret", "set", "mcp_token", "--store", "vault"], input="x\nx\n"
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"


# -- list -------------------------------------------------------------------


def test_list_reports_references_without_a_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path, keyring: FakeKeyring
) -> None:
    stored = tmp_path / "access.txt"
    stored.write_text(ACCESS, encoding="utf-8")
    keyring.set("financecoffer", "mcp_token", TOKEN)
    monkeypatch.setenv("FC_MINIO_ACCESS_KEY", f"file:{stored}")
    monkeypatch.setenv("FC_MCP_TOKEN", "keyring:financecoffer/mcp_token")
    monkeypatch.setenv("FC_MINIO_SECRET_KEY", SECRET)

    result = RUNNER.invoke(app, ["config", "secret", "list"])

    assert result.exit_code == 0, result.stdout
    data = _data(result)
    fields = {item["env"]: item for item in data["fields"]}
    assert fields["FC_MINIO_ACCESS_KEY"]["type"] == "file"
    assert fields["FC_MINIO_ACCESS_KEY"]["target"] == str(stored)
    assert fields["FC_MINIO_ACCESS_KEY"]["status"] == "ok"
    assert fields["FC_MCP_TOKEN"]["type"] == "keyring"
    assert fields["FC_MCP_TOKEN"]["target"] == "financecoffer/mcp_token"
    assert fields["FC_MINIO_SECRET_KEY"]["type"] == "literal"
    assert "FC_EMBEDDING_API_KEY" in data["unset"]
    _assert_no_secret(result.stdout, ACCESS, TOKEN, SECRET)


def test_list_flags_a_reference_that_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch, keyring: FakeKeyring
) -> None:
    monkeypatch.setenv("FC_DB_URL", "keyring:financecoffer/nothing_here")

    result = RUNNER.invoke(app, ["config", "secret", "list"])

    assert result.exit_code == 0, result.stdout
    entry = _data(result)["fields"][0]
    assert entry["status"] == "unresolved"
    assert "no fake-keyring entry" in entry["reason"]


def test_list_names_a_malformed_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC_MCP_TOKEN", "keyring:onlyservice")

    result = RUNNER.invoke(app, ["config", "secret", "list"])

    assert result.exit_code == 0, result.stdout
    entry = next(item for item in _data(result)["fields"] if item["env"] == "FC_MCP_TOKEN")
    assert entry["type"] == "invalid"
    assert entry["status"] == "error"


# -- delete -----------------------------------------------------------------


def test_delete_file_removes_the_material(
    monkeypatch: pytest.MonkeyPatch, tmp_path, keyring: FakeKeyring
) -> None:
    stored = tmp_path / "secret.txt"
    stored.write_text(SECRET, encoding="utf-8")
    monkeypatch.setenv("FC_MINIO_SECRET_KEY", f"file:{stored}")

    result = RUNNER.invoke(app, ["config", "secret", "delete", "minio_secret_key", "--confirm"])

    assert result.exit_code == 0, result.stdout
    assert _data(result)["removed"] is True
    assert not stored.exists()


def test_delete_keyring_removes_the_entry(
    monkeypatch: pytest.MonkeyPatch, keyring: FakeKeyring
) -> None:
    keyring.set("financecoffer", "mcp_token", TOKEN)
    monkeypatch.setenv("FC_MCP_TOKEN", "keyring:financecoffer/mcp_token")

    result = RUNNER.invoke(app, ["config", "secret", "delete", "mcp_token", "--confirm"])

    assert result.exit_code == 0, result.stdout
    assert keyring.entries == {}
    assert _data(result)["target"] == "financecoffer/mcp_token"


def test_delete_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path, keyring: FakeKeyring
) -> None:
    stored = tmp_path / "secret.txt"
    stored.write_text(SECRET, encoding="utf-8")
    monkeypatch.setenv("FC_MINIO_SECRET_KEY", f"file:{stored}")

    result = RUNNER.invoke(app, ["config", "secret", "delete", "minio_secret_key"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_INVALID_ARGUMENT"
    assert stored.exists()


def test_delete_refuses_a_literal_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC_MCP_TOKEN", TOKEN)

    result = RUNNER.invoke(app, ["config", "secret", "delete", "mcp_token", "--confirm"])

    assert result.exit_code == 2
    assert "literal" in json.loads(result.stdout)["error"]["message"]
    _assert_no_secret(result.stdout, TOKEN)


def test_delete_reports_a_missing_file_without_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path, keyring: FakeKeyring
) -> None:
    monkeypatch.setenv("FC_MINIO_SECRET_KEY", f"file:{tmp_path / 'gone.txt'}")

    result = RUNNER.invoke(app, ["config", "secret", "delete", "minio_secret_key", "--confirm"])

    assert result.exit_code == 0, result.stdout
    assert _data(result)["removed"] is False
