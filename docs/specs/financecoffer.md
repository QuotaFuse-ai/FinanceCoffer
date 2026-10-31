# FinanceCoffer 产品规格

版本：v0.11（设计稿）
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

- 主键 `symbol`；`as_of` 标注计算时点；`metrics_json` 是 object，逐年指标放在其 `years` 数组内（DDL CHECK 要求 object）；`risk_summary`、`latest_announcements` 为预计算摘要。
- 构建方式：`fcoffer dws build --symbol ...` / 定时全量重建；只读 DWD，不回写。
- 字段映射、NULL 规则、幂等与对账方案见 `docs/specs/dws-metrics.md`；两者冲突时以该文为准。

### 4.4 向量检索层（PGVector）

- 数据源：直接复用上游 `clean_content`，不重复清洗。
- 切片：512 token，重叠 50 token。
- 模型：本地 `BAAI/bge-m3`，维度 **1024**（`FC_EMBEDDING_DIM` 落库前必须与表定义一致）。
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

归档任务幂等：先上传再置空，失败保持原状并以 `ops.archive_job` 的 `status = 'failed'` 记录（对外指标为 `fc_archive_job_total{status="failed"}`）；提供 `fcoffer archive restore --symbol ...` 回读校验。

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
| `query_structured_data` | `symbol, metric, year_range, valuation_basis` | 查 DWS 宽表，`metric` 走白名单；`valuation_basis` 可选 `snapshot` / `reference` |
| `search_announcements_semantically` | `symbol, query_text, year_range` | 先过滤后向量 |
| `get_risk_history` | `symbol` | 返回历史风险事件 |

传输：`fcoffer mcp stdio`、`fcoffer mcp http`。HTTP 模式绑定回环地址，需 Bearer Token 与 Host 白名单。

### 6.2 REST

- `GET /health`、`GET /metrics`
- `GET /v1/structured/{symbol}`、`POST /v1/search/announcements`、`GET /v1/risk/{symbol}`
- 统一返回信封：`{code, message, data, trace_id}`；错误码见 `docs/specs/cli.md` 第 5 节。

#### 6.2.1 `GET /health` 就绪检查

`/health` 是编排系统的就绪探针：**永不抛错、永不返回 5xx**。依赖挂掉只体现在 `checks` 与整体 `status` 上，不会被误读成「API 进程坏了」。响应仍是标准信封，`data`：

```json
{
  "status": "ok",
  "version": "0.1.0",
  "checks": {
    "database": {"status": "ok", "elapsed_ms": 3.41},
    "embedding": {"status": "ok", "elapsed_ms": 12.08},
    "redis": {"status": "ok", "elapsed_ms": 0.83},
    "etl": {"status": "ok", "lag_seconds": 0.0, "pending": 0},
    "minio": {"status": "not_configured"}
  },
  "compliance_enabled": false
}
```

**统一形状**：`checks` 每一项都是对象，`status` 必填，各项自有字段留在对象内。`elapsed_ms` 是探测耗时（两位小数）；**只有真正发起了探测才有**——`not_configured` 不探测，也就不给 `elapsed_ms`。

**`status` 词表是闭集，五项共用，不允许出现第五个值**：

| 值 | 含义 |
| --- | --- |
| `ok` | 正常 |
| `degraded` | 连得上，但自身状态不健康（当前只有 etl 的积压用这一档） |
| `unreachable` | 已配置，但连不上 / 探测抛错 |
| `not_configured` | 未配置，压根没发起探测 |

`not_installed` 已废止：客户端未安装一律归 `not_configured`。

**`checks` 完整清单**：

| 项 | 取值集合 | 自有字段 | 探测方式 |
| --- | --- | --- | --- |
| `database` | `ok` / `unreachable` / `not_configured` | `elapsed_ms`（`not_configured` 时无） | 仓库执行 `SELECT 1`；无 DSN（运行时 DSN 与 `FC_DB_URL` 都为空）→ `not_configured` |
| `embedding` | `ok` / `unreachable` / `not_configured` | `elapsed_ms`（同上） | 请求 embedding 服务的 `GET /health`，校验 `status` 与维度；`FC_EMBEDDING_URL` 缺 scheme/host → `not_configured` |
| `redis` | `ok` / `unreachable` / `not_configured` | `elapsed_ms`（客户端未安装时无） | 一次 `PING`，单次操作超时 0.5s；redis 客户端未安装（基础安装、未装 `[etl]` extra）→ `not_configured` |
| `etl` | `ok` / `degraded` / `unreachable` | `lag_seconds`、`pending` | 一条聚合 SQL，与 §7 的 `fc_etl_consume_lag_seconds` 共用同一个定义 |
| `minio` | `ok` / `unreachable` / `not_configured` | `elapsed_ms`（`not_configured` 时无） | 一次 `HeadBucket`（不 list、不上传下载），connect / read 各 0.5s、`max_attempts=0`（只发一次请求，最坏 ~1s 内返回）；`FC_MINIO_ACCESS_KEY` / `FC_MINIO_SECRET_KEY` 任一为空，或未装 `s3` extra → `not_configured`；已配置但探测抛错（连接拒绝 / 超时 / 凭据错 / bucket 缺失）→ `unreachable` |

