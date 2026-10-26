# FinanceCoffer

FinanceCoffer（金融数据金库）是面向 AI Agent 的大规模金融数仓与混合检索引擎。它消费 TickHub-CN / TickHub-US（原 QuotaFuse StockData）采集并校验后的标准事件，完成 ODS / DWD / DWS / 向量四层落库，并通过 MCP 与 CLI 向 AI 提供「可回溯、带出处、带质量标记」的结构化与语义检索能力。

> 状态：**S1 基线（v0.1）**。T-001 工程骨架已落地：可安装的 `financecoffer` 包、`fcoffer` 命令树、配置加载、错误码、结构化日志与统一返回信封；业务命令仍为占位实现，调用返回 `E_NOT_IMPLEMENTED`，由后续任务填充。
> 需求来源：`新建 DOCX 文档.docx` 中的《QuotaFuse Vault 完整开发文档（v1.0）》，按客户要求全部更名为 FinanceCoffer。

## 1. 定位与边界

| 角色 | 职责 | 不做什么 |
| --- | --- | --- |
| TickHub-CN / TickHub-US（StockData） | 抓取、清洗、勾稽校验、发布标准事件 | 不连业务数仓，不承担持久化重压 |
| FinanceCoffer（本仓库） | 消费事件、落库、维护数仓分层、构建向量索引、提供查询 API/MCP | 不抓取行情、不重复做财务勾稽、不覆盖历史版本 |

四条硬约束：

1. **只存不算**：只负责存储与检索，不做数据抓取。
2. **不重复校验**：完全继承 StockData 的 `quality_status` / `confidence` / `balance_check_result`，保证数据 100% 同源。
3. **不覆盖历史**：DWD 强制版本快照，`MAX(version)` 为最新。
4. **安全检索**：向量检索必须带 `symbol` 等前置过滤，禁止无过滤全表扫描。

## 2. 快速开始（T-001 骨架）

要求 Python 3.11+。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

fcoffer --version        # fcoffer 0.1.0
fcoffer --help           # 命令树与 docs/specs/cli.md 的 P0 一致
python -m pytest         # 骨架单测
```

当前所有业务命令都是占位符，例如 `fcoffer doctor` / `fcoffer etl consume` 会返回 `E_NOT_IMPLEMENTED` 信封并以退出码 1 结束，由后续任务实现（见 `docs/tasks/TASK-000-plan/plan.md`）。

配置按 **内置默认值 → `.env` → 进程环境变量 → 显式参数** 的优先级加载，只读取 `FC_*` 变量，模板见 [`.env.example`](.env.example)。密钥字段使用 `SecretStr`，日志与 `fcoffer config show` 会脱敏连接串口令。

可选依赖按需安装：`pip install -e ".[db]"`、`.[api]`、`.[vector]`、`.[minio]`、`.[mcp]`、`.[etl]`、`.[admin]`，或 `.[all]`。

## 3. 总体数据流

```text
[TickHub-CN / TickHub-US]
  抓取 & 清洗 & 勾稽校验
        │
        ▼ 标准事件（Redis Stream / Kafka / Webhook）
[FinanceCoffer ETL 消费者]
        ├── ODS 原始层：raw_json + 原件，只追加、可重放
        ├── DWD 明细层：版本快照，金额 NUMERIC，透传质量结果
        ├── DWS 宽表层：按股票预计算近 5 年指标与风险摘要
        └── 向量层：clean_content 切片 → embedding → PGVector HNSW
        │
        ▼
[FinanceCoffer API / MCP]
  关系型过滤 + 向量距离的混合检索
