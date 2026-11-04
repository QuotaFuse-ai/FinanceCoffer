"""FinanceCoffer.

Financial data warehouse and hybrid retrieval for AI agents.

The package is intentionally split into single-responsibility modules; see
``docs/specs/packages.md`` for the full module map. T-001 only ships the
skeleton: configuration, errors, logging and the shared response envelope.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

# 版本唯一真源是 pyproject.toml 的 ``[project] version``。``setuptools`` 通过
# ``dynamic = ["version"]`` 在构建时从本模块读 ``__version__`` 写进 wheel 元数据，
# 这里改成读已安装的元数据，运行期不会再出现第二份手写版本（QUOT-404）。
# 纯源码树未安装时回退 ``0.0.0+source``。
try:
    __version__ = _dist_version("financecoffer")
except PackageNotFoundError:  # pragma: no cover - 未安装的源码树
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