**整体判定表**（`status` 只有 `ok` / `degraded` 两档）：

| 场景 | 该项 `status` | 整体 `status` |
| --- | --- | --- |
| 全部正常 | 各项 `ok` | `ok` |
| database 不通 | `unreachable` | `degraded` |
| database 未配置 | `not_configured` | `degraded` |
| embedding 不可用 | `unreachable` | `degraded` |
| embedding 未配置 | `not_configured` | `degraded` |
| redis 不通 | `unreachable` | `ok` |
| redis 客户端未安装 | `not_configured` | `ok` |
| etl 有积压，最老一条等待超过 60 秒 | `degraded` | `ok` |
| etl 有积压，等待未超过 60 秒 | `ok` | `ok` |
| etl 无积压 | `ok` | `ok` |
| etl 采样失败（队列读不到） | `unreachable` | `ok` |
| minio 未配置 | `not_configured` | `ok` |
| minio 已配置但连不上 | `unreachable` | `ok` |

判定语义：**只有核心依赖 `database` / `embedding` 不是 `ok`，整体才 `degraded`**。`redis` / `etl` / `minio` 只出现在 `checks` 里，不影响整体 `status`。

**为什么 redis 挂了整体仍是 `ok`**：整体 `status` 决定编排要不要把查询流量路由过来。能服务读请求的进程，不应因为缓存、事件流、归档这类可选或辅助依赖抖动而被摘掉——否则一次 Redis 抖动会连带下线本来可用的查询面。这些依赖的异常由 `checks` 与告警（§7、`deploy/prometheus/rules.yml`）负责暴露。

**etl 的判定口径**：

- 阈值 **60 秒**，对齐既有告警 `FinanceCofferEtlConsumeLagHigh`（`deploy/prometheus/rules.yml`：`fc_etl_consume_lag_seconds > 60`）。单元测试断言两侧一致，避免就绪页与告警页对同一积压给出相反结论。
- 无积压（`pending == 0`）→ `ok`；最老一条等待超过 60 秒 → `degraded`；未超过 60 秒 → `ok`（正常消费中，告警本身也不在该区间触发）。
- 采样失败 → `unreachable`，`lag_seconds` / `pending` 均为 `null`。
- 有积压但取不到 `received_at`（`lag_seconds` 为 `null`）→ `degraded`：已确认有积压，只是测不出严重程度，归 `ok` 会把积压藏起来。

**etl 为什么没有 `elapsed_ms`**：这条探针的产出是「队列里还有多少、最老的等了多久」，单条聚合查询的往返耗时不是运维能据此决策的量；另外四项是点对点连通性探测，耗时本身就是信号，所以有。这里的形状不一致是刻意的，不要为了「整齐」补一个无意义的数值。

**已知限制**：每次 `/health` 会为 `database` 与 `etl` 各开一条独立连接（`ping()` 与 `etl_queue()` 各自建连），即一次请求两条连接。当前未合并为一条，属于已知开销，不是缺陷。

**与 CLI `fcoffer health` 的关系**（同口径 / 刻意不同）：

| 维度 | REST `GET /health` | CLI `fcoffer health` |
| --- | --- | --- |
| 检查项 | `database` / `embedding` / `redis` / `etl` / `minio` | `db` / `minio` / `redis` / `etl` |
| `status` 词表 | `ok` / `degraded` / `unreachable` / `not_configured` | `ok` / `warn` / `fail` |
| 单项形状 | 以名称为键的对象：`{status, elapsed_ms?, ...}` | 数组：`[{name, status, reason, details?}]` |
| 整体 `status` | `ok` / `degraded`，只看核心依赖 | `ok` / `degraded` / `fail`；任一 `fail` → `E_DEPENDENCY`，退出码 5；只有 `warn` → `degraded`，退出码 0 |
| 定位 | 编排探针：永不 5xx，不为可选依赖摘流量 | 运维诊断：面向人，可用 `warn` 与退出码表达可选依赖异常 |

同名但语义不同，文档与实现都以本表为准：

