#!/usr/bin/env python3
"""空库重建演练：证明备份真的能把一个空库恢复成同一份数据库。

一次运行做完这条链路：

    建空库 A -> fcoffer db migrate -> 写入样例数据 -> scripts/backup.py
    -> 建空库 B -> scripts/restore.py -> 逐项比对 -> 负向用例 -> 清理

比对项覆盖迁移账本、表/索引/约束/序列数量、每个表的行数与内容校验和、
序列当前位置。最后一条是故意改一行数据，比对必须报出差异——只报“一致”
的比对脚本等于没比对。

临时库只使用 ``financecoffer_qa_drill_`` 前缀的空库，跑完即删；脚本拒绝
任何不以该前缀开头的库名，也拒绝操作 ``financecoffer`` 本身。产物与日志
留在本机忽略目录（``var/``、``qa_evidence/``），不进版本库。

    python scripts/backup_drill.py
    python scripts/backup_drill.py --keep      # 保留临时库与备份产物便于排查
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pg_backup_lib as lib  # noqa: E402

ROOT = lib.ROOT
QA_PREFIX = "financecoffer_qa_drill"
PROTECTED_DATABASES = {"financecoffer", "postgres", "template0", "template1"}

# 样例数据覆盖每一层：ops 运行表、ODS 分区表、DWD 版本化表（含追加版本）、
# DWS 宽表、向量表（1024 维，外键指向公告）。DWD 有 append-only 触发器，
# 所以“修正”用新增版本表达，绝不 UPDATE 业务列。
SEED_SQL: tuple[str, ...] = (
    """
    INSERT INTO ops.source_heartbeat (source, last_seen, status, items, run_id) VALUES
        ('sse',  now(),                      'ok',      42, 'drill-run-1'),
        ('szse', now() - interval '5 min',   'partial',  7, 'drill-run-2')
    """,
    """
    INSERT INTO ops.etl_event_log
        (event_id, provider, data_type, symbol, biz_key, event_time, status, attempts, trace_id)
    VALUES
        ('drill-evt-0001', 'sse', 'kline', '600519', '2026-10-28:none',
         '2026-10-28 09:30:00+08', 'done', 1, 'drill-trace-1'),
        ('drill-evt-0002', 'sse', 'announcement', '600519', 'ann-2026-10-27',
         '2026-10-27 18:00:00+08', 'done', 1, 'drill-trace-2')
    """,
    """
    INSERT INTO ops.etl_consumer_offset
        (consumer_group, stream, shard, last_event_id, last_event_time)
    VALUES ('fcoffer-etl', 'fc:events', '0', 'drill-evt-0002', now())
    """,
    """
    INSERT INTO ops.etl_dlq (event_id, stream, entry_id, reason, raw_event, retry_count, status)
    VALUES ('drill-evt-bad', 'fc:events', '1-0', 'invalid_event_id',
            '{"event_id": "bad"}'::jsonb, 2, 'retried')
    """,
    """
    INSERT INTO ops.archive_job
        (table_name, symbol, object_key, status, rows_archived, bytes_archived)
    VALUES ('dwd.stock_kline', '600519', 'cold/dwd/stock_kline/2026.parquet',
            'done', 120, 4096)
    """,
    """
    INSERT INTO ods.stock_raw_data
        (event_id, data_type, symbol, market, source, biz_key, fetch_time,
         request_params, raw_json, raw_text, etl_status)
    VALUES
        ('drill-evt-0001', 'kline', '600519', 'SH', 'sse', '2026-10-28:none', now(),
         '{"symbol": "600519"}'::jsonb,
         '{"close": 1523.5, "open": 1500.0}'::jsonb,
         '{"close": 1523.5, "open": 1500.0}', 'done'),
        ('drill-evt-0002', 'announcement', '600519', 'SH', 'sse', 'ann-2026-10-27', now(),
         NULL,
         '{"title": "2025 年度报告"}'::jsonb,
         '{"title": "2025 年度报告"}', 'done')
    """,
    """
    INSERT INTO dwd.stock_announcement
        (symbol, ann_date, ann_type, title, content_hash, clean_content,
         quality_status, fetch_time, event_id, provider)
    VALUES ('600519', CURRENT_DATE - 1, 'annual', '2025 年度报告',
            repeat('ab', 32), '2025 年度报告正文', 'normal', now(),
            'drill-evt-0002', 'sse')
    """,
    """
    INSERT INTO dwd.stock_kline
        (symbol, trade_date, adjust, version, open, high, low, close, volume, amount,
         quality_status, fetch_time, event_id, provider)
    VALUES
        ('600519', DATE '2026-10-28', 'none', 1, 1500.0, 1530.0, 1495.0, 1523.5,
         1234567, 1880000000.0, 'normal', now(), 'drill-evt-0001', 'sse'),
        ('600519', DATE '2026-10-28', 'none', 2, 1500.0, 1531.0, 1495.0, 1524.0,
         1234987, 1880100000.0, 'normal', now(), 'drill-evt-0003', 'sse')
    """,
    """
    INSERT INTO dwd.stock_financial_snapshot
        (symbol, report_date, report_type, statement, version, amount_revenue,
         amount_net_profit, quality_status, confidence, fetch_time, event_id, provider)
    VALUES ('600519', DATE '2025-12-31', 'annual', 'income', 1,
            123456789012.3456, 9876543210.1234, 'normal', 'high', now(),
            'drill-evt-fin-1', 'sse')
    """,
    """
    INSERT INTO dws.stock_agent_analysis_wide
        (symbol, as_of, metrics_json, risk_summary, fiscal_years, source_version)
    VALUES ('600519', now(), '{"pe": 31.2, "roe": 0.28}'::jsonb,
            '{"level": "low"}'::jsonb, 5, 'drill-v1')
    """,
    """
    INSERT INTO vector.announcement_chunk
        (symbol, ann_date, ann_type, chunk_index, chunk_text, token_count,
         embedding, embedding_model, dwd_ref_id)
    SELECT '600519', CURRENT_DATE - 1, 'annual', 0, '2025 年度报告正文（第 1 段）', 64,
           array_fill(0.01::real, ARRAY[1024])::vector, 'BAAI/bge-m3', id
    FROM dwd.stock_announcement
    WHERE event_id = 'drill-evt-0002'
    """,
)


class DrillFailed(RuntimeError):
    """A rehearsal step did not hold; the remaining steps cannot be trusted."""


class Drill:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = uuid.uuid4().hex[:6]
        self.db_a = f"{args.prefix}_a_{self.stamp.lower()}_{suffix}"
        self.db_b = f"{args.prefix}_b_{self.stamp.lower()}_{suffix}"
        self.backup_dir = ROOT / "var" / "backups" / f"drill-{self.stamp}"
        self.report_dir = ROOT / "var" / "restore-reports" / f"drill-{self.stamp}"
        self.manifest: dict[str, Any] = {}
        self.restored: dict[str, Any] = {}
        self.diff: list[lib.DiffRow] = []
        self.transcript: list[str] = []
        self.created: list[str] = []

    # ---------------------------------------------------------------- output
    def log(self, line: str) -> None:
        self.transcript.append(line)
        print(line, flush=True)

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "通过" if ok else "失败"
        self.log(f"[{mark}] {name}" + (f"：{detail}" if detail else ""))

    # ------------------------------------------------------------- plumbing
    def dsn_for(self, database: str) -> str:
        return lib.with_database(self.server_dsn, database)

    def maintenance(self) -> str:
        if self.args.maintenance_dsn_env:
            return lib.resolve_dsn(self.args.maintenance_dsn_env, what="maintenance")
        return self.dsn_for("postgres")

    def _ensure_qa_name(self, name: str) -> None:
        if name in PROTECTED_DATABASES or not name.startswith("financecoffer_qa_"):
            raise lib.UsageError(
                f"refusing to touch {name!r}: the drill only creates and drops "
                f"databases named financecoffer_qa_*"
            )

    def create_database(self, name: str) -> None:
        self._ensure_qa_name(name)
        conn = lib.connect(self.maintenance(), autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
        finally:
            conn.close()
        self.created.append(name)

    def drop_database(self, name: str) -> None:
        self._ensure_qa_name(name)
        conn = lib.connect(self.maintenance(), autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            conn.close()

    def child_env(self, **overrides: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(PYTHONIOENCODING="utf-8")
        env.update(overrides)
        return env

    def cli_prefix(self) -> list[str]:
        candidate = Path(sys.executable).with_name("fcoffer.exe")
        if not candidate.is_file():
            candidate = Path(sys.executable).with_name("fcoffer")
        if candidate.is_file():
            return [str(candidate)]
        return [sys.executable, "-c", "from financecoffer.cli.main import main; main()"]

    def run_script(self, argv: list[str], *, env: dict[str, str], what: str) -> str:
        result = subprocess.run(
            argv,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip()[-2000:]
            raise DrillFailed(f"{what} 退出码 {result.returncode}：\n{tail}")
        return result.stdout.strip()

    # ----------------------------------------------------------------- steps
    def step_create_a(self) -> str:
        self.create_database(self.db_a)
        return f"空库 {self.db_a}"

    def step_migrate(self) -> str:
        self.run_script(
            [*self.cli_prefix(), "db", "migrate", "--revision", "head", "--ahead", "3"],
            env=self.child_env(FC_DB_URL=self.dsn_for(self.db_a)),
            what="fcoffer db migrate",
        )
        return "迁移到 head"

    def step_seed(self) -> str:
        conn = lib.connect(self.dsn_for(self.db_a))
        try:
            with conn.cursor() as cur:
                for statement in SEED_SQL:
                    cur.execute(statement)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM ods.stock_raw_data WHERE event_id LIKE 'drill-evt-%'"
                )
                ods_rows = int(cur.fetchone()[0])
        finally:
            conn.close()
        return f"{len(SEED_SQL)} 条语句，ODS 落 {ods_rows} 行"

    def step_backup(self) -> str:
        output = self.run_script(
            [
                sys.executable,
                str(ROOT / "scripts" / "backup.py"),
                "--out",
                str(self.backup_dir),
            ],
            env=self.child_env(FC_DB_URL=self.dsn_for(self.db_a)),
            what="scripts/backup.py",
        )
        self.log(lib_indent(output))
        return str(self.backup_dir)

    def step_backup_manifest(self) -> str:
        self.manifest = lib.read_manifest(self.backup_dir / lib.MANIFEST_NAME)
        dump = self.manifest.get("dump") or {}
        dump_path = self.backup_dir / str(dump.get("file", lib.DUMP_NAME))
        if not dump_path.is_file():
            raise DrillFailed("备份目录里没有 dump 文件")
        actual = lib.sha256_file(dump_path)
        if actual != dump.get("sha256"):
            raise DrillFailed(f"dump sha256 与清单记录不一致：{actual} != {dump.get('sha256')}")
        tables = self.manifest["counts"]["tables_checked"]
        if tables < 5 or self.manifest["totals"]["rows"] <= 0:
            raise DrillFailed(
                f"清单内容为空（表 {tables} 张，行 {self.manifest['totals']['rows']}）"
            )
        if self.manifest["schema_version"] <= 0:
            raise DrillFailed("清单里没有迁移账本版本")
        return (
            f"dump {dump['bytes']} 字节，sha256 与清单一致，"
            f"{tables} 张表 / {self.manifest['totals']['rows']} 行"
        )

    def step_create_b(self) -> str:
        self.create_database(self.db_b)
        return f"空库 {self.db_b}"

    def step_restore(self) -> str:
        output = self.run_script(
            [
                sys.executable,
                str(ROOT / "scripts" / "restore.py"),
                "--backup-dir",
                str(self.backup_dir),
                "--target-dsn",
                "env:FC_DRILL_TARGET_DSN",
                "--report-dir",
                str(self.report_dir),
            ],
            env=self.child_env(FC_DRILL_TARGET_DSN=self.dsn_for(self.db_b)),
            what="scripts/restore.py",
        )
        self.log(lib_indent(output))
        return f"pg_restore 完成，报告 {self.report_dir}"

    def step_compare(self) -> str:
        restored_path = self.report_dir / "restored-manifest.json"
        self.restored = lib.read_manifest(restored_path)
        self.diff = lib.diff_manifests(self.manifest, self.restored)
        passed, total = lib.summarize(self.diff)
        self.log(lib_indent(lib.render_diff(self.diff)))
        return f"{passed}/{total} 项一致"

    # --------------------------------------------------------- subset checks
    def _subset(self, area: str, suffix: str = "", prefix: str = "") -> list[lib.DiffRow]:
        matches = [
            row
            for row in self.diff
            if row.area == area
            and row.item.endswith(suffix)
            and row.item.startswith(prefix)
        ]
        if not matches:
            raise DrillFailed(f"比对结果里没有「{area}/{prefix}{suffix}」项，比对本身可能没跑完")
        return matches

    def _all_ok(self, rows: list[lib.DiffRow], label: str) -> str:
        bad = [row for row in rows if not row.ok]
        if bad:
            detail = "；".join(f"{row.item}: {row.expected} -> {row.actual}" for row in bad[:5])
            raise DrillFailed(f"{len(bad)} 项不一致：{detail}")
        return f"{label}（{len(rows)} 项）"

    def step_count_tables(self) -> str:
        rows = self._subset("结构", "表数量")
        return self._all_ok(rows, "表数量")

    def step_count_indexes(self) -> str:
        rows = self._subset("结构", "索引数量")
        return self._all_ok(rows, "索引数量")

    def step_count_constraints(self) -> str:
        rows = self._subset("结构", "约束数量")
        return self._all_ok(rows, "约束数量")

    def step_object_sets(self) -> str:
        strays = [
            row for row in self.diff if "缺失" in row.item or "多余" in row.item
        ]
        if strays:
            detail = "；".join(row.item for row in strays[:5])
            raise DrillFailed(f"结构对象集合不一致：{detail}")
        return "表/索引/约束逐项一一对应"

    def step_row_counts(self) -> str:
        rows = self._subset("数据", "行数")
        return self._all_ok(rows, "逐表行数一致")

    def step_checksums(self) -> str:
        rows = self._subset("数据", "内容校验")
        return self._all_ok(rows, "逐表内容校验一致")

    def step_sequences(self) -> str:
        rows = self._subset("序列")
        return self._all_ok(rows, "序列位置一致")

    def step_ledger(self) -> str:
        rows = self._subset("账本")
        return self._all_ok(rows, "迁移账本一致")

    def step_negative(self) -> str:
        dsn = self.dsn_for(self.db_b)
        conn = lib.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE dws.stock_agent_analysis_wide "
                    "SET metrics_json = metrics_json || '{\"drill\": \"tampered\"}'::jsonb "
                    "WHERE symbol = '600519'"
                )
                if cur.rowcount != 1:
                    raise DrillFailed(f"负向用例没能改到行（rowcount={cur.rowcount}）")
            conn.commit()
        finally:
            conn.close()

        tampered = lib.build_manifest(dsn)
        rows = lib.diff_manifests(self.manifest, tampered)
        target = [
            row
            for row in rows
            if row.area == "数据"
            and row.item.endswith("内容校验")
            and row.item.startswith("dws.stock_agent_analysis_wide")
        ]
        if not target:
            raise DrillFailed("负向用例缺少 DWS 宽表的内容校验项")
        if all(row.ok for row in target):
            raise DrillFailed("改了一行数据，比对却仍然报一致——比对是假的")
        self.log(lib_indent(lib.render_diff(target, only_failures=True)))
        return "改 1 行后比对报出差异"

    def step_cleanup(self) -> str:
        dropped = []
        for name in reversed(self.created):
            self.drop_database(name)
            dropped.append(name)
        self.created.clear()
        return f"已 DROP {len(dropped)} 个临时库"

    # ------------------------------------------------------------------- run
    def run(self) -> int:
        lib.load_env_file()
        self.server_dsn = lib.resolve_dsn(self.args.dsn_env, what="rehearsal server")
        server = lib.describe_dsn(self.server_dsn)
        self.log(f"== 备份恢复演练 {self.stamp} ==")
        self.log(
            f"服务器 {server['host']}:{server['port']} as {server['user']}；"
            f"临时库前缀 {self.args.prefix}_"
        )
        self.log(f"备份产物 {self.backup_dir}")
        self.log("")

        steps: list[tuple[str, Callable[[], str]]] = [
            ("创建空库 A（template0，无任何对象）", self.step_create_a),
            ("在空库 A 上执行 fcoffer db migrate 到 head", self.step_migrate),
            ("写入样例数据（ops/ODS/DWD 双版本/DWS/向量）", self.step_seed),
            ("scripts/backup.py：pg_dump -Fc + 校验和清单", self.step_backup),
            ("备份清单自检（dump sha256、表数、行数、账本）", self.step_backup_manifest),
            ("创建空库 B（template0）", self.step_create_b),
            ("scripts/restore.py：恢复到空库 B 并自动比对", self.step_restore),
            ("按恢复后的库重建清单并逐项差异", self.step_compare),
            ("结构：表数量一致", self.step_count_tables),
            ("结构：索引数量一致", self.step_count_indexes),
            ("结构：约束数量一致", self.step_count_constraints),
            ("结构：表/索引/约束对象集合一一对应", self.step_object_sets),
            ("数据：逐表行数一致", self.step_row_counts),
            ("数据：逐表内容校验和一致", self.step_checksums),
            ("数据：序列位置一致", self.step_sequences),
            ("账本：迁移版本与记录一致", self.step_ledger),
            ("负向用例：改 1 行后比对必须报差异", self.step_negative),
        ]

        passed = 0
        failed = False
        for name, step in steps:
            if failed:
                self.record(name, False, "未执行（前一步失败）")
                continue
            try:
                detail = step()
            except Exception as exc:  # noqa: BLE001 - a drill reports, it does not crash
                failed = True
                detail = str(exc) or exc.__class__.__name__
                if self.args.debug:
                    detail = traceback.format_exc()
                self.record(name, False, detail.replace("\n", "\n    "))
            else:
                passed += 1
                self.record(name, True, detail)

        try:
            detail = self.step_cleanup()
        except Exception as exc:  # noqa: BLE001
            failed = True
            self.record("清理临时库", False, str(exc))
        else:
            passed += 1
            self.record("清理临时库", True, detail)

        total = len(steps) + 1
        if self.args.keep:
            self.log(f"保留：{self.db_a} / {self.db_b} / {self.backup_dir}（--keep）")
        self._write_evidence(passed, total)
        print(f"== 备份恢复演练：{passed}/{total} 项通过 ==", flush=True)
        return lib.EXIT_OK if passed == total and not failed else lib.EXIT_FAILED

    def _write_evidence(self, passed: int, total: int) -> None:
        directory = Path(self.args.evidence_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"drill-{self.stamp}.txt"
        header = [
            f"备份恢复演练 {self.stamp}",
            f"临时库：{self.db_a} / {self.db_b}",
            f"备份产物：{self.backup_dir}",
            f"恢复报告：{self.report_dir}",
            f"结果：{passed}/{total} 项通过",
            "",
        ]
        path.write_text("\n".join([*header, *self.transcript]) + "\n", encoding="utf-8")
        self.log(f"证据：{path}")


def lib_indent(text: str) -> str:
    return "\n".join(f"    {line}" if line else line for line in text.splitlines())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rehearse backup/restore on throwaway financecoffer_qa databases."
    )
    parser.add_argument(
        "--dsn-env",
        default="FC_DB_URL",
        help="environment variable holding the server DSN (default: FC_DB_URL)",
    )
    parser.add_argument(
        "--maintenance-dsn-env",
        default="",
        help="optional env var with a DSN used only to CREATE/DROP the rehearsal databases",
    )
    parser.add_argument(
        "--prefix",
        default=QA_PREFIX,
        help=f"temporary database prefix (default: {QA_PREFIX}, must start with financecoffer_qa_)",
    )
    parser.add_argument(
        "--evidence-dir",
        default=str(ROOT / "qa_evidence" / "quot181_backup_drill"),
        help="where to write the textual evidence",
    )
    parser.add_argument("--keep", action="store_true", help="keep the rehearsal databases and dump")
    parser.add_argument("--debug", action="store_true", help="print full tracebacks on failure")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.prefix.startswith("financecoffer_qa_"):
        print("ERROR: --prefix must start with financecoffer_qa_", file=sys.stderr)
        raise SystemExit(lib.EXIT_USAGE)
    lib.main_guard(lambda: Drill(args).run())


if __name__ == "__main__":
    main()
