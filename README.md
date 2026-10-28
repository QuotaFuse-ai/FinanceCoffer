<h1 align="center">FinanceCoffer</h1>

<p align="center">
  <b>金融数据金库</b><br>
  面向 AI Agent 的金融数仓与混合检索引擎
</p>

<p align="center">
  只存不算 · 不重复校验 · 不覆盖历史 · 安全检索
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License: Apache 2.0">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-336791?logo=postgresql&logoColor=white" alt="PostgreSQL 16 + pgvector">
  <br>
  <img src="https://img.shields.io/badge/storage-ODS%20%2F%20DWD%20%2F%20DWS%20%2F%20Vector-2ea44f.svg" alt="Storage layers">
  <img src="https://img.shields.io/badge/embedding-bge--m3%201024d-8a2be2.svg" alt="bge-m3 1024 dims">
  <img src="https://img.shields.io/badge/MCP-3%20tools-orange.svg" alt="3 MCP tools">
  <img src="https://img.shields.io/badge/status-alpha-yellow.svg" alt="Alpha">
</p>

---

<p align="center">
  <a href="#定位与边界">定位</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#总体数据流">数据流</a> ·
  <a href="#数仓分层">数仓分层</a> ·
  <a href="#对外接口">接口</a> ·
  <a href="#与上游-tickhub-的衔接">上游衔接</a> ·
  <a href="#文档索引">文档</a>
</p>

> FinanceCoffer（金融数据金库）是面向 AI Agent 的大规模金融数仓与混合检索引擎。它消费 TickHub-CN / TickHub-US 采集并校验后的标准事件，完成 ODS / DWD / DWS / 向量四层落库，并通过 MCP、REST 与 CLI 向 AI 提供「可回溯、带出处、带质量标记」的结构化与语义检索能力。
>
> **它只做存储与检索**：不抓取行情、不重复做财务勾稽、不覆盖历史版本。质量字段 `quality_status` / `confidence` / 勾稽结果与合规字段原样透传，DWD 永远保留历史快照，向量检索必须先按 `symbol` 过滤。

- **四层落库**：ODS 只追加、DWD 版本快照、DWS 按股票预计算、向量层切片 + HNSW。
- **三个 MCP 工具、五条 REST 路由、一棵 `fcoffer` 命令树**，共用同一个 `{ok, data, meta, error}` 返回信封。
- **本地 embedding**：bge-m3，1024 维，OpenAI 兼容接口，不依赖外部 API、不需要 API Key。
- **安全检索**：无 `symbol` 直接 `E_INVALID_ARGUMENT`，`top_k` 上限 50，先过滤再走向量索引。
- **冷热分离**：3 年以上或 >8KB 的公告正文转存对象存储，主表只留指针。

> **状态**：v0.1（Alpha）。已可用 —— 数据库迁移（ODS / DWD / DWS / 向量 / Ops）、ODS 落库与重放、DWD 版本快照写入、DWS 宽表构建、查询服务、REST API、MCP 三个工具、向量表结构与本地 embedding、公告归档列与对象存储写入。正在补齐 —— ETL 消费者、向量写入 CLI、部署编排。命令实现状态以 [`docs/specs/cli.md`](docs/specs/cli.md) 为准。

## 1. 定位与边界

| 角色 | 职责 | 不做什么 |
| --- | --- | --- |
| TickHub-CN / TickHub-US | 抓取、清洗、勾稽校验、发布标准事件 | 不连业务数仓，不承担持久化重压 |
| FinanceCoffer（本仓库） | 消费事件、落库、维护数仓分层、构建向量索引、提供查询 API / MCP | 不抓取行情、不重复做财务勾稽、不覆盖历史版本 |

四条硬约束：

1. **只存不算**：只负责存储与检索，不做数据抓取。
2. **不重复校验**：完全继承上游的 `quality_status` / `confidence` / `balance_check_result`，保证数据同源。
3. **不覆盖历史**：DWD 强制版本快照，`MAX(version)` 为最新。
4. **安全检索**：向量检索必须带 `symbol` 等前置过滤，禁止无过滤全表扫描。

## 2. 快速开始

要求 Python 3.11+（3.12 已验证），以及一个装了 `pgvector` 扩展的 PostgreSQL 16。

