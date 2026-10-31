"""Configuration layer.

Loading precedence (lowest to highest): built-in defaults -> ``.env`` file ->
process environment (``FC_*``) -> explicit overrides passed to ``load_config``.
"""

from financecoffer.config.loader import (
    ENV_PREFIX,
    LoadedConfig,
    default_env_file,
    env_var_name,
    load_cli_config,
    load_cli_config_detailed,
    load_config,
    load_config_detailed,
)
from financecoffer.core.config import SECRET_FIELDS, Config

__all__ = [
    "ENV_PREFIX",
    "SECRET_FIELDS",
    "Config",
    "LoadedConfig",
    "default_env_file",
    "env_var_name",
    "load_cli_config",
    "load_cli_config_detailed",
    "load_config",
    "load_config_detailed",
]
