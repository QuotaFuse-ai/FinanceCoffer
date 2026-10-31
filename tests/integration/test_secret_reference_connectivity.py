"""Integration: a credential *reference* reaches a real consumer (QUOT-195).

One case per credential field, each with the field configured as a reference and
each driving the consumer that actually has to authenticate -- a DSN that opens a
connection, MinIO credentials that sign a request, a bearer token that passes the
MCP middleware, an API key that arrives in an ``Authorization`` header. A resolver
that returns a string is not enough; the string has to work.

Two rules hold throughout, and they are the reason these cases are written the way
they are:

* the keyring is always a fake backend, and ``file:`` always points at a temp
  file, so no test reads or writes this machine's Credential Manager;
* a real credential value is moved *into* the store under test, never invented --
  MinIO needs the deployment's own keys, so they are read from the local ``.env``
  exactly as ``test_archive.py`` does. Nothing here prints one: assertions compare
  digests and names, and a failure message carries at most a database name.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from financecoffer.archive import S3Storage
from financecoffer.config.loader import load_config
from financecoffer.core.config import Config
from financecoffer.db import connect
from financecoffer.mcp.server import BearerAuthMiddleware
from financecoffer.vector.embeddings import EmbeddingClient

ROOT = Path(__file__).resolve().parents[2]

#: Values this file invents. They are sent to a mock transport only.
FAKE_EMBEDDING_KEY = "quota195-sentinel-embedding-key"
FAKE_MCP_TOKEN = "quota195-sentinel-mcp-token"


class FakeKeyring:
    """Credential store standing in for the OS one, keyed by (service, account)."""

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


def _url(path: Path) -> str:
    """A ``file:`` reference target a Windows path can be read back through.

    Forward slashes are what ``dotenv`` and the reference grammar round-trip
    reliably; ``Path`` accepts them on every platform.
    """

    return path.as_posix()


def _file_reference(tmp_path: Path, name: str, secret: str) -> tuple[str, str]:
    path = tmp_path / name
    path.write_text(secret, encoding="utf-8")
    return f"file:{_url(path)}", secret


# -- the deployment-shaped services this suite is allowed to touch -----------


def _reachable(endpoint: str) -> bool:
    cleaned = endpoint.replace("http://", "").replace("https://", "")
    host, _, port = cleaned.partition(":")
    try:
        socket.create_connection((host, int(port or 9000)), 2).close()
    except OSError:
        return False
    return True


@pytest.fixture(scope="module")
def live() -> Config:
    """The local deployment configuration, for the credential *values* it holds."""

    env_file = ROOT / ".env"
    try:
        settings = load_config(env_file=env_file) if env_file.is_file() else load_config()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot load the deployment configuration: {type(exc).__name__}")
    return settings


@pytest.fixture(scope="module")
def minio(live: Config) -> Config:
    """The live store, and proof that it really checks these credentials.

    Both halves of the baseline are required, because a green test that cannot go
    red is worthless: some local HTTP services answer ``404`` to everything, and
    such an endpoint makes every credential look valid. So the fixture demands
    that the configured keys authenticate *and* that a wrong secret is refused, and
    skips the module when the endpoint cannot tell them apart.
    """

    if not _reachable(live.minio_endpoint):
        pytest.skip(f"MinIO is not reachable at {live.minio_endpoint}")
    access, secret = live.minio_access_key, live.minio_secret_key
    if access is None or secret is None:
        pytest.skip("MinIO credentials are not configured locally")
    if not _accepts(live):
        pytest.skip("the locally configured MinIO credentials do not authenticate")
    if _accepts(_with_wrong_secret(live)):
        pytest.skip(
            f"{live.minio_endpoint} does not verify signatures; credential cases would be vacuous"
        )
    return live


def _with_wrong_secret(config: Config) -> Config:
    """The same store, reached with a secret that cannot be right."""

    return Config(
        db_url=config.db_url,
        minio_endpoint=config.minio_endpoint,
        minio_bucket=config.minio_bucket,
        minio_access_key=config.minio_access_key,
        minio_secret_key="quota195-not-the-secret",
    )


def _accepts(config: Config) -> bool:
    """True when the store answers a read signed with these credentials.

    ``head`` on an absent key is the cheapest round trip: it authenticates, and it
    creates nothing. A bad secret is a 403, which ``S3Storage`` does not treat as
    "missing", so the two outcomes are distinguishable.
    """

    try:
        S3Storage(config).head("test/quota195/probe")
    except Exception:  # noqa: BLE001 - the distinction is raised vs not raised
        return False
    return True


# -- db_url: file: -> a real connection -------------------------------------


def test_db_url_reference_opens_the_database_it_names(
    migrated_warehouse: str, tmp_path: Path
) -> None:
    reference, dsn = _file_reference(tmp_path, "db_url.txt", migrated_warehouse)

    config = load_config(env={"FC_DB_URL": reference}, backend=FakeKeyring())

    assert config.db_url is not None
    assert config.db_url == dsn
    connection = connect(config.db_url, timeout=10)
    try:
        row = connection.execute("select current_database()").fetchone()
    finally:
        connection.close()
    # Not merely "a connection": the scratch database the file names.
    assert row is not None and row[0] == dsn.rsplit("/", 1)[-1]


def test_db_url_reference_keeps_its_password_masked(tmp_path: Path) -> None:
    """The password a reference resolved is still masked everywhere it could leak.

    A sentinel DSN, not the scratch one: its test password is ``fcoffer``, which
    also appears as the username and the database name, so masking it proves
    nothing. The password below appears nowhere but in the file.
    """

    dsn = "postgresql://fcoffer_sentinel:zq195-not-a-real-password@127.0.0.1:5432/fcoffer_sentinel"
    reference, _ = _file_reference(tmp_path, "db_url.txt", dsn)

    config = load_config(env={"FC_DB_URL": reference}, backend=FakeKeyring())

    assert config.db_url == dsn
    password = dsn.partition("://")[2].partition("@")[0].partition(":")[2]
    assert password == "zq195-not-a-real-password"
    assert "file:" not in str(config.db_url)
    for rendered in (json.dumps(config.redacted_dict()), repr(config), str(config)):
        assert password not in rendered
        assert reference not in rendered


# -- minio_access_key: keyring: -> a signed request -------------------------


def test_minio_access_key_from_the_keyring_signs_a_request(minio: Config, tmp_path: Path) -> None:
    access = minio.minio_access_key
    secret = minio.minio_secret_key
    assert access is not None and secret is not None

    backend = FakeKeyring()
    backend.set("financecoffer", "minio_access_key", access.get_secret_value())
    env_file, _ = _file_reference(tmp_path, "minio_secret.txt", secret.get_secret_value())

    config = load_config(
        env={
            "FC_MINIO_ENDPOINT": minio.minio_endpoint,
            "FC_MINIO_BUCKET": minio.minio_bucket,
            "FC_MINIO_ACCESS_KEY": "keyring:financecoffer/minio_access_key",
            "FC_MINIO_SECRET_KEY": env_file,
        },
        backend=backend,
    )

    assert config.minio_access_key is not None
    assert config.minio_access_key.get_secret_value() == access.get_secret_value()
    assert _accepts(config), "the referenced credential did not authenticate"


# -- minio_secret_key: file: -> a signed request ----------------------------


def test_minio_secret_key_from_a_file_signs_a_request(minio: Config, tmp_path: Path) -> None:
    access = minio.minio_access_key
    secret = minio.minio_secret_key
    assert access is not None and secret is not None

    reference, _ = _file_reference(tmp_path, "minio_secret.txt", secret.get_secret_value())

    config = load_config(
        env={
            "FC_MINIO_ENDPOINT": minio.minio_endpoint,
            "FC_MINIO_BUCKET": minio.minio_bucket,
            "FC_MINIO_ACCESS_KEY": access.get_secret_value(),
            "FC_MINIO_SECRET_KEY": reference,
        },
        backend=FakeKeyring(),
    )

    assert _accepts(config), "the referenced credential did not authenticate"


def test_a_wrong_secret_key_from_a_file_is_refused_by_the_store(
    minio: Config, tmp_path: Path
) -> None:
    """The probe is not vacuously green: a bad credential does not authenticate."""

    access = minio.minio_access_key
    assert access is not None
    reference, _ = _file_reference(tmp_path, "wrong.txt", "quota195-not-the-secret")

    config = load_config(
        env={
            "FC_MINIO_ENDPOINT": minio.minio_endpoint,
            "FC_MINIO_BUCKET": minio.minio_bucket,
            "FC_MINIO_ACCESS_KEY": access.get_secret_value(),
            "FC_MINIO_SECRET_KEY": reference,
        },
        backend=FakeKeyring(),
    )

    assert not _accepts(config), "the store accepted a credential it should have refused"


def test_minio_references_are_what_the_client_signs_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both MinIO fields, without a listening store: the keys reach the signer.

    The live cases above are the real proof, but they skip on a machine with no
    MinIO -- which would leave the acceptance criterion unproven rather than
    honestly reported. boto3's arguments are where a credential is either used or
    lost, so this pins the hand-off from the reference to the signature.
    """

    import boto3

    captured: dict[str, Any] = {}

    def fake_client(service: str, **kwargs: Any) -> object:
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(boto3, "client", fake_client)

    backend = FakeKeyring()
    backend.set("financecoffer", "minio_access_key", "quota195-fake-access")
    reference, secret = _file_reference(tmp_path, "minio_secret.txt", "quota195-fake-secret")

    config = load_config(
        env={
            "FC_MINIO_ENDPOINT": "127.0.0.1:9100",
            "FC_MINIO_ACCESS_KEY": "keyring:financecoffer/minio_access_key",
            "FC_MINIO_SECRET_KEY": reference,
        },
        backend=backend,
    )

    S3Storage(config)

    assert captured["service"] == "s3"
    assert captured["aws_access_key_id"] == "quota195-fake-access"
    assert captured["aws_secret_access_key"] == secret
    assert captured["config"].signature_version == "s3v4"


