"""Credential references: grammar, backends and load-time resolution.

A credential field may hold a *reference* instead of the secret itself, so the
plaintext never has to live in a ``.env``, the process environment, a log line or
an error message:

``keyring:<service>/<account>``
    Operating-system credential store -- Windows Credential Manager through
    ``ctypes`` (no third-party dependency, DEC-0031), macOS Keychain and Linux
    Secret Service through the optional ``keyring`` package when it is installed.
``file:<path>``
    The file's contents, minus trailing newlines. On Windows a file written by
    ``fcoffer config secret set --dpapi`` is DPAPI-protected and decrypts only for
    the storing user.
``env:<VAR>``
    An explicit read of another environment variable. Kept for operators who
    inject secrets from a runner secret store; it is the one form whose value
    *does* sit in the process environment.

Anything else is a literal. Only these three lowercase schemes are recognised, so
a DSN such as ``postgresql://user:pass@host/db`` is never mistaken for a
reference. Resolution runs in :func:`financecoffer.config.loader.load_config`
before validation; a reference that cannot be resolved is a hard ``E_CONFIG``
failure -- there is no fallback to an empty value.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol

from financecoffer.core.errors import ConfigError
from financecoffer.core.wincred import DPAPI_MARKER

REFERENCE_SCHEMES: tuple[str, ...] = ("keyring", "file", "env")

#: Fields allowed to hold a reference: every credential the platform accepts from
#: configuration. ``db_url`` is included because its DSN carries the password.
CREDENTIAL_FIELDS: tuple[str, ...] = (
    "db_url",
    "embedding_api_key",
    "minio_access_key",
    "minio_secret_key",
    "mcp_token",
)

#: Default Credential Manager service for ``fcoffer config secret set``.
DEFAULT_KEYRING_SERVICE = "financecoffer"

#: A credential that resolves to more than this is pointing at the wrong thing
#: (a log file, a binary), and the mistake is caught before it reaches a driver.
_MAX_RESOLVED_LENGTH = 8192

_SYNTAX_HINT = (
    "expected keyring:<service>/<account>, file:<path> or env:<VAR>; "
    "any other value is the literal secret"
)


class KeyringBackend(Protocol):
    """A credential store addressable by ``(service, account)``."""

    name: str

    def get(self, service: str, account: str) -> str | None:
        """Return the stored secret, or ``None`` when the entry does not exist."""

    def set(self, service: str, account: str, secret: str) -> None:
        """Create or replace the entry."""

    def delete(self, service: str, account: str) -> bool:
        """Remove the entry; ``False`` means it was not there."""

    def find(self, service: str) -> list[str]:
        """Return the accounts stored under ``service`` -- never a secret."""


class WindowsCredentialBackend:
    """Windows Credential Manager through ``advapi32``.

    The entry's target name is ``<service>/<account>`` and its user name is
    ``<account>``, so ``cmdkey /list`` shows exactly what the reference says.
    """

    name: str = "windows-credential-manager"

    def get(self, service: str, account: str) -> str | None:
        from financecoffer.core import wincred

        return wincred.read_password(f"{service}/{account}")

    def set(self, service: str, account: str, secret: str) -> None:
        from financecoffer.core import wincred

        wincred.write_password(f"{service}/{account}", account, secret)

    def delete(self, service: str, account: str) -> bool:
        from financecoffer.core import wincred

        return wincred.delete_password(f"{service}/{account}")

    def find(self, service: str) -> list[str]:
        from financecoffer.core import wincred

        prefix = f"{service}/"
        return [
            item.target.removeprefix(prefix)
            for item in wincred.enumerate_credentials(prefix)
            if item.target.startswith(prefix)
        ]


class PythonKeyringBackend:
    """macOS Keychain and Linux Secret Service, through the optional ``keyring``.

    Imported lazily: Windows needs no third-party dependency at all, and a missing
    ``keyring`` on another platform becomes a clear ``E_CONFIG`` with an
    alternative instead of an ``ImportError`` traceback.
    """

    name: str = "python-keyring"

    def __init__(self) -> None:
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - platform specific
            raise ConfigError(
                "a keyring: reference needs the keyring package on this platform",
                details={
                    "hint": "install financecoffer[keyring], or use a file: or env: reference"
                },
            ) from exc
        self._keyring = keyring

    def get(self, service: str, account: str) -> str | None:
        return self._keyring.get_password(service, account)  # pragma: no cover

    def set(self, service: str, account: str, secret: str) -> None:
        self._keyring.set_password(service, account, secret)  # pragma: no cover

    def delete(self, service: str, account: str) -> bool:
        try:  # pragma: no cover - platform specific
            self._keyring.delete_password(service, account)
        except self._keyring.errors.PasswordDeleteError:
            return False
        return True

    def find(self, service: str) -> list[str]:
        """Not supported here; ``config secret list`` reads the configuration instead."""
        return []


_backend_override: KeyringBackend | None = None


def set_keyring_backend(backend: KeyringBackend | None) -> None:
    """Install a process-wide backend, for tests and embedding hosts."""

    global _backend_override
    _backend_override = backend


def default_keyring_backend() -> KeyringBackend:
    """Return the credential store for this platform."""

    if _backend_override is not None:
        return _backend_override
    if os.name == "nt":
        return WindowsCredentialBackend()
    return PythonKeyringBackend()  # pragma: no cover - platform specific


@dataclass(frozen=True, slots=True)
class SecretReference:
    """A parsed ``<scheme>:<target>`` credential reference."""

    scheme: str
    target: str

    @property
    def text(self) -> str:
        return f"{self.scheme}:{self.target}"

    @property
    def service(self) -> str:
        """The keyring service, or ``""`` for another scheme."""

        service, _, _ = self.target.partition("/")
        return service if self.scheme == "keyring" else ""

    @property
    def account(self) -> str:
        """The keyring account, or ``""`` for another scheme."""

        _, _, account = self.target.partition("/")
        return account if self.scheme == "keyring" else ""

    def describe(self) -> str:
        """Name what the reference points at -- safe for a log or an error."""

        return self.target


def _invalid(field: str, text: str, reason: str) -> NoReturn:
    name = field if field.startswith("FC_") else f"FC_{field.upper()}"
    raise ConfigError(
        f"{name} is not a valid credential reference: {reason}",
        details={"field": name, "reference": text[:200], "hint": _SYNTAX_HINT},
    )


def parse_reference(value: str, *, field: str = "") -> SecretReference | None:
    """Parse a reference, or return ``None`` when ``value`` is a literal.

    A known scheme whose target is malformed is an error, not a literal: writing
    ``keyring:financecoffer`` is a typo, and quietly keeping it as the secret
    itself would deploy a credential nobody can rotate.
    """

    text = value.strip()
    scheme, separator, target = text.partition(":")
    if not separator or scheme not in REFERENCE_SCHEMES:
        return None
    target = target.strip()
    if not target:
        _invalid(field, text, f"'{scheme}' needs a target")
    if scheme == "keyring":
        service, _, account = target.partition("/")
        if not service or not account:
            _invalid(field, text, "the syntax is keyring:<service>/<account>")
        if "/" in account:
            _invalid(field, text, "'<service>/<account>' has too many path separators")
        return SecretReference(scheme="keyring", target=target)
    if scheme == "file":
        return SecretReference(scheme="file", target=target)
    if not target.replace("_", "").isalnum():
        _invalid(field, text, "'env:' names one environment variable")
    return SecretReference(scheme="env", target=target)


def _fail(field: str, reference: SecretReference | None, missing: str, hint: str) -> NoReturn:
    name = field if field.startswith("FC_") else f"FC_{field.upper()}"
    raise ConfigError(
        f"{name} is a {reference.scheme if reference else 'invalid'} reference that cannot be "
        f"resolved: {missing}",
        details={
            "field": name,
            "reference": reference.scheme if reference else "invalid",
            "target": reference.describe() if reference else field,
            "missing": missing,
            "hint": hint,
        },
    )


def _read_file(field: str, reference: SecretReference) -> str:
    path = Path(reference.target).expanduser()
    if not path.is_file():
        _fail(
            field,
            reference,
            f"the file does not exist: {path}",
            "create the file, or point the reference at an existing path",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _fail(
            field,
            reference,
            f"the file cannot be read: {exc.strerror or type(exc).__name__}",
            "check the file's permissions",
        )
    if raw.startswith(DPAPI_MARKER.encode("ascii")):
        if os.name != "nt":  # pragma: no cover - platform specific
            _fail(
                field,
                reference,
                "the file is DPAPI-protected and DPAPI is a Windows facility",
                "rewrite it unprotected, or run on Windows",
            )
        from financecoffer.core import wincred

        try:
            raw = wincred.unprotect(raw)
        except (OSError, ValueError) as exc:
            _fail(
                field,
                reference,
                f"DPAPI decryption failed ({getattr(exc, 'winerror', None) or exc})",
                "a DPAPI blob only decrypts for the user and machine that wrote it",
            )
    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
    if not text.strip():
        _fail(field, reference, f"the file is empty: {path}", "write the secret into the file")
    return text


def _read_keyring(field: str, reference: SecretReference, backend: KeyringBackend | None) -> str:
    store = backend if backend is not None else default_keyring_backend()
    try:
        secret = store.get(reference.service, reference.account)
    except ConfigError:
        raise
    except OSError as exc:
        _fail(
            field,
            reference,
            f"the credential store is not reachable ({getattr(exc, 'winerror', None) or exc})",
            "check that a credential store is available for this session",
        )
    if secret is None:
        _fail(
            field,
            reference,
            f"no {store.name} entry for {reference.target}",
            f"run: fcoffer config secret set {field}",
        )
    if not secret.strip():
        _fail(
            field,
            reference,
            f"the {store.name} entry is empty",
            f"run: fcoffer config secret set {field}",
        )
    return secret


def _read_env(field: str, reference: SecretReference, environ: Mapping[str, str]) -> str:
    value = environ.get(reference.target)
    if value is None or not value.strip():
        _fail(
            field,
            reference,
            f"the environment variable {reference.target} is not set",
            "set the variable the env: reference names",
        )
    return value


def resolve_reference(
    field: str,
    value: str,
    *,
    environ: Mapping[str, str] | None = None,
    backend: KeyringBackend | None = None,
) -> str:
    """Return the plaintext ``value`` refers to, or ``value`` when it is a literal.

    Hand this raw configuration strings, never an already-resolved secret.
    """

    reference = parse_reference(value)
    if reference is None:
        return value.strip()
    source: Mapping[str, str] = environ if environ is not None else os.environ
    resolvers: dict[str, Callable[[], str]] = {
        "file": lambda: _read_file(field, reference),
        "keyring": lambda: _read_keyring(field, reference, backend),
        "env": lambda: _read_env(field, reference, source),
    }
    resolved = resolvers[reference.scheme]()
    if len(resolved) > _MAX_RESOLVED_LENGTH:
        _fail(
            field,
            reference,
            "the resolved value is implausibly long",
            "check what the reference points at",
        )
    return resolved


def resolve_credentials(
    values: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
    backend: KeyringBackend | None = None,
) -> dict[str, str]:
    """Resolve the credential fields of ``values``; other fields pass through."""

    resolved = dict(values)
    for field in CREDENTIAL_FIELDS:
        if field in resolved:
            resolved[field] = resolve_reference(
                field, resolved[field], environ=environ, backend=backend
            )
    return resolved


__all__ = [
    "CREDENTIAL_FIELDS",
    "DEFAULT_KEYRING_SERVICE",
    "KeyringBackend",
    "PythonKeyringBackend",
    "REFERENCE_SCHEMES",
    "SecretReference",
    "WindowsCredentialBackend",
    "default_keyring_backend",
    "parse_reference",
    "resolve_credentials",
    "resolve_reference",
    "set_keyring_backend",
]
