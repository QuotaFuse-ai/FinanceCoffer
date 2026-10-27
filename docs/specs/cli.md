# FinanceCoffer CLI 命令清单（`fcoffer`）

版本：v0.4（设计稿）
原则：CLI 与 MCP 共用同一服务层与返回信封；stdout 只输出数据，日志/进度全部走 stderr。
变更记录：v0.2 新增第 6 节错误码表（含 `E_NOT_FOUND`、`E_RATE_LIMITED`），`config show` 明确密钥始终脱敏。
v0.3 按 T-005 实装 `ods` 四个子命令（`--etl-status` 取值改为 DDL 口径 `pending|done|failed`），明确 `--raw` 与 `replay` 语义。
v0.4 按 T-008 实装 `dws build` / `dws show` / `dws rebuild`，§3.9 字段表改为 DDL 口径（指标移入 `metrics_json`），补充确定性、部分失败与合规规则。

## 1. 全局参数

```text
fcoffer [GLOBAL OPTIONS] COMMAND [ARGS]...
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--version` | — | 打印版本并退出 |
| `--output, -o` | `json` | `json` / `table` / `csv` / `yaml` |
| `--fields` | — | 逗号分隔字段投影 |
| `--tier` | `full` | `brief` / `medium` / `full` |
| `--no-cache` | off | 跳过查询缓存 |
| `--refresh-cache` | off | 刷新查询缓存 |
| `--timeout` | `30` | 单次查询/操作超时（秒） |
| `--retries` | `2` | 可重试操作的次数 |
| `--db-url` | 环境变量 | 临时覆盖 `FC_DB_URL` |
| `--trace-id` | 自动 | 透传链路 ID |
| `--quiet, -q` | off | 仅 stderr 输出告警/错误 |
| `--verbose, -v` | off | 调试日志 |

退出码沿用 TickHub 风格：

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 一般错误 |
| 2 | 参数错误 |
| 3 | 配置错误 |
| 4 | 存储不可用（PG / MinIO） |
| 5 | 依赖不可用（Redis / embedding） |
| 6 | 鉴权失败 |
| 8 | 批量部分失败 |
| 130 | Ctrl-C 中断 |

## 2. 命令树总表

| 组 | 命令 | 文档依据 | 优先级 |
| --- | --- | --- | --- |
| 初始化 | `init` | 工程落地 | P0 |
| 自检 | `doctor` | 参考 TickHub | P0 |
| 配置 | `config show` / `config check` | 环境变量章节 | P0 |
| 数据库 | `db migrate` / `db status` / `db partitions create` / `db partitions list` | 3.1 分区 | P0 |
| 服务 | `serve api` / `serve admin` / `start-all` | 部署形态 | P0 |
| MCP | `mcp stdio` / `mcp http` / `mcp tools` | 4.2 | P0 |
| ETL | `etl consume` / `etl status` / `etl pause` / `etl resume` / `etl replay` / `etl dlq list` / `etl dlq retry` | 1.2、4.1 | P0 |
| 接入 | `ingest event` / `ingest backfill` / `ingest webhook` | 4.1 | P0/P1 |
| ODS | `ods list` / `ods show` / `ods stats` / `ods replay` | 2.1、3.1 | P0 |
| DWD | `dwd show` / `dwd versions` / `dwd backfill` / `dwd verify` | 2.2、3.2 | P0 |
| DWS | `dws build` / `dws show` / `dws rebuild` | 2.3 | P0 |
| 向量 | `vector chunk` / `vector embed` / `vector index` / `vector search` / `vector reindex` / `vector stats` | 2.4、3.4、3.5 | P0 |
| 冷热 | `archive run` / `archive status` / `archive restore` / `archive verify` | 3.3、7 | P1 |
| 查询 | `query structured` / `query risk` / `search announcements` | 4.2 | P0 |
| 通用工具 | `tool list` / `tool call` | 参考 TickHub | P1 |
| 监控 | `metrics` / `health` | 5 | P0 |
| Skill | `skill install` | 4.2 | P2 |

> 不提供的命令：任何抓取/爬虫命令（如 `crawler`、`fetch`）。FinanceCoffer 只存不算，抓取属于 TickHub。

## 3. 命令明细

### 3.1 初始化与自检

```bash
fcoffer init [--db-url URL] [--with-extensions] [--dry-run]
fcoffer doctor [--check db,minio,redis,embedding,mcp] [--output json]
fcoffer config show [--json] [--redact]
fcoffer config check
```

