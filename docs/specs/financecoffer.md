# FinanceCoffer 产品规格

版本：v0.5（设计稿）
变更规则：本文件为最新版本，每次修改必须在文末「变更记录」追加一行。

## 1. 产品定位

FinanceCoffer 是面向 AI Agent 的大规模金融数仓与混合检索引擎，常驻后台独立服务。它承接 TickHub-CN / TickHub-US 采集、清洗、勾稽校验后的全量数据，提供：

- 高可靠持久化（PostgreSQL，ODS/DWD/DWS 分层）
- 冷热分离（近 3 年热数据在 PG，历史与大文本转存 MinIO）
- 向量化与多维索引（PGVector + HNSW）
- 给 AI Agent 的查询面（MCP 工具 + REST + CLI）

部署形态：`FastAPI + PostgreSQL 14+ / PGVector + MinIO + Redis Streams`。

## 2. 职责边界（读写分离）

| 系统 | 职责 | 明确不做 |
| --- | --- | --- |
| TickHub-CN / TickHub-US（StockData） | 抓取、清洗、勾稽校验，向外抛标准化事件 | 不连业务数仓、不承担持久化重压 |
| FinanceCoffer | 消费事件、落库、维护分层、建向量索引、提供查询 | 不抓取行情、不重复财务勾稽、不覆盖历史版本 |

四条约束（最终固定）：

1. **只存不算**：只负责存储与检索，不做数据抓取。
2. **严禁重复校验**：不重复实现财务勾稽，完全依赖上游 `quality_status`，保证数据 100% 同源。
3. **严禁 Update 覆盖**：DWD 强制版本快照，历史永久保留。
4. **安全检索**：向量检索必须带前置条件（`symbol` 等），禁止无过滤全表扫描。

## 3. 总体数据流

```text
[TickHub-CN / TickHub-US] 抓取 & 校验完成
        │ 标准事件（Redis Stream / Kafka / Webhook）
        ▼
[FinanceCoffer ETL 消费者] 异步消费
        ├── ODS 原始层：raw_json + 原件，只追加、可重放
        ├── DWD 明细层：版本快照，金额 NUMERIC，透传质量结果
        ├── 文本转存 MinIO 并生成向量（PGVector）
        └── DWS 聚合宽表：按股票预计算
        │
        ▼
[FinanceCoffer API / MCP] 关系型 + 向量混合检索，响应 AI Agent
```

## 4. 数仓分层

### 4.1 ODS 原始数据层（证据可回溯）

定位：只存原始，不加工。禁止修改 `raw_json`，只追加，不 update 旧记录；发现解析错误时随时重跑 ETL。

表：`ods.stock_raw_data`

字段：`id, data_type（financial/announcement/kline）, symbol, fetch_time, source, request_params, raw_json, raw_file_path, etl_status（pending/done/failed）`

规则：

- 按 `fetch_time` 月度 RANGE 分区，提前 3 个月建分区，防止单表膨胀。
- 大文本 / PDF 存 MinIO，本表只存 `raw_file_path`。
- 重放接口只读 ODS，不碰上游。

DDL：

```sql
CREATE SCHEMA IF NOT EXISTS ods;
CREATE TABLE ods.stock_raw_data (
    id              BIGSERIAL,
    data_type       VARCHAR(50)  NOT NULL,
    symbol          VARCHAR(20)  NOT NULL,
    fetch_time      TIMESTAMPTZ  NOT NULL,
    source          VARCHAR(50)  NOT NULL,
    request_params  JSONB,
    raw_json        JSONB,
    raw_file_path   VARCHAR(500),
    etl_status      VARCHAR(20)  DEFAULT 'pending',
    PRIMARY KEY (id, fetch_time)
) PARTITION BY RANGE (fetch_time);
```

### 4.2 DWD 明细层（业务标准化）

定位：高质量结构化底座，不重复做财务勾稽（直接复用上游校验结果）。版本快照，不原地更新；金额统一 `NUMERIC`，严禁 `FLOAT`。