# -- mcp_token: env: -> the bearer middleware accepts it --------------------


def _mcp_status(token: str, supplied: str | None) -> int:
    """One ASGI exchange through the bearer middleware; returns its status code.

    ``supplied`` is the whole ``Authorization`` header value, because that is what
    the middleware compares: it expects the literal ``Bearer <token>``.
    """

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    events: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        events.append(message)

    headers = [] if supplied is None else [(b"authorization", supplied.encode("ascii"))]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8100),
    }
    app = BearerAuthMiddleware(inner, token=token)

    async def call() -> None:
        await app(scope, receive, send)

    anyio.run(call)
    return int(events[0]["status"])


def test_mcp_token_from_the_environment_authenticates_the_http_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env:`` is the explicit fallback, and it resolves in the same source."""

    monkeypatch.setenv("FC_QUOTA195_MCP_TOKEN", FAKE_MCP_TOKEN)

    config = load_config(
        env={"FC_MCP_TOKEN": "env:FC_QUOTA195_MCP_TOKEN", "FC_QUOTA195_MCP_TOKEN": FAKE_MCP_TOKEN},
        backend=FakeKeyring(),
    )

    assert config.mcp_token is not None
    resolved = config.mcp_token.get_secret_value()
    assert resolved == FAKE_MCP_TOKEN
    assert _mcp_status(resolved, f"Bearer {resolved}") == 204
    assert _mcp_status(resolved, "Bearer quota195-not-the-token") == 401
    assert _mcp_status(resolved, None) == 401


# -- embedding_api_key: keyring: -> the Authorization header ----------------


def test_embedding_key_from_the_keyring_reaches_the_request_header(tmp_path: Path) -> None:
    backend = FakeKeyring()
    backend.set("financecoffer", "embedding_api_key", FAKE_EMBEDDING_KEY)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    config = load_config(
        env={
            "FC_EMBEDDING_DIM": "2",
            "FC_EMBEDDING_API_KEY": "keyring:financecoffer/embedding_api_key",
        },
        backend=backend,
    )

    outcome = EmbeddingClient(config, transport=httpx.MockTransport(handler)).embed(["one"])

    assert outcome.ok, outcome.errors
    assert seen == [f"Bearer {FAKE_EMBEDDING_KEY}"]
    # The key arrived through a store, so a dump of the config must not carry it.
    assert FAKE_EMBEDDING_KEY not in json.dumps(config.redacted_dict())
    assert FAKE_EMBEDDING_KEY not in repr(config)


def test_embedding_key_from_a_file_reaches_the_request_header(tmp_path: Path) -> None:
    reference, key = _file_reference(tmp_path, "embedding_key.txt", FAKE_EMBEDDING_KEY)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    config = load_config(
        env={"FC_EMBEDDING_DIM": "2", "FC_EMBEDDING_API_KEY": reference},
        backend=FakeKeyring(),
    )

    outcome = EmbeddingClient(config, transport=httpx.MockTransport(handler)).embed(["one"])

    assert outcome.ok, outcome.errors
    assert seen == [f"Bearer {key}"]


# -- a broken reference never reaches a consumer ----------------------------


def test_a_missing_file_reference_is_a_hard_configuration_error(tmp_path: Path) -> None:
    from financecoffer.core.errors import ConfigError, ErrorCode, ExitCode

    with pytest.raises(ConfigError) as caught:
        load_config(
            env={"FC_DB_URL": f"file:{_url(tmp_path / 'absent.txt')}"},
            backend=FakeKeyring(),
        )

    assert caught.value.code is ErrorCode.E_CONFIG
    assert ExitCode.CONFIG == 3
    assert "FC_DB_URL" in caught.value.message
    assert "file" in caught.value.message
    # The point of the hard failure: no empty value to connect with.
    assert caught.value.details["missing"]


def test_an_absent_keyring_entry_is_a_hard_configuration_error() -> None:
    from financecoffer.core.errors import ConfigError, ErrorCode

    with pytest.raises(ConfigError) as caught:
        load_config(
            env={"FC_MCP_TOKEN": "keyring:financecoffer/mcp_token"},
            backend=FakeKeyring(),
        )

    assert caught.value.code is ErrorCode.E_CONFIG
    assert "no fake-keyring entry" in caught.value.message
    assert caught.value.details["target"] == "financecoffer/mcp_token"