- `init`：创建 schema、扩展（`vector`）、基础表、分区与 HNSW 索引；可重复执行（幂等）。
- `doctor`：逐项探测 PG 版本/扩展、MinIO bucket、Redis Stream、embedding 维度、MCP 协议帧。
- `config show`：输出生效配置，密钥**始终**脱敏（DSN 密码、MinIO AccessKey/SecretKey、embedding key、MCP token 均显示为 `***`）；`--json` 走统一信封，`--redact` 仅为兼容保留。
- `config check`：校验配置完整性，报告缺失项（不打印任何密钥）。

### 3.2 数据库

```bash
fcoffer db migrate [--revision head|--sql]
fcoffer db status
fcoffer db partitions create --month 2026-11 [--ahead 3]
fcoffer db partitions list [--table ods.stock_raw_data]
```

### 3.3 服务

```bash
fcoffer serve api --host 127.0.0.1 --port 8100 [--workers 2]
fcoffer serve admin --host 127.0.0.1 --port 8788
fcoffer start-all --host 127.0.0.1 --port 8100 [--consumer redis] [--no-etl]
```

- `serve api`：FastAPI，暴露 REST、`/health`、`/metrics`。
- `start-all`：主进程 API，子进程 ETL 消费；子进程崩溃重启策略参考 TickHub（5 分钟 3 次后停止并告警，API 继续服务）。
- 默认只监听回环地址；HTTP 暴露到非回环地址必须配置 token 与 Host 白名单。

### 3.4 MCP

```bash
fcoffer mcp stdio
fcoffer mcp http --host 127.0.0.1 --port 8100 [--token TOKEN] [--allowed-hosts h]
fcoffer mcp tools [--json]
```

`mcp tools` 首期固定返回 3 个工具：

```text
query_structured_data(symbol, metric, year_range)
search_announcements_semantically(symbol, query_text, year_range)
get_risk_history(symbol)
```

### 3.5 ETL 消费

```bash
fcoffer etl consume [--consumer redis|kafka|webhook] [--group fc-etl] [--once]
fcoffer etl status
fcoffer etl pause
fcoffer etl resume
fcoffer etl replay --from-ods [--data-type financial|announcement|kline|risk|quote] \
                   [--symbol 600000] [--since 2026-01-01] [--until 2026-12-31] [--dry-run]
fcoffer etl dlq list [--limit 50]
fcoffer etl dlq retry <event-id>|--all
```

- 消费语义：至少一次；以 `event_id` 幂等去重。
- 失败重试上限默认 3 次，超限进 DLQ 并计数 `fc_dirty_data_total`。
- 消费延迟 = `now - event_time`，暴露为 `fc_etl_consume_lag_seconds`。

### 3.6 手工接入与回填

```bash
fcoffer ingest event --file event.json [--validate-only]
fcoffer ingest backfill --source tickhub-cn|tickhub-us \
                        --from 2020-01-01 --to 2026-10-26 \
                        [--symbols 600000,000001] [--data-type all] [--dry-run]
fcoffer ingest webhook --host 127.0.0.1 --port 8101 [--token TOKEN]
```

- `ingest backfill` 只用于一次性迁移/对账，不作为运行时取数通道；执行时在 ODS 标记 `source=backfill`。
- `ingest event` 是上游事件格式的契约验证入口，测试工程师用它做对照测试。
  默认行为是「校验 → `ops.etl_event_log` 幂等登记 → 追加 ODS」，两步在**同一事务**内完成：
  分区缺失导致 ODS 写入失败时，事件日志登记一并回滚，事件可原样重投。
  `--validate-only` 只校验、不连库。

### 3.7 ODS

```bash
fcoffer ods list [--data-type financial|announcement|kline|quote|risk|research|northbound|heartbeat] \
                 [--symbol S] [--since T] [--until T] [--etl-status pending|done|failed] [--limit 100]
fcoffer ods show <id> [--raw]
fcoffer ods stats [--group-by data_type|source|etl_status]
fcoffer ods replay <id>|--all-failed [--dry-run]
```

- `list` 按 `fetch_time` 倒序；`--since` / `--until` 接受 `YYYY-MM-DD`（UTC 零点，日期型 `--until` 含当天）
  或带时区的 RFC 3339；`--limit` 上限 1000。
- `show --raw` 输出落库时保存的 `raw_text` 原文（末尾换行也保留），不套 JSON 信封，供逐字节比对；
  只有 `raw_file_path` 的历史行没有原文，退化为 `raw_json` 的紧凑 JSON。