表：`dwd.stock_financial_snapshot`

```sql
CREATE SCHEMA IF NOT EXISTS dwd;
CREATE TABLE dwd.stock_financial_snapshot (
    symbol               VARCHAR(20)  NOT NULL,
    report_date          DATE         NOT NULL,
    report_type          VARCHAR(20)  NOT NULL,
    version              INT          NOT NULL,
    amount_revenue       NUMERIC(20,4),
    amount_net_profit    NUMERIC(20,4),
    quality_status       VARCHAR(20)  NOT NULL,   -- normal / suspicious / bad
    confidence           VARCHAR(20),
    balance_check_result JSONB,
    fetch_time           TIMESTAMPTZ  NOT NULL,
    is_cold              BOOLEAN      DEFAULT FALSE,
    minio_url            VARCHAR(500),
    PRIMARY KEY (symbol, report_date, report_type, version)
);
CREATE INDEX idx_fin_snapshot_symbol_date
    ON dwd.stock_financial_snapshot (symbol, report_date DESC);
```

版本写入逻辑：消费到新事件时取该 `(symbol, report_date, report_type)` 的 `MAX(version)`，插入 `version + 1`；查询默认取 `MAX(version)`。

表：`dwd.stock_announcement`（文档只给字段描述，DDL 为补齐项，见 G-03）

- 大文本 / PDF 存 MinIO，本表只存 `minio_url`、`content_hash`、`title`、`ann_type`、`ann_date`、`symbol`、`quality_status`、`confidence`、`fetch_time`。
- `content_hash` 唯一索引用于同一公告去重，避免重复切片。

表：`dwd.stock_kline`、`dwd.stock_risk_event`、`dwd.stock_quote_snapshot`（补齐项，见 G-04/G-05）

- K 线：支撑 `data_type=kline` 的 ODS 事件落 DWD。
- 风险事件：支撑 `get_risk_history` 与 DWS 风险摘要。
- 行情快照：PE 预计算需要价格，事件契约当前无 quote 类型（见 G-05）。

### 4.3 DWS 聚合宽表层（面向 AI 预计算）

定位：解决 AI 跨多表 JOIN 慢的问题。按股票维度预计算「近 5 年营收、净利润、ROE、PE、关键风险事件摘要、最新公告集合」。

表：`dws.stock_agent_analysis_wide`（DDL 为补齐项，见 G-03）

- 主键 `symbol`；`as_of` 标注计算时点；`metrics_json` 存过去 5 年逐年指标数组；`risk_summary`、`latest_announcements` 为预计算摘要。
- 构建方式：`fcoffer dws build --symbol ...` / 定时全量重建；只读 DWD，不回写。

### 4.4 向量检索层（PGVector）

- 数据源：直接复用上游 `clean_content`，不重复清洗。
- 切片：512 token，重叠 50 token。
- 模型：本地 `BAAI/bge-m3`（DEC-0005），维度 **1024**（`FC_EMBEDDING_DIM` 落库前必须与表定义一致）。
- 调用方式：HTTP 调用本地 embedding 服务（OpenAI 兼容 `/v1/embeddings`，可用 TEI / Ollama / Xinference 承载），FinanceCoffer 内不内嵌模型权重；返回向量做 L2 归一化后再落库。
- 索引：HNSW + cosine。
- 换模型或改维度必须新建表并全量重跑 `fcoffer vector reindex`，禁止原地 `ALTER` 混存两套向量。

```sql
CREATE SCHEMA IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE vector.announcement_chunk (
    id          BIGSERIAL PRIMARY KEY,
    symbol      VARCHAR(20)  NOT NULL,
    ann_date    DATE         NOT NULL,
    ann_type    VARCHAR(50),
    chunk_text  TEXT         NOT NULL,
    embedding   vector(1024) NOT NULL,
    dwd_ref_id  BIGINT REFERENCES dwd.stock_announcement(id)
);
CREATE INDEX idx_ann_chunk_filter ON vector.announcement_chunk (symbol, ann_date);
CREATE INDEX idx_ann_chunk_embedding
    ON vector.announcement_chunk USING hnsw (embedding vector_cosine_ops);
```

