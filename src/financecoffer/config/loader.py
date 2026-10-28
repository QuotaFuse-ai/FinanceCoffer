"""Configuration-file loading.

Precedence, lowest to highest: built-in defaults -> ``.env`` file -> process
environment -> explicit keyword arguments passed to :func:`load_config`.

Only ``FC_*`` variables are read. Secrets use :class:`pydantic.SecretStr` so
they never leak through ``repr``, logs or ``model_dump(mode="json")``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError

from financecoffer.core.config import Config
from financecoffer.core.errors import ConfigError

ENV_PREFIX = "FC_"

#: The repo-local file from the documented precedence chain; absent in CI,
#: containers and on a fresh clone.
DEFAULT_ENV_FILE = Path(".env")


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


def load_config(
    *,
    env: Mapping[str, str | None] | None = None,
    env_file: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Build a :class:`Config` using the precedence documented in the module docstring.

    ``env`` defaults to ``os.environ``; tests pass a mapping instead. An empty
    value means "unset", so ``FC_MINIO_SECRET_KEY=`` does not override a default
    or leave a blank secret behind. A missing ``env_file`` is an error rather
    than silently ignored, so a misconfigured deployment fails loudly.
    """

    values: dict[str, Any] = {}

    if env_file is not None:
        path = Path(env_file)
        if not path.is_file():
            raise ConfigError(f"env file not found: {path}")
        values.update(_extract_known(dotenv_values(path)))

    values.update(_extract_known(os.environ if env is None else env))

    if overrides:
        unknown = set(overrides) - set(Config.model_fields)
        if unknown:
            raise ConfigError(f"unknown configuration overrides: {sorted(unknown)}")
        values.update({key: value for key, value in overrides.items() if value is not None})

    try:
        return Config(**values)
    except ValidationError as exc:
        # Drop pydantic's raw ``input`` values so a malformed secret never lands
        # in an error envelope or a log line.
        problems = [
            {"field": ".".join(str(part) for part in error["loc"]), "reason": error["msg"]}
            for error in exc.errors(include_url=False)
        ]
        raise ConfigError("invalid configuration", details={"errors": problems}) from exc


def load_cli_config(*, db_url: str | None = None) -> Config:
    """Load the configuration for one CLI invocation.

    Every command shares one rule: the repo ``.env`` participates when it exists
    (README precedence: defaults -> ``.env`` -> process environment), and
    ``--db-url`` wins over both. A command that loads its configuration some
    other way silently ignores a local ``.env`` and reports "FC_DB_URL is not
    configured" on a checkout that is configured.
    """

    return load_config(
        env_file=default_env_file(),
        overrides={"db_url": db_url},
    )