```

## 4. 数仓分层

| 层 | Schema / 表 | 关键规则 |
| --- | --- | --- |
| ODS | `ods.stock_raw_data` | 按月分区；只追加；`raw_json` 禁止修改；`etl_status` 可重跑 |
| DWD | `dwd.stock_financial_snapshot` | 版本快照；`NUMERIC(20,4)`；继承质量三字段 |
| DWD | `dwd.stock_announcement` | 大文本/PDF 存 MinIO，本表只存 `minio_url` 与元数据 |
| DWD | `dwd.stock_kline`、`dwd.stock_risk_event`、`dwd.stock_quote_snapshot` | 文档未给 DDL，为支撑 K 线、风险回溯与 PE 预计算补齐（待确认） |
| DWS | `dws.stock_agent_analysis_wide` | 近 5 年营收/净利/ROE/PE、风险摘要、最新公告集合 |
| Vector | `vector.announcement_chunk` | 512 token 切片、50 token 重叠、1024 维（本地 bge-m3）、HNSW cosine |
| Ops | `ops.*` | 消费位点、幂等日志、DLQ、归档任务、迁移版本 |

> 冷热分离：近 3 年热数据留在 PG；3 年以上及 >8KB 文本/PDF 转存 MinIO，DWD 置 `is_cold=true` 并清空大字段。

## 5. 对外接口

### MCP 工具（严格按文档，首期 3 个）

| 工具 | 入参 | 说明 |
| --- | --- | --- |
| `query_structured_data` | `symbol, metric, year_range` | 查 DWS 结构化财报宽表 |
| `search_announcements_semantically` | `symbol, query_text, year_range` | 先按 symbol/日期过滤，再算向量距离 |
| `get_risk_history` | `symbol` | 返回历史风险事件 |

传输：`fcoffer mcp stdio` / `fcoffer mcp http`。

### CLI

```bash
fcoffer init
fcoffer doctor
fcoffer serve api --host 127.0.0.1 --port 8100
fcoffer mcp stdio
fcoffer etl consume
fcoffer query structured --symbol 600000 --metric roe --year-range 2021-2025
fcoffer search announcements --symbol 600000 --query "商誉减值" --year-range 2023-2025
fcoffer query risk --symbol 600000
```

完整命令树见 [`docs/specs/cli.md`](docs/specs/cli.md)。

## 6. 命名

从 QuotaFuse Vault 全量更名为 FinanceCoffer：

| 类别 | 旧 | 新 |
| --- | --- | --- |
| 产品名 | QuotaFuse Vault | FinanceCoffer |
| PyPI | `quota-fuse-vault` | `financecoffer` |
| Python 包 | `qf_vault` | `financecoffer` |
| CLI | `qfv` | `fcoffer` |
| MCP Server | `quota-fuse-vault` | `financecoffer` |
| 环境变量 | `VAULT_*` | `FC_*` |
| 数据库 | `qf_vault` | `financecoffer` |
| 容器镜像 | `ghcr.io/quotafuse-ai/quota-fuse-vault` | `ghcr.io/quotafuse-ai/financecoffer` |

完整映射与理由见 [`docs/specs/naming.md`](docs/specs/naming.md) 与 [`docs/decisions/DEC-0001-rename-to-financecoffer.md`](docs/decisions/DEC-0001-rename-to-financecoffer.md)。

## 7. 目录结构

```text
FinanceCoffer/
├── README.md
├── docs/
│   ├── specs/           # 产品规格、命名、CLI、包名、参考对比、需求追踪
│   ├── decisions/       # 决策记录（只增不改）
│   ├── tasks/           # 任务产出
│   └── multica-handover/# 接手报告（只读）
├── pyproject.toml        # 发行名 financecoffer，入口 fcoffer
├── .env.example          # FC_* 配置模板（无真实凭据）
├── docs/
│   ├── specs/           # 产品规格、命名、CLI、包名、参考对比、需求追踪
│   ├── decisions/       # 决策记录（只增不改）
│   ├── tasks/           # 任务产出（TASK-000 计划、TASK-001 骨架）
│   └── multica-handover/# 接手报告（只读）
├── src/financecoffer/
│   ├── cli/             # Typer 命令树（P0 占位命令）
│   ├── core/            # 配置模型、错误码、日志、返回信封
│   └── config/          # 配置加载（默认值 → .env → 环境变量 → 显式参数）
├── tests/unit/          # 骨架单测（配置 / 信封 / 错误码 / 日志 / CLI）
├── config/              # 运行时配置目录（预留）
├── scripts/             # 运维脚本（待实现）
├── skills/financecoffer/# Agent Skill（SKILL.md）
└── deploy/              # Docker Compose / 部署清单（待实现）
```

## 8. 参考仓库对比结论

| 项 | TickHub-CN | TickHub-US | FinanceCoffer |
| --- | --- | --- | --- |
| 定位 | A 股采集/MCP | 美股/港股采集/MCP | 数仓与检索（只存不算） |
| PyPI / CLI | `tickhub-cn` / `th-cn` | `tickhub-us` / `th-us` | `financecoffer` / `fcoffer` |
| MCP 工具数 | 149 个名称 | 41 个工具 | 首期 3 个（文档规定） |
| 持久化 | SQLite 归档 `var/archive/tickhub.db` | 仅 Redis/进程内缓存 | PG + PGVector + MinIO |
| 是否发布事件 | **否** | **否** | 消费事件 |
| 质量/合规 | `confidence`、勾稽结果 | `compliance_tier`、warnings | 必须原样继承 |

**最重要的发现**：文档 4.1 假设 StockData 会向 Redis Stream 抛事件，但当前 TickHub-CN 与 TickHub-US 都没有事件发布能力；CN 只有盘后 SQLite 归档，US 只有缓存。FinanceCoffer 无法在「只存不算」的边界内直接拿到数据。详见 [`docs/specs/reference-readme-comparison.md`](docs/specs/reference-readme-comparison.md)。

## 9. 文档索引

- [产品规格](docs/specs/financecoffer.md)
- [命名规范](docs/specs/naming.md)
- [CLI 命令清单](docs/specs/cli.md)
- [包名与模块清单](docs/specs/packages.md)
- [参考仓库 README 对比](docs/specs/reference-readme-comparison.md)
- [需求逐条追踪](docs/specs/requirements-traceability.md)
- [事件契约](docs/specs/event-contract.md)
- [TickHub 侧改造方案](docs/specs/tickhub-integration.md)
- [实施计划](docs/tasks/TASK-000-plan/plan.md)
- [T-001 工程骨架交付说明](docs/tasks/TASK-001/README.md)
- [决策记录](docs/decisions/)
- [DEC-0007 返回信封与错误码](docs/decisions/DEC-0007-envelope-and-error-codes.md)

## 10. 许可与免责

代码计划采用 Apache-2.0。FinanceCoffer 只分发代码与存储结构，不分发行情数据；数据合规义务由上游数据源与使用方承担。