### 4.5 冷热分离与 MinIO 归档

| 数据 | 判定 | 处理 |
| --- | --- | --- |
| 热 | 近 3 年 | 全量留在 PG |
| 冷 | 3 年以上 | 大文本 / PDF 上传 MinIO；成功后 PG 大字段置 `NULL`，`is_cold = true`，写 `minio_url` |
| 大文本 | 单条 > 8KB | 无论年份，一律转存 MinIO |

归档任务幂等：先上传再置空，失败保持原状并计数 `fc_archive_failed_total`；提供 `fcoffer archive restore --symbol ...` 回读校验。

## 5. 混合检索（防性能雪崩）

严禁全局暴力向量计算。固定流程：

1. 校验 `symbol` 必填且格式合法（A 股 6 位、美股 ticker 规则），拒绝空过滤请求。
2. B-Tree 索引按 `symbol` + `ann_date` 区间过滤，得到候选集。
3. 候选集内计算 `embedding <=> :q` 余弦距离，取 top-k。
4. 返回时附带 `symbol`、`ann_date`、`ann_type`、`minio_url` / `dwd_ref_id`，保证可回溯。

上限：`top_k ≤ 50`，`year_range ≤ 10 年`；超限直接 400，不静默截断。

## 6. 对外接口

### 6.1 MCP 工具（首期 3 个，严格按文档）

| 工具 | 入参 | 行为 |
| --- | --- | --- |
| `query_structured_data` | `symbol, metric, year_range` | 查 DWS 宽表，`metric` 走白名单 |
| `search_announcements_semantically` | `symbol, query_text, year_range` | 先过滤后向量 |
| `get_risk_history` | `symbol` | 返回历史风险事件 |

传输：`fcoffer mcp stdio`、`fcoffer mcp http`。HTTP 模式绑定回环地址，需 Bearer Token 与 Host 白名单。

### 6.2 REST

- `GET /health`、`GET /metrics`
- `GET /v1/structured/{symbol}`、`POST /v1/search/announcements`、`GET /v1/risk/{symbol}`
- 统一返回信封：`{code, message, data, trace_id}`；错误码见 `docs/specs/cli.md` 第 5 节。

### 6.3 Skill SOP（`skills/financecoffer/SKILL.md`）

- 何时使用：历史长周期数据、模糊语义搜索、历史风险回溯。
- 查今日实时行情优先使用 TickHub。
- 严禁大模型脑补历史财务数据；语义检索必须提取 `symbol` 作为前置过滤。

## 7. 监控指标

`GET /metrics` 暴露 Prometheus 指标：

| 指标 | 类型 | 说明 |
| --- | --- | --- |
| `fc_etl_consume_lag_seconds` | Gauge | 消费延迟，> 60s 告警 |
| `fc_etl_events_total{status}` | Counter | 消费成功/失败 |
| `fc_dirty_data_total{reason}` | Counter | 落库脏数据 / 勾稽失败总数 |
| `fc_missing_chunk_total` | Counter | 大文本转存或切片失败数 |
| `fc_archive_failed_total` | Counter | MinIO 归档失败 |
| `fc_vector_search_seconds` | Histogram | 混合检索耗时 |
| `fc_pg_pool_in_use` | Gauge | 连接池占用 |

上游指标 `qf_stockdata_source_error_total`、`qf_stockdata_proxy_active_count` 由 TickHub 暴露，FinanceCoffer 只做告警规则引用，不重复定义。

