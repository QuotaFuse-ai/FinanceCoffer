"""Unit tests for the public-docs hygiene checker.

tools/check_public_docs.py turns the public-repo rule (no internal task ids, no
team names, no machine-local absolute paths in docs/, deploy/ and the root
README) into a machine check. Each rule gets one hit case and one exemption
case here; the exemption is the append-only decisions directory.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "check_public_docs.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_public_docs", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_checker()

TARGET_FILE = "docs/specs/cli.md"
EXEMPT_FILE = "docs/decisions/DEC-0025-public-docs.md"

#: 每条规则一行命中样本, 一行豁免样本用同一行文本。
HIT_SAMPLES = {
    "task-id": "本条依据 QUOT-138 与 T-042 排期确定。",
    "team-name": "该验收项由测试团队与安全团队共同确认。",
    "local-path": r"解释器在 D:\workplace\python 与 /d/workplace 下各一份。",
}


@pytest.mark.parametrize("rule,text", sorted(HIT_SAMPLES.items()))
def test_rule_hit_reported_outside_exempt_dir(rule: str, text: str) -> None:
    findings = check.scan_text(TARGET_FILE, text)
    assert [f.rule for f in findings] == [rule]
    assert findings[0].line == 1
    assert findings[0].path == TARGET_FILE


@pytest.mark.parametrize("rule,text", sorted(HIT_SAMPLES.items()))
def test_rule_hit_exempted_inside_decisions(rule: str, text: str) -> None:
    assert check.scan_text(EXEMPT_FILE, text) == []


def test_client_name_rule_is_a_wired_placeholder() -> None:
    """第四类规则暂无名单: 空名单不命中, 名单补上即生效。"""
    assert check.CLIENT_NAMES == ()
    assert check.scan_text(TARGET_FILE, "交付给某某资本的版本已冻结。") == []
    rules = check.build_rules(client_names=("某某资本",))
    findings = check.scan_text(TARGET_FILE, "交付给某某资本的版本已冻结。", rules)
    assert [f.rule for f in findings] == ["client-name"]
    assert check.scan_text(EXEMPT_FILE, "交付给某某资本的版本已冻结。", rules) == []


def test_finding_renders_as_path_line_reason() -> None:
    (finding,) = check.scan_text(TARGET_FILE, "见 QUOT-138 说明。")
    assert re.fullmatch(r"docs/specs/cli\.md:1: 内部任务编号, 命中 'QUOT-138'", finding.render())


def test_task_id_patterns_are_bounded() -> None:
    """误判控制: 决策编号与三位以下的排期号写法不算任务编号。"""
    assert check.scan_text(TARGET_FILE, "依据 DEC-0025 与 QUOT-174 的口径。") != []
    assert check.scan_text(TARGET_FILE, "依据 DEC-0025 执行。") == []
    assert check.scan_text(TARGET_FILE, "方案 MT-100 与 T-042 并行。") == [
        check.Finding(TARGET_FILE, 1, "task-id", "T-042")
    ]


def test_external_team_pattern_does_not_flag_meta_wording() -> None:
    assert check.scan_text(TARGET_FILE, "公开文档不出现团队称谓。") == []
    findings = check.scan_text(TARGET_FILE, "由 TickHub 团队发布事件。")
    assert [f.rule for f in findings] == ["team-name"]


def test_scan_walks_targets_and_skips_exempt_dir(tmp_path: Path) -> None:
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "src").mkdir()
    violation = "解释器路径 C:/Users/dev 见 QUOT-138。\n"
    (tmp_path / "docs" / "specs" / "a.md").write_text(violation, encoding="utf-8")
    (tmp_path / "docs" / "decisions" / "DEC-0001.md").write_text(violation, encoding="utf-8")
    (tmp_path / "deploy" / "compose.yaml").write_text("image: postgres:16\n", encoding="utf-8")
    (tmp_path / "src" / "leak.md").write_text("QUOT-138 不在公开面内。\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# 标题\n", encoding="utf-8")

    findings = check.scan(tmp_path)
    assert [f.path for f in findings] == ["docs/specs/a.md", "docs/specs/a.md"]
    assert {f.rule for f in findings} == {"task-id", "local-path"}


def test_clean_tree_produces_no_findings(tmp_path: Path) -> None:
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "specs" / "a.md").write_text(
        "依据 DEC-0025, 正文用职能口径描述验收环节。\n", encoding="utf-8"
    )
    assert check.scan(tmp_path) == []


def test_repo_public_surface_is_clean() -> None:
    """基线: 当前检出的公开面必须过检查, 否则钩子与 CI 立刻红。"""
    findings = check.scan(ROOT)
    assert findings == [], "\n".join(f.render() for f in findings)
