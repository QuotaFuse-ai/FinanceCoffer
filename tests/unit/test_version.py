"""版本单一真源：运行期 ``__version__`` 必须回读自安装元数据。"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version as dist_version
from pathlib import Path

import financecoffer

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_version_matches_installed_metadata() -> None:
    assert financecoffer.__version__ == dist_version("financecoffer")


def test_installed_metadata_matches_pyproject() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert financecoffer.__version__ == declared["project"]["version"]


def test_version_is_not_hand_written_in_source() -> None:
    source = (ROOT / "src" / "financecoffer" / "__init__.py").read_text(encoding="utf-8")
    literals = re.findall(r"__version__\s*=\s*\d", source)
    assert literals == []
