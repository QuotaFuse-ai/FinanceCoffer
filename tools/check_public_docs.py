#!/usr/bin/env python3
r"""公开文档敏感内容检查.

本仓库为公开仓库, ``docs/`` 、 ``deploy/`` 与根目录 ``README.md`` 中不允许出现
内部任务编号、团队称谓与本机绝对路径（判定口径见
``docs/decisions/DEC-0025-public-docs-carry-no-internal-ids-or-team-names.md``）。
本脚本把那条口径变成机器检查：命中即非零退出, 并逐行打印
``文件:行: 命中原因``, 供提交前钩子与人工复检使用。

用法::

    python tools/check_public_docs.py             # 检查整个公开面
    python tools/check_public_docs.py docs/specs  # 只检查指定路径（相对仓库根）

豁免: ``docs/decisions/`` 是只增不改的历史记录, 整体豁免（按路径前缀实现,
不靠注释或标点绕过）。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 纳入检查的公开面。
TARGETS: tuple[str, ...] = ("docs", "deploy", "README.md")

#: 整体豁免的路径前缀（相对仓库根, POSIX 分隔符）。
EXEMPT_PREFIXES: tuple[str, ...] = ("docs/decisions/",)

#: 团队称谓词表 —— 按 DEC-0025 第 2 条, 指代「谁来做、谁验收」的组织称谓不允许
#: 出现在公开文档; 行业通用职能词（工具类别、使用人身份、表名释义）不在本表内。
#: 发现新的称谓直接往这里追加。
TEAM_TERMS: tuple[str, ...] = (
    "测试团队",
    "安全团队",
    "开发团队",
    "上游团队",
    "运维团队",
    "产品团队",
    "数据团队",
    "算法团队",
    "前端团队",
    "后端团队",
    "项目领导人",
    "架构师",
    "设计师",
    "产品经理",
)

#: 外部协作方的「XX 团队」写法（TickHub 团队 / LadderStock 团队 …）用一条通用规则
#: 兜住, 不必逐个进词表; 它不会误伤正文里的「团队称谓」这类元表述。
EXTERNAL_TEAM_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._-]*\s*团队")

#: 客户名 / 真实机构名: 目前没有自动化判据（哪些名字算客户名只有人知道）,
#: 先留占位说明, 不做启发式猜测, 以免把产品名、技术名误判成违规。
#: 人工确认过的名单确定后写进这个元组即可, 无需改动扫描逻辑。
CLIENT_NAMES: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    patterns: tuple[re.Pattern[str], ...]

    def first_match(self, line: str) -> str | None:
        for pattern in self.patterns:
            found = pattern.search(line)
            if found:
                return found.group(0)
        return None


def build_rules(
    team_terms: tuple[str, ...] = TEAM_TERMS,
    client_names: tuple[str, ...] = CLIENT_NAMES,
) -> tuple[Rule, ...]:
    """由词表构造规则; 词表追加后无需改动扫描逻辑。"""
    return (
        Rule(
            key="task-id",
            label="内部任务编号",
            patterns=(
                re.compile(r"QUOT-\d+", re.IGNORECASE),
                re.compile(r"\bT-\d{3}\b"),
            ),
        ),
        Rule(
            key="team-name",
            label="团队称谓",
            patterns=(
                re.compile("|".join(re.escape(term) for term in team_terms)),
                EXTERNAL_TEAM_PATTERN,
            ),
        ),
        Rule(
            key="local-path",
            label="本机绝对路径",
            patterns=(
                re.compile(r"[A-Za-z]:\\"),
                re.compile(r"[A-Za-z]:/[Uu]sers/"),
                re.compile(r"/[Dd]/workplace"),
                re.compile(r"ruanjian"),
            ),
        ),
        Rule(
            key="client-name",
            label="客户名/真实机构名",
            patterns=tuple(
                re.compile(re.escape(name), re.IGNORECASE) for name in client_names
            ),
        ),
    )


RULES: tuple[Rule, ...] = build_rules()


RULE_LABELS: dict[str, str] = {rule.key: rule.label for rule in RULES}
RULE_LABELS["unreadable"] = "无法按 UTF-8 解码的文本文件, 请转为 UTF-8 或移出公开面"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    text: str

    @property
    def reason(self) -> str:
        label = RULE_LABELS.get(self.rule, self.rule)
        if not self.text:
            return label
        return f"{label}, 命中 {self.text!r}"

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def is_exempt(rel_path: str) -> bool:
    return rel_path.startswith(EXEMPT_PREFIXES)


def rel_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def iter_target_files(root: Path, targets: tuple[str, ...] = ()) -> Iterator[Path]:
    """按 TARGETS（或显式传入的 target）产出待检查文件, 顺序稳定、不重复。"""
    names = targets or TARGETS
    seen: set[Path] = set()
    for name in sorted(names):
        base = root / name
        if base.is_file():
            candidates: list[Path] = [base]
        elif base.is_dir():
            candidates = sorted(p for p in base.rglob("*") if p.is_file())
        else:
            continue
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield path


def scan_text(rel_path: str, text: str, rules: tuple[Rule, ...] = RULES) -> list[Finding]:
    if is_exempt(rel_path):
        return []
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule in rules:
            hit = rule.first_match(line)
            if hit is not None:
                findings.append(Finding(path=rel_path, line=lineno, rule=rule.key, text=hit))
    return findings


def scan_file(root: Path, path: Path, rules: tuple[Rule, ...] = RULES) -> list[Finding]:
    rel_path = rel_posix(root, path)
    if is_exempt(rel_path):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [
            Finding(
                path=rel_path,
                line=0,
                rule="unreadable",
                text="",
            )
        ]
    return scan_text(rel_path, text, rules)


def scan(
    root: Path,
    targets: tuple[str, ...] = (),
    rules: tuple[Rule, ...] = RULES,
) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_target_files(root, targets):
        findings.extend(scan_file(root, path, rules))
    findings.sort(key=lambda f: (f.path, f.line))
    return findings


def main(argv: list[str]) -> int:
    targets = tuple(arg.replace("\\", "/").strip("/") for arg in argv)
    findings = scan(ROOT, targets)
    for finding in findings:
        print(finding.render())
    if not findings:
        scope = ", ".join(targets) if targets else ", ".join(TARGETS)
        print(f"公开文档检查通过: {scope}")
        return 0
    print(
        f"公开文档检查未通过: {len(findings)} 处命中。"
        "公开面（docs/、deploy/、根 README.md）不出现内部任务编号、团队称谓与本机绝对路径; "
        "docs/decisions/ 整体豁免。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
