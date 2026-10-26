"""Configuration layer.

Loading precedence (lowest to highest): built-in defaults -> ``.env`` file ->
process environment (``FC_*``) -> explicit overrides passed to ``load_config``.
"""

from financecoffer.config.loader import ENV_PREFIX, env_var_name, load_config
from financecoffer.core.config import SECRET_FIELDS, Config

__all__ = ["ENV_PREFIX", "SECRET_FIELDS", "Config", "env_var_name", "load_config"]