- `stats` 只做计数，不做业务汇总。
- `replay` 只把 `ops.etl_event_log` 中对应事件重置为 `received` 交给下游消费者，**不修改 ODS 行**；
  `--dry-run` 只打印 manifest（`id` / `event_id` / `data_type` / `symbol` / `source` / `fetch_time` / `etl_status`），零写入。

### 3.8 DWD

```bash
fcoffer dwd show --symbol S [--report-date D] [--report-type income|balance|cashflow] [--version N]
fcoffer dwd versions --symbol S --report-date D --report-type income
fcoffer dwd backfill --from-ods [--symbol S] [--since T] [--dry-run]
fcoffer dwd verify [--symbol S] [--report-date D]
```

- `dwd show` 默认返回 `MAX(version)`；`--version` 可查看历史快照。
- `dwd verify` 输出 `quality_status` / `confidence` / 金额精度检查结果，不重做勾稽。
- 公告：`fcoffer dwd announcement list|show` 可查看 `minio_url`、`content_hash`、版本。

### 3.9 DWS

```bash
fcoffer dws build (--symbol S [--symbol S ...] | --all) [--as-of YYYY-MM-DD] [--years N]
fcoffer dws show SYMBOL
fcoffer dws rebuild [--as-of YYYY-MM-DD] [--years N]
```

- `build` 每个 `symbol` 产出一行宽表快照；`--symbol` 与 `--all` 互斥，且必须给出其一，否则参数错误（退出码 2）。
- `--as-of` 缺省为 UTC 当天，只按该日期截断 DWD 事实，不用 `now()` 参与计算；`--years` 取 1–10，默认 5。
- 单个 symbol 失败或跳过不影响其他 symbol；存在失败时输出完整 `built` / `skipped` / `failed` 明细，
  并以 `E_PARTIAL_FAILURE`（退出码 8）返回。
- `rebuild` 等价于 `build --all`。
- 同一输入与同一 `--as-of` 重复执行，`metrics_json` / `risk_summary` / `latest_announcements` 的 JSONB 内容逐字节一致
  （仅 `built_at` 变化）。已存 `as_of` 更新时，更早的 `--as-of` 会被跳过（`reason: stale_as_of`），不覆盖较新快照。
- `show` 读取已构建的宽表行；不存在返回 `E_NOT_FOUND`。

宽表字段（以 DDL `dws.stock_agent_analysis_wide` 为准）：`symbol`、`as_of`、`metrics_json`、`risk_summary`、
`latest_announcements`、`fiscal_years`、`source_version`、`built_at`、`compliance_tier`、`commercial_use`。

- 近 5 年指标在 `metrics_json.years`（`year` / `revenue` / `net_profit` / `roe` / `pe` / `pb` 及质量字段）；
  缺失年份显式为 `null` 且 `data_status="missing"`，不做零填充或插值。
- 年度质量只落在 `metrics_json.quality`（`bad_years` / `suspicious_years` / `missing_years`），顶层不再设
  `quality_status` / `confidence` 列。
- 金额为元、4 位小数的定长字符串；`roe` / `pb` 当前恒为 `null` 并给出原因；`pe` 使用 `--as-of` 时点的估值快照
  （临时口径）。
- `risk_summary` 覆盖 `(as_of − 3 年, as_of]`；`latest_announcements` 最多 20 条，按 `ann_date DESC, id DESC`。
- 合规开关关闭时 `compliance_tier` / `commercial_use` 保持 `NULL`；开启时同一 symbol 的 DWD 源行必须同档同商用标记，
  否则该 symbol 失败（不猜测、不取最严档）。

### 3.10 向量

```bash
fcoffer vector chunk --symbol S|--all [--chunk-size 512] [--overlap 50] [--dry-run]
fcoffer vector embed [--pending] [--batch-size 64] [--model BAAI/bge-m3]
fcoffer vector index create|status|drop
fcoffer vector search --symbol S --query TEXT [--year-range 2023-2025] [--top-k 10]
fcoffer vector reindex [--symbol S]
fcoffer vector stats [--symbol S]
```

硬性约束：

- `search` 必须提供 `symbol`；缺省直接报参数错误，杜绝全表向量扫描。
- `top-k` 上限 50；`year_range` 上限 10 年。
- 切片：512 token，重叠 50 token；`embedding vector(1024)`（bge-m3）；HNSW `vector_cosine_ops`。

### 3.11 冷热分离与 MinIO