- CLI 的 `etl` 检查 **Redis Stream 消费组是否挂上**、有无 idle 超过 60 秒的 pending；REST 的 `etl` 读 **`ops` 表里 `received` / `processing` 状态的积压与最老等待时长**。
- CLI 的 `db` 探针额外取 `SHOW server_version` 作为诊断信息；REST 只做 `SELECT 1`。
- REST 与 CLI 都检查 `minio`；区别在于 REST 的 `minio` 只进 `checks`、不影响整体 `status`，CLI 的 `minio` 参与退出码。

刻意不同的原因：两者读者不同。REST 由编排消费，必须保持两档、不被可选依赖左右，且异常只能体现在响应体里；CLI 是人用来问「到底哪里坏了」，需要覆盖 `minio`、用 `warn` / `fail` 分档，并用退出码把可选依赖的故障带进脚本与巡检。因此**两边的输出形状与词表互不兼容，不可互相解析**。


### 6.3 Skill SOP（`skills/financecoffer/SKILL.md`）

- 何时使用：历史长周期数据、模糊语义搜索、历史风险回溯。
- 查今日实时行情优先使用 TickHub。
- 严禁大模型脑补历史财务数据；语义检索必须提取 `symbol` 作为前置过滤。

## 7. 监控指标

`GET /metrics` 暴露 Prometheus 指标：

| 指标 | 类型 | 说明 |
| --- | --- | --- |
| `fc_etl_consume_lag_seconds` | Gauge | 消费延迟，> 60s 告警 |
| `fc_etl_events_total{status}` | Counter | 消费成功/失败；`status` ∈ `received/processing/done/failed/dlq/duplicate` |
| `fc_dirty_data_total{reason}` | Counter | 落库脏数据 / 勾稽失败总数 |
| `fc_missing_chunk_total` | Counter | 大文本转存或切片失败数 |
| `fc_archive_job_total{status}` | Counter | 归档任务；`status` ∈ `pending/running/done/failed`，`failed` 即归档失败（唯一名字） |
| `fc_vector_chunks_total{status}` | Counter | 切片写入/删除行数 |
| `fc_vector_embeddings_total{status}` | Counter | 向量接受/拒收条数 |
| `fc_vector_search_seconds` | Histogram | 混合检索耗时 |
| `fc_pg_pool_in_use` | — | **不适用，不再暴露**（原因见下） |

三条输出约定：

1. **注册即输出**：上表每个家族在每次抓取时都会出现，没有数据时输出 0 值基线，不按「非空才渲染」。`status` / `reason` 的零值基线取 DDL 的 CHECK 值域与 `etl/processor.py:classify_failure` 的固定分类；其他 `reason` 出现时按实际值追加。`ops.*` 采样失败时同样输出零值基线，并在 API 日志打 WARN（`metrics: ops sample unavailable`），避免「无告警」被误读为「一切正常」。
2. **仅 Prometheus 文本格式**：`/metrics` 只输出 `text/plain; version=0.0.4`，`Accept` 头不影响响应体；Prometheus 只解析该格式，需要 JSON 的脚本请用 CLI `fcoffer metrics --format json`（客户端转换）。
3. **归档指标只有一个名字**：`fc_archive_job_total{status}`。`fc_archive_failed_total` 已废弃，不再出现在 `/metrics`，也不再出现在 `fcoffer archive stats|verify` 的 JSON（失败数读同结构的 `failed` 字段）。

`fc_pg_pool_in_use` 不适用：查询层用 psycopg **直连**（`query/service.py` 的 `_connect()`），每个请求建连、结束即归还，进程内没有连接池可采样；持有 SQLAlchemy 池的 worker 不暴露 HTTP 端口。按「不伪造恒为 0 的指标」原则不实现该家族，等真正引入连接池后再补真实数值。

上游指标 `qf_stockdata_source_error_total`、`qf_stockdata_proxy_active_count` 由 TickHub 暴露，FinanceCoffer 只做告警规则引用，不重复定义。

## 8. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FC_DB_URL` | — | `postgresql://user:pass@127.0.0.1:5432/financecoffer` |
| `FC_EMBEDDING_DIM` | `1024` | 向量维度，必须与 `vector.announcement_chunk.embedding` 一致 |
| `FC_EMBEDDING_PROVIDER` | `local` | `local`（bge-m3）；`openai` 仅作备用开关 |
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
| `FC_COMPLIANCE_ENABLED` | `false` | 合规标记开关：关闭时不读写 `compliance_tier`/`commercial_use`，开启时写入必填、读取回填 `meta.compliance_tier` |
| `FC_LOG_LEVEL` | `INFO` | 日志级别 |

