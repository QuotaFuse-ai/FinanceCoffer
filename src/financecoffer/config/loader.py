"""Configuration-file loading.

Precedence, lowest to highest: built-in defaults -> ``.env`` file -> process
environment -> explicit keyword arguments passed to :func:`load_config`.

Only ``FC_*`` variables are read. Secrets use :class:`pydantic.SecretStr` so
they never leak through ``repr``, logs or ``model_dump(mode="json")``.

A credential field may hold a *reference* instead of the secret (see
:mod:`financecoffer.core.secrets`). References are resolved here, before
validation, so a :class:`Config` always carries the plaintext value its consumers
expect while every source -- ``.env``, the process environment, ``--db-url`` --
can keep the reference. :func:`load_config_detailed` additionally returns the
unresolved strings, which is what lets ``serve api --workers N`` hand its children
a reference rather than a secret.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError

from financecoffer.core.config import Config
from financecoffer.core.errors import ConfigError
from financecoffer.core.secrets import CREDENTIAL_FIELDS, KeyringBackend, resolve_reference

ENV_PREFIX = "FC_"

#: The repo-local file from the documented precedence chain; absent in CI,
#: containers and on a fresh clone.
DEFAULT_ENV_FILE = Path(".env")


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """A resolved :class:`Config` plus the raw strings its credentials came from."""

    config: Config
    sources: Mapping[str, str] = field(default_factory=dict[str, str])


def env_var_name(field_name: str) -> str:
    """Return the canonical environment variable for a config field."""

    return f"{ENV_PREFIX}{field_name.upper()}"


def default_env_file(path: str | Path = DEFAULT_ENV_FILE) -> Path | None:
    """Return ``path`` when it is a file, otherwise ``None``.

    Discovery is deliberately silent: a missing ``.env`` means "fall back to the
    process environment", while an explicitly passed ``env_file`` that does not
    exist is still an error (see :func:`load_config`).
    """

    candidate = Path(path)
    return candidate if candidate.is_file() else None


def _extract_known(source: Mapping[str, str | None]) -> dict[str, str]:
    known = Config.model_fields
    values: dict[str, str] = {}
    for key, raw in source.items():
        if not key.startswith(ENV_PREFIX) or raw is None or not str(raw).strip():
            continue
        field_name = key[len(ENV_PREFIX) :].lower()
        if field_name in known:
            values[field_name] = raw
    return values


def _environ(values: Mapping[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in values.items() if value is not None}


def _resolve_references(
    values: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    backend: KeyringBackend | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve the credential fields and keep what they were written as."""

    resolved = dict(values)
    sources: dict[str, str] = {}
    for name in CREDENTIAL_FIELDS:
        value = resolved.get(name)
        if not isinstance(value, str):
            continue
        sources[name] = value
        resolved[name] = resolve_reference(name, value, environ=environ, backend=backend)
    return resolved, sources


def load_config_detailed(
    *,
    env: Mapping[str, str | None] | None = None,
    env_file: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    backend: KeyringBackend | None = None,
) -> LoadedConfig:
    """Build a :class:`Config` using the precedence documented in the module docstring.

    ``env`` defaults to ``os.environ``; tests pass a mapping instead. An empty
    value means "unset", so ``FC_MINIO_SECRET_KEY=`` does not override a default
    or leave a blank secret behind. A missing ``env_file`` is an error rather
    than silently ignored, so a misconfigured deployment fails loudly.

    Credential references are resolved before validation, so an unresolvable
    reference fails the load with ``E_CONFIG`` instead of reaching a driver.
    ``env:`` references are looked up in ``env`` (the same source the field itself
    came from), which keeps a test's stubbed mapping authoritative.
    """

    values: dict[str, Any] = {}
    source_env: Mapping[str, str | None] = os.environ if env is None else env

    if env_file is not None:
        path = Path(env_file)
        if not path.is_file():
            raise ConfigError(f"env file not found: {path}")
        values.update(_extract_known(dotenv_values(path)))

    values.update(_extract_known(source_env))

    if overrides:
        unknown = set(overrides) - set(Config.model_fields)
        if unknown:
            raise ConfigError(f"unknown configuration overrides: {sorted(unknown)}")
        values.update({key: value for key, value in overrides.items() if value is not None})

    resolved, sources = _resolve_references(values, environ=_environ(source_env), backend=backend)

    try:
        config = Config(**resolved)
    except ValidationError as exc:
        # Drop pydantic's raw ``input`` values so a malformed secret never lands
        # in an error envelope or a log line.
        problems = [
            {"field": ".".join(str(part) for part in error["loc"]), "reason": error["msg"]}
            for error in exc.errors(include_url=False)
        ]
        raise ConfigError("invalid configuration", details={"errors": problems}) from exc
    return LoadedConfig(config=config, sources=sources)


def load_config(
    *,
    env: Mapping[str, str | None] | None = None,
    env_file: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    backend: KeyringBackend | None = None,
) -> Config:
    """Return the resolved :class:`Config` (see :func:`load_config_detailed`)."""

    return load_config_detailed(
        env=env, env_file=env_file, overrides=overrides, backend=backend
    ).config


def credential_sources(
    *,
    env: Mapping[str, str | None] | None = None,
    env_file: str | Path | None = None,
) -> dict[str, str]:
    """Return the raw credential strings, resolved or not, as ``config secret`` sees them.

    Reading a reference is safe to report; resolving one is not, and a broken
    reference must still be *listable* -- that is the point of ``list``. Precedence
    matches :func:`load_cli_config` (repo ``.env`` below the process environment).
    """

    values: dict[str, str] = {}
    if env_file is None:
        env_file = default_env_file()
    if env_file is not None:
        values.update(_extract_known(dotenv_values(env_file)))
    values.update(_extract_known(os.environ if env is None else env))
    return {name: value for name, value in values.items() if name in CREDENTIAL_FIELDS}


def load_cli_config(*, db_url: str | None = None, backend: KeyringBackend | None = None) -> Config:
    """Load the configuration for one CLI invocation.

    Every command shares one rule: the repo ``.env`` participates when it exists
    (README precedence: defaults -> ``.env`` -> process environment), and
    ``--db-url`` wins over both. A command that loads its configuration some
    other way silently ignores a local ``.env`` and reports "FC_DB_URL is not
    configured" on a checkout that is configured.
    """

    return load_cli_config_detailed(db_url=db_url, backend=backend).config


def load_cli_config_detailed(
    *, db_url: str | None = None, backend: KeyringBackend | None = None
) -> LoadedConfig:
    """As :func:`load_cli_config`, keeping the unresolved credential strings."""

    return load_config_detailed(
        env_file=default_env_file(),
        overrides={"db_url": db_url},
        backend=backend,
    )