```bash
pip install -e ".[all]"          # 或按需：.[db] .[api] .[vector] .[mcp] .[etl] .[minio]

cp .env.example .env             # 填入本地连接串，.env 已 gitignore
fcoffer --version                # fcoffer 0.1.0
fcoffer db migrate               # 建 schema、扩展、基础表与分区
fcoffer db status                # 已应用 / 待应用迁移
fcoffer serve api --port 8100    # REST API：/health /metrics /v1/*
python -m pytest                 # 单元 + 集成测试
```

配置按 **内置默认值 → `.env` → 进程环境变量 → 显式参数** 的优先级加载，只读取 `FC_*` 变量，模板见 [`.env.example`](.env.example)。`.env` 按**当前工作目录**查找，因此请在仓库根目录执行 `fcoffer`；文件不存在时静默回退到进程环境变量（CI、容器和全新克隆即是这种情况）。密钥字段使用 `SecretStr`，日志与 `fcoffer config show` 会脱敏连接串口令。

语义检索需要一个 OpenAI 兼容的 embedding 服务。仓库自带本地 bge-m3 启动脚本（`scripts/embedding_server.py` + `scripts/start-embedding.cmd`），默认监听 `127.0.0.1:8001`，通过 `FC_EMBEDDING_URL` 指过去即可。模型维度由 `FC_EMBEDDING_DIM` 固定为 1024（见 [DEC-0005](docs/decisions/DEC-0005-embedding-bge-m3-1024.md)）。

## 3. 总体数据流