## 8. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FC_DB_URL` | — | `postgresql://user:pass@127.0.0.1:5432/financecoffer` |
| `FC_EMBEDDING_DIM` | `1024` | 向量维度，必须与 `vector.announcement_chunk.embedding` 一致 |
| `FC_EMBEDDING_PROVIDER` | `local` | `local`（bge-m3，DEC-0005）；`openai` 仅作备用开关 |
| `FC_EMBEDDING_MODEL` | `BAAI/bge-m3` | 模型名，随服务端部署一致 |
| `FC_EMBEDDING_URL` | `http://127.0.0.1:8080/v1/embeddings` | 本地 embedding 服务地址（OpenAI 兼容） |
| `FC_EMBEDDING_API_KEY` | — | 备用；本地服务可留空 |
| `FC_MINIO_ENDPOINT` | `127.0.0.1:9000` | MinIO |
| `FC_MINIO_ACCESS_KEY` | — | 密钥，仅服务端读取 |
| `FC_MINIO_SECRET_KEY` | — | 密钥，日志脱敏 |
| `FC_MINIO_BUCKET` | `financecoffer` | 桶名 |
| `FC_EVENT_CONSUMER` | `redis` | `redis` / `kafka` / `webhook` |
| `FC_REDIS_URL` | `redis://127.0.0.1:6379/1` | 事件流 |
| `FC_MCP_TOKEN` | — | MCP HTTP Bearer |
| `FC_COMPLIANCE_ENABLED` | `false` | 合规标记开关（DEC-0010）：关闭时不读写 `compliance_tier`/`commercial_use`，开启时写入必填、读取回填 `meta.compliance_tier` |
| `FC_LOG_LEVEL` | `INFO` | 日志级别 |

完整变量清单见 `docs/specs/naming.md`。

## 9. 部署

`deploy/docker-compose.yml` 计划包含：PostgreSQL 16 + PGVector、MinIO、Redis、`financecoffer` 服务（API + ETL 两个进程，由 `fcoffer start-all` 监督）。服务以非 root 用户运行，仅暴露 `/metrics` 给内网 Prometheus。

## 10. 验收口径

测试工程师按本文与 `requirements-traceability.md` 逐条比对；安全工程师按 S-01…S-10 检查边界。最低通过线：

1. 文档中出现的每个表、字段、索引、约束均可在迁移脚本中找到对应实现（补齐项须在决策记录说明）。
2. 三个 MCP 工具可按文档签名调用并返回可回溯出处。
3. 无 symbol 的向量检索请求必须被拒绝。
4. 重复投递同一 `event_id` 不产生重复数据。
5. DWD 中同一报告期存在多版本，历史版本不被覆盖。
6. 冷数据归档后 PG 大字段为 `NULL` 且 `is_cold = true`，可 restore 校验。

## 11. 里程碑

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| M0 | 规格、命名、CLI、包名、参考对比、任务拆解 | 本文档集 |
| M1 | 骨架 + 迁移 + ODS + DWD | 可 `db migrate`，可落 ODS/DWD |
| M2 | ETL 消费 + 幂等 + DLQ + DWS | 事件端到端可跑 |
| M3 | 向量切片 + embedding + 混合检索 | 三个 MCP 工具可用 |
| M4 | 归档 + 指标 + 部署 + 安全加固 | 可交付测试/安全评审 |

## 12. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-26 | v0.1 | 首版，依据 v1.0 文档更名并整理 |
| 2026-10-26 | v0.2 | G-06 定案：embedding 改用本地 bge-m3 1024 维（DEC-0005），DDL、环境变量、CLI 同步更新 |
| 2026-10-27 | v0.3 | G-11 改为运行时开关 `FC_COMPLIANCE_ENABLED`（默认关闭，DEC-0010）；§6.2 REST 接口进入实现范围 |
| 2026-10-27 | v0.4 | T-017a：`cli.md` 新增错误码表（第 5 节），同步 §6.2 的交叉引用 |
| 2026-10-27 | v0.5 | 清理历史产品名与内部环境信息，文档只保留 FinanceCoffer 命名 |