加载时的取值校验有两种，都以内层 `E_CONFIG` 报错并点名变量，不回显值：

- **凭据字段拒绝模板占位符**（`FC_EMBEDDING_API_KEY`、`FC_MINIO_ACCESS_KEY`、`FC_MINIO_SECRET_KEY`、`FC_MCP_TOKEN`）：值精确等于 `CHANGE_ME` 时拒绝。部署模板现在直接留空，这条校验保留为旧模板文件的兜底。空值与缺省值表示「未启用」（loopback-only 部署不设 MCP token、`local` provider 不用 API key），照常加载，且 `fcoffer config show` 中保持为空而非掩码。照抄模板而未填写的副本仍然无法启动：`compose.yaml` 对必填项用 `${VAR:?}` 拒绝空值，`FC_DB_URL` 为空时应用侧报 `E_CONFIG`（not configured）。
- **DSN 项的 scheme 校验**（`FC_DB_URL` 须为 `postgresql://` 或 `postgresql+psycopg://`，`FC_REDIS_URL` 须为 `redis://` 或 `rediss://`）。

### 8.1 凭据引用（不落进程环境的密钥）

五个字段的值可以写成引用而不是明文：`FC_DB_URL`、`FC_MINIO_ACCESS_KEY`、`FC_MINIO_SECRET_KEY`、`FC_MCP_TOKEN`、`FC_EMBEDDING_API_KEY`。

| 语法 | 材料放在哪 | 说明 |
| --- | --- | --- |
| `keyring:<service>/<account>` | 操作系统凭据库 | Windows = 凭据管理器（generic credential，目标名 `<service>/<account>`）；macOS = Keychain、Linux = Secret Service，走可选的 `keyring` 包 |
| `file:<path>` | 一个文件 | 内容首行即凭据（尾部换行剥离）；Windows 上可写成 `dpapi:v1:<base64>` 的 DPAPI 封装，只对本机本用户可解 |
| `env:<VAR>` | 另一个环境变量 | 显式的向后兼容退路：值仍然在进程环境里，只是不在**这个字段**里 |

解析口径：

- **引用与明文的区分只看前缀**。`keyring:` / `file:` / `env:` 三个 scheme 之外一律当明文，所以 `postgresql://user:pass@host/db` 不会被误解析——这是必须的，DSN 自己就带冒号。大小写敏感（`FILE:` 是明文的一部分）。
- **解析发生在 `load_config()` 内、`Config` 校验之前**（`config/loader.py`）。因此校验、DSN scheme 检查、占位符检查看到的都是解析后的值，`Config` 里仍然是 `SecretStr`，`fcoffer config show` / 日志 / `/health` 的脱敏口径完全不变。
- **引用解析失败是硬失败**：文件不存在或不可读或为空、DPAPI 解不开、keyring 条目不存在、`env:` 指向的变量未设置、引用语法本身写错，一律 `E_CONFIG`（退出码 3），消息点名是哪个字段、哪种引用、缺什么、下一步做什么；绝不静默退化成空值——空 DSN 会连出一个「配置正常但连不上」的服务，比启动失败难查得多。
- **配置的读取顺序不变**（内置默认值 → 仓库 `.env` → 进程环境 → `--db-url`），引用解析发生在该顺序**之后**：先决定哪个值生效，再决定它是不是引用。`env:` 引用的变量在同一份环境映射里查找，所以测试与部署的桩环境是权威的。
- **跨 fork 的是引用**：`serve api --workers N` 导出给 worker 的是这串引用（见 `docs/specs/cli.md` §3.3），每个 worker 自己解析一次，明文不进任何进程的环境块。写成明文的凭据仍按明文导出，行为与旧版一致。

存引用指向的材料用 `fcoffer config secret set|list|delete`（`docs/specs/cli.md` §3.1）；密钥从交互式隐藏输入读入，不经 argv，也不回显。

各平台的 keyring 依赖：

- **Windows：无额外依赖**。`ctypes` 直接调 `advapi32` 的 `CredReadW/CredWriteW/CredDeleteW/CredEnumerateW` 与 `crypt32` 的 `CryptProtectData/CryptUnprotectData`（实现见 `core/wincred.py`）。选它的理由与备选方案记录在 `docs/decisions/DEC-0031-credential-references-resolved-at-load.md`。
- **macOS / Linux：需要 `keyring` 包**（`pip install 'financecoffer[keyring]'`）。未安装时不是静默降级，而是一条 `E_CONFIG`：提示装该 extra，或改用它本来就能读的 `file:` / `env:` 引用。
- `file:` 与 `env:` 在任何平台都零依赖；DPAPI 封装只在 Windows 可解，非 Windows 读到 DPAPI 文件是明确的硬失败。