```bash
fcoffer archive run [--older-than-years 3] [--min-bytes 8192] [--dry-run] [--limit N]
fcoffer archive status [--symbol S]
fcoffer archive restore --symbol S [--since T] [--until T]
fcoffer archive verify [--sample 100]
```

- 转存成功后 PG 大字段置 `NULL`，`is_cold=true`，写入 `minio_url` 与 `content_hash`。
- 失败计数 `fc_missing_chunk_total`；支持断点重跑。

### 3.12 查询（MCP 的 CLI 等价入口）

```bash
fcoffer query structured --symbol S --metric revenue|net_profit|roe|pe|pb --year-range 2021-2025
fcoffer query risk --symbol S [--since T] [--limit 50]
fcoffer search announcements --symbol S --query TEXT --year-range 2023-2025 [--top-k 10]
```

输出信封：

```json
{
  "ok": true,
  "data": {},
  "meta": {
    "source": "dws|dwd|vector",
    "fetched_at": "2026-10-26T10:00:00Z",
    "quality_status": "normal",
    "confidence": "high",
    "compliance_tier": "S|B|C|D",
    "trace_id": "..."
  }
}
```

### 3.13 通用工具注册表

```bash
fcoffer tool list [--layer structured|vector|risk]
fcoffer tool call <tool-name> --params '{"symbol":"600000"}' [--fields ...]
```

### 3.14 监控与健康

```bash
fcoffer metrics [--format prometheus|json]
fcoffer health [--check db,minio,redis,etl] [--output json]
```

指标清单（本产品）：

| 指标 | 类型 | 告警 |
| --- | --- | --- |
| `fc_etl_consume_lag_seconds` | Gauge | > 60s |
| `fc_etl_events_total{status}` | Counter | failed 增长 |
| `fc_dirty_data_total{reason}` | Counter | 持续增长 |
| `fc_missing_chunk_total` | Counter | > 0 |
| `fc_vector_search_seconds` | Histogram | P95 超阈值 |
| `fc_archive_job_total{status}` | Counter | failed 增长 |
| `fc_db_pool_in_use` | Gauge | 接近上限 |

上游指标 `qf_stockdata_source_error_total`、`qf_stockdata_proxy_active_count` 由 TickHub 暴露；FinanceCoffer 不重复实现，只做抓取/告警联动（待确认部署方式）。

### 3.15 Skill

```bash
fcoffer skill install --target claude|cursor|codex [--path DIR] [--force]
```

## 4. 命令实施顺序（建议）

1. `init` / `db migrate` / `doctor`
2. `etl consume` / `ods` / `dwd`
3. `dws` / `vector`
4. `query` / `mcp` / `serve api` / `metrics`
5. `archive` / `ingest backfill` / `tool` / `skill`

## 5. 错误码

错误码是 CLI / REST / MCP 共用的稳定契约：格式 `^E_[A-Z0-9_]{2,48}$`，只增不改（DEC-0007）。调用方遇到未知错误码一律按 `E_INTERNAL` 处理。

| 错误码 | CLI 退出码 | REST HTTP | 含义 |
| --- | --- | --- | --- |
| `E_INTERNAL` | 1 | 500 | 未预期错误（响应不含堆栈） |
| `E_INVALID_ARGUMENT` | 2 | 400 | 参数或请求体校验失败 |
| `E_CONFIG` | 3 | 503 | 配置缺失或非法 |
| `E_STORAGE` | 4 | 500 | PG / MinIO 等存储不可用 |
| `E_DEPENDENCY` | 5 | 503 | Redis / embedding 等依赖不可用 |
| `E_AUTH` | 6 | 401 | 鉴权失败 |
| `E_PARTIAL_FAILURE` | 8 | 500 | 批量操作部分失败 |
| `E_INTERRUPTED` | 130 | 500 | Ctrl-C 中断 |
| `E_NOT_IMPLEMENTED` | 1 | 501 | 命令或接口尚未实现 |
| `E_NOT_FOUND` | 1 | 404 | 目标不存在或无数据 |
| `E_RATE_LIMITED` | 1 | 429 | 请求超过限流阈值，响应带 `Retry-After` |

REST 限流默认开启：按客户端 IP 的进程内令牌桶，默认 120 次/分钟，仅作用于 `/v1/*`，`/health` 与 `/metrics` 不限流。`FC_RATE_LIMIT_ENABLED=false` 关闭；`FC_RATE_LIMIT_PER_MINUTE` 调整阈值（1–10000）。