```text
[TickHub-CN / TickHub-US]
  抓取 & 清洗 & 勾稽校验
        │
        ▼ Redis Stream fc:stockdata:events（schema_version 1.0）
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
| DWD | `dwd.stock_announcement` | 大文本 / PDF 存对象存储，本表只存 `minio_url` 与元数据 |
| DWD | `dwd.stock_kline`、`dwd.stock_risk_event`、`dwd.stock_quote_snapshot`、`dwd.stock_research_report`、`dwd.northbound_flow` | 支撑 K 线、风险回溯、PE 预计算与研报 / 北向检索 |
| DWS | `dws.stock_agent_analysis_wide` | 近 5 年营收 / 净利 / ROE / PE、风险摘要、最新公告集合 |
| Vector | `vector.announcement_chunk` | 512 token 切片、50 token 重叠、1024 维（本地 bge-m3）、HNSW cosine |
| Ops | `ops.*` | 消费位点、幂等日志、DLQ、上游心跳、归档任务、迁移版本 |

> 冷热分离：近 3 年热数据留在 PG；3 年以上及 >8KB 文本 / PDF 转存对象存储，DWD 置 `is_cold=true` 并清空大字段。

## 5. 对外接口

### MCP 工具

| 工具 | 入参 | 说明 |
| --- | --- | --- |
| `query_structured_data` | `symbol, metric, year_range` | 查 DWS 结构化财报宽表 |
| `search_announcements_semantically` | `symbol, query_text, year_range` | 先按 symbol / 日期过滤，再算向量距离 |
| `get_risk_history` | `symbol` | 返回历史风险事件 |

传输：`fcoffer mcp stdio` / `fcoffer mcp http`。

### REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 进程与依赖健康检查 |
| `GET` | `/metrics` | Prometheus 文本指标 |
| `GET` | `/v1/structured/{symbol}` | 结构化财报宽表查询 |
| `GET` | `/v1/risk/{symbol}` | 风险事件历史 |
| `POST` | `/v1/search/announcements` | 带 `symbol` 前置过滤的语义检索 |

`/v1/*` 默认限流 120 次/分钟。

### CLI

```bash
fcoffer db migrate
fcoffer ods stats
fcoffer dws build --symbol 600000 --years 5
fcoffer serve api --host 127.0.0.1 --port 8100
fcoffer mcp stdio
fcoffer query structured --symbol 600000 --metric roe --year-range 2021-2025
fcoffer search announcements --symbol 600000 --query "商誉减值" --year-range 2023-2025
fcoffer query risk --symbol 600000
```

完整命令树见 [`docs/specs/cli.md`](docs/specs/cli.md)。

## 6. 命名

项目统一使用 FinanceCoffer 命名体系：

| 类别 | 值 |
| --- | --- |
| 产品名 | FinanceCoffer（金融数据金库） |
| PyPI | `financecoffer` |
| Python 包 | `financecoffer` |
| CLI | `fcoffer` |
| MCP Server | `financecoffer` |
| 环境变量 | `FC_*` |
| 数据库 | `financecoffer` |
| 容器镜像 | `ghcr.io/quotafuse-ai/financecoffer` |

完整清单与理由见 [`docs/specs/naming.md`](docs/specs/naming.md)。

## 7. 目录结构

```text
FinanceCoffer/
├── README.md
├── docs/
│   ├── specs/            # 产品规格、命名、CLI、包名、事件契约、参考对比、需求追踪
│   └── decisions/        # 决策记录（只增不改）
├── pyproject.toml        # 发行名 financecoffer，入口 fcoffer
├── .env.example          # FC_* 配置模板（无真实凭据）
├── src/financecoffer/
│   ├── api/              # FastAPI 路由与限流
│   ├── cli/              # Typer 命令树
│   ├── config/           # 配置加载（默认值 → .env → 环境变量 → 显式参数）
│   ├── core/             # 错误码、日志、返回信封
│   ├── db/               # 迁移、ORM 模型、仓储、分区
│   ├── dwd/              # DWD 版本快照写入
│   ├── dws/              # DWS 宽表构建
│   ├── events/           # 事件契约校验
│   ├── mcp/              # MCP Server 与三个工具
│   ├── ods/              # ODS 落库、重放、分区维护
│   └── query/            # 结构化 / 风险 / 语义检索服务
├── tests/
│   ├── unit/             # 单元测试
│   ├── integration/      # 真实 PostgreSQL 集成测试
│   └── security/         # 检索边界与合规用例
├── scripts/              # 本地 embedding 服务与启动脚本
├── skills/financecoffer/ # Agent Skill（SKILL.md）
├── config/               # 运行时配置目录
└── deploy/               # 容器编排与部署清单（预留）
```

## 8. 与上游 TickHub 的衔接

两端都通过 **Event Contract v1** 向同一个 Redis Stream 发布标准事件，FinanceCoffer 只消费、不回查上游库。

| 项 | TickHub-CN | TickHub-US |
| --- | --- | --- |
| 发布方式 | 采集进程内嵌钩子，只对新入库的行发布 | 旁路采集器，把 MCP 工具结果转成事件 |
| 开关 | `EVENT_PUBLISH_ENABLE`，默认关闭 | `EVENT_PUBLISH_ENABLE`，默认关闭 |
| 目标 Stream | `fc:stockdata:events` | `fc:stockdata:events` |
| 消费组 | `fc-etl` | `fc-etl` |
| 合规标记 | `compliance_tier=C`、`commercial_use=false` | `compliance_tier` 为 S / B / C / D，`commercial_use=false` |

共同约定：

- **`schema_version` 固定 `"1.0"`**，不兼容变更才升主版本；消费端不认识版本直接进死信。
- **确定性 `event_id`**：同一业务事实重发（outbox 重试、补跑、双实例）生成同一个 ID，由消费端幂等去重。
- **发布是旁路**：Redis 不可用时写本地 SQLite outbox，按 1 / 5 / 25 / 120 / 600 秒退避有界重试，永不阻塞采集。
- **有界保留**：`XADD MAXLEN ~ 100000`，Redis 需开启 AOF，否则重启丢事件。
- **`heartbeat`** 只更新 `ops.source_heartbeat`，不落 ODS，不参与 DWD / DWS。

事件字段、8 种 `data_type` 与失败处理见 [`docs/specs/event-contract.md`](docs/specs/event-contract.md)，两端改造细节见 [`docs/specs/tickhub-integration.md`](docs/specs/tickhub-integration.md)。

## 9. 文档索引

- [产品规格](docs/specs/financecoffer.md)
- [命名规范](docs/specs/naming.md)
- [CLI 命令清单](docs/specs/cli.md)
- [包名与模块清单](docs/specs/packages.md)
- [数据库结构](docs/specs/db-schema.md)
- [事件契约](docs/specs/event-contract.md)
- [TickHub 侧改造方案](docs/specs/tickhub-integration.md)
- [参考仓库 README 对比](docs/specs/reference-readme-comparison.md)
- [需求逐条追踪](docs/specs/requirements-traceability.md)
- [决策记录](docs/decisions/)

## 10. 许可与免责

代码采用 Apache-2.0。FinanceCoffer 只分发代码与存储结构，不分发行情数据；数据合规义务由上游数据源与使用方承担。