`.env` 里放引用示例（值列写引用而不是密钥，模板才能进仓库）：

```bash
FC_DB_URL=file:/etc/financecoffer/db_url.txt
FC_MINIO_ACCESS_KEY=keyring:financecoffer/minio_access_key
FC_MINIO_SECRET_KEY=file:/etc/financecoffer/minio_secret.txt
FC_MCP_TOKEN=keyring:financecoffer/mcp_token
FC_EMBEDDING_API_KEY=env:FC_EMBEDDING_KEY
```

完整变量清单见 `docs/specs/naming.md`。

## 9. 部署

`deploy/docker-compose.yml` 计划包含：PostgreSQL 16 + PGVector、MinIO、Redis、`financecoffer` 服务（API + ETL 两个进程，由 `fcoffer start-all` 监督）。服务以非 root 用户运行，仅暴露 `/metrics` 给内网 Prometheus。

## 10. 验收口径

验收环节按本文与 `requirements-traceability.md` 逐条比对；第 3 节的安全边界按 S-01…S-10 检查。最低通过线：

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
| 2026-10-26 | v0.2 | G-06 定案：embedding 改用本地 bge-m3 1024 维，DDL、环境变量、CLI 同步更新 |
| 2026-10-27 | v0.3 | G-11 改为运行时开关 `FC_COMPLIANCE_ENABLED`（默认关闭）；§6.2 REST 接口进入实现范围 |
| 2026-10-27 | v0.4 | `cli.md` 新增错误码表（第 5 节），同步 §6.2 的交叉引用 |
| 2026-10-27 | v0.5 | 清理历史产品名与内部环境信息，文档只保留 FinanceCoffer 命名 |
| 2026-10-29 | v0.6 | §7 改为「注册即输出」（无数据输出 0 值基线）、明确 `/metrics` 仅 Prometheus 文本格式（`Accept` 不生效，JSON 走 CLI）、归档指标统一为 `fc_archive_job_total{status}`（`fc_archive_failed_total` 废弃，§4.5 同步）、`fc_pg_pool_in_use` 标注不适用（查询层 psycopg 直连，无连接池）；补登 `fc_vector_chunks_total`、`fc_vector_embeddings_total`、`fc_archive_job_total` 的 `status` 值域 |
| 2026-10-29 | v0.7 | 新增 §6.2.1：`/health` 的 `checks` 统一为 `{status, elapsed_ms?, ...}` 对象、`status` 闭集词表（`ok`/`degraded`/`unreachable`/`not_configured`，`not_installed` 废止）、降级判定表、`etl` 以 60 秒告警阈值为积压判定（消灭 `unknown`）、REST 与 CLI `fcoffer health` 的差异及原因、每次请求两连接的已知限制；依据：客户端可消费的形状必须唯一，且就绪判定与告警不得对同一积压给出相反结论 |
| 2026-10-29 | v0.8 | §6.2.1 的 `minio` 探针由「尚未实现」改为已落地的 `HeadBucket` 单次探测（含 `not_configured` / `unreachable` 的判定条件），REST 与 CLI 检查项清单同步；`redis` 的「客户端未安装」分支按统一形状不再给 `elapsed_ms`，自有字段列补注。依据：可选依赖的就绪语义以代码可验证的行为为准，`elapsed_ms` 只表示真正发起过的探测耗时 |
| 2026-10-29 | v0.9 | 变更记录 v0.4 行删除内部任务号，只留业务描述（公开仓库规则：`docs/` 不出现内部任务编号）；本文件现已无任务号残留，其余历史行的编号形态不在此单处理 |
| 2026-10-29 | v0.10 | §8 补记加载时的两类取值校验：凭据字段拒绝字面量 `CHANGE_ME`（照抄模板的副本无法启动）、DSN 项 scheme 校验；写明空值/缺省表示「未启用」仍照常加载。依据：配置层行为变更后规格须同步，否则测试无法按本文比对 |
| 2026-10-29 | v0.11 | §10 验收口径改为职能表述（「验收环节」「第 3 节的安全边界」），删除团队称谓（公开仓库规则：`docs/` 不出现内部任务编号与团队称谓） |
| 2026-10-29 | v0.12 | §8 同步部署模板口径变更：`deploy/.env.example` 的凭据项一律留空，`CHANGE_ME` 校验降为旧模板兜底；「照抄模板无法启动」的依据改为 compose 的 `${VAR:?}` 与空 DSN 报 `E_CONFIG`。依据：模板取值策略变更后，规格里描述启动失败原因的句子必须与实际机制一致 |
