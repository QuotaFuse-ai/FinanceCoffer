# FinanceCoffer CLI 命令清单（`fcoffer`）

版本：v0.1（设计稿）
原则：CLI 与 MCP 共用同一服务层与返回信封；stdout 只输出数据，日志/进度全部走 stderr。

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
fcoffer config show [--redact]
fcoffer config check
```

- `init`：创建 schema、扩展（`vector`）、基础表、分区与 HNSW 索引；可重复执行（幂等）。
- `doctor`：逐项探测 PG 版本/扩展、MinIO bucket、Redis Stream、embedding 维度、MCP 协议帧。
- `config show`：密钥默认脱敏。

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

### 3.7 ODS

```bash
fcoffer ods list [--data-type financial|announcement|kline|risk|quote] [--symbol S] \
                 [--since T] [--until T] [--etl-status pending|processed|failed] [--limit 100]
fcoffer ods show <id> [--raw]
fcoffer ods stats [--group-by data_type|source|etl_status]
fcoffer ods replay <id>|--all-failed [--dry-run]
```

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
fcoffer dws build --symbol S|--all [--years 5] [--dry-run]
fcoffer dws show --symbol S
fcoffer dws rebuild [--since T] [--symbols ...]
```

宽表字段：`revenue_5y`、`net_profit_5y`、`roe_5y`、`pe`、`pb`、`risk_summary`、`latest_announcement_ids`、`quality_status`、`confidence`、`as_of_date`。

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
