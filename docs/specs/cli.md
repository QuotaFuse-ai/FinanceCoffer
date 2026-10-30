# FinanceCoffer CLI 命令清单（`fcoffer`）

版本：v0.13（设计稿）
原则：CLI 与 MCP 共用同一服务层与返回信封；stdout 只输出数据，日志/进度全部走 stderr。
变更记录：v0.2 新增第 6 节错误码表（含 `E_NOT_FOUND`、`E_RATE_LIMITED`），`config show` 明确密钥始终脱敏。
v0.3 实装 `ods` 四个子命令（`--etl-status` 取值改为 DDL 口径 `pending|done|failed`），明确 `--raw` 与 `replay` 语义。
v0.4 实装 `dws build` / `dws show` / `dws rebuild`，§3.9 字段表改为 DDL 口径（指标移入 `metrics_json`），补充确定性、部分失败与合规规则。
v0.5 `fc_etl_consume_lag_seconds` 的基准从 `event_time` 改为 `received_at`（排队等待时长），避免历史补录被算成巨大延迟。
v0.6 明确配置解析口径：所有命令按 **内置默认值 → 仓库 `.env` → 进程环境变量 → `--db-url`** 加载，`.env` 按当前工作目录查找、缺失时静默回退；`db migrate/status` 等经 `MigrationRunner` 的命令统一走该路径（此前会忽略本机 `.env` 并报 `FC_DB_URL is not configured`）。
v0.7 错误码表新增 `E_METHOD_NOT_ALLOWED`（REST 405）；未命中路由与其它框架级 4xx 统一走标准信封，`404` 复用 `E_NOT_FOUND`。
v0.8 变更记录删除内部任务编号；§3.6 的 `ingest event` 说明改为职能口径（公开仓库规则：`docs/` 不出现内部任务编号与团队称谓）。
v0.9 §3.3 写明 `serve api --workers N`（N>1）时的配置来源：fork 前把已解析配置导出为 `FC_*` 进程环境，worker 继承后与单 worker 一致；并记录凭据因此多出的落点。
v0.10 §3.11 `archive run` 新增可重复的 `--symbol` / `--table` 作用域（与时间、字节窗口取交集）；`archive run` / `archive restore` 非 `--dry-run` 时必须显式 `--yes`，否则退出码 2 并给出候选行数；`--dry-run` 保证无副作用；注明 `archive prune` 尚未实现。
v0.11 新增 §3.16 `minio` 命令组（`status|ensure|start|stop`）与 `FC_MINIO_AUTOSTART` 开关：默认 `false`，打开后 `serve api` 在 fork worker 前尽力确保本地存储可用，失败只告警不阻断；路径项无默认值、只在启动时报错；`start` 拒绝非回环端点。
v0.12 新增 §1.1 输出编码约定：CLI 入口强制 stdout/stderr 为 UTF-8，修复 Windows 重定向下 `--output json` 落盘 GBK 的问题；§3.9 补充指向 `docs/specs/dws-metrics.md` 的口径交叉引用。
v0.13 数值区间选项第二批（8 项）改为命令内判界并走 `E_INVALID_ARGUMENT` 信封：全局 `--timeout` / `--retries`、`vector embed --batch-size`、`etl consume --batch-size|--block-ms|--claim-idle-ms`、`etl replay --limit`、`db migrate --ahead`。其中 `--batch-size` 上限由 64 统一为客户端上限 32（33–64 从静默夹取改为参数错误）；`etl replay --limit` 保留 1000 条夹取；`db migrate --ahead` 的负值校验前移到迁移执行前。语义判定见 `docs/decisions/DEC-0030`。

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
| `--timeout` | `30` | 单次查询/操作超时（秒），`>= 1` |
| `--retries` | `2` | 可重试操作的次数，`>= 0` |
| `--db-url` | `.env` / 环境变量 | 临时覆盖 `FC_DB_URL`（优先级最高） |
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

### 1.1 输出编码约定

- **stdout 恒为 UTF-8**：CLI 入口在任何命令执行前把 `stdout` / `stderr` 重配为 UTF-8，与宿主代码页无关。
  Windows 下重定向（`fcoffer … --output json > out.json`）默认拿到的是本机 ANSI/OEM 代码页（简体中文为 `cp936`/GBK）；
  不重配则落盘字节不是 UTF-8，`jq` / Node / CI 一律解码失败——这是 `--output json` 在 Windows 管道场景失效的根因。
- **诊断走 stderr**：进度、告警、日志只写 stderr，stdout 只承载数据（信封或表格），保证 `--output json` 可被直接管道消费。
- **降级策略**：重配失败不阻断命令。`pythonw` 下 `sys.stdout is None`、被测试框架包装或自定义 writer 不支持 `reconfigure` 时，
  跳过重配继续执行；编码错误用 `errors="replace"`，宁可丢一个字符，也不让整个信封因 `UnicodeEncodeError` 崩溃。
  不使用 `backslashreplace`——它把中文转成 `\uXXXX` 转义序列，等于静默破坏可读输出。
- `PYTHONIOENCODING` 仍可显式覆盖；本约定只保证"未显式指定时也是 UTF-8"。
- 验证方式：把 `--output json` 的标准输出重定向到文件，文件内容必须能按 UTF-8 严格解码。

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
| 对象存储 | `minio status` / `minio ensure` / `minio start` / `minio stop` | 3.3、DEC-0014 | P1 |
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
- `serve api --workers N` 的配置来源：`N = 1` 时在主进程内构建 app，直接使用 CLI 已解析的配置；`N > 1` 时 uvicorn 只能按 import string 拉起 worker，每个 worker 会重新执行 `create_app()`，
  该入口默认只读**进程环境**、不读仓库 `.env`。为此 CLI 在 fork 前把**已解析的完整配置**（内置默认值 → 仓库 `.env` → 进程环境 → `--db-url` 的最终结果）
  导出为 `FC_*` 进程环境变量，worker 继承后与 `--workers 1` 的配置逐项一致，不依赖 worker 侧重新发现 `.env`。
- 上述导出的取舍：DSN（含密码）、MinIO AccessKey/SecretKey、MCP token 因此多一个落点——本 CLI 自己 fork 的 worker 进程环境。
  这与 `--db-url` 既有的暴露同级，不是日志或输出泄漏；`config show` / `config check` 的脱敏不受影响。`serve admin` 与 `start-all` 均在主进程内构建 app，不导出任何 `FC_*`。
- `serve api` 在构建 app 之前会按 `FC_MINIO_AUTOSTART` 做一次 §3.16 的 `ensure`：开关为 `false`（默认）时完全跳过；为 `true` 时尽力把本地存储带起来，失败只写一条 WARNING 并继续提供服务（归档按既有的「写失败记账」降级）。这一步在 fork worker 之前执行，多 worker 也只做一次。

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
- `etl consume` 的可选参数：`--batch-size` 取值 1–1000，`--block-ms` 取值 0–60000，`--claim-idle-ms` 取值 `>= 0`；越界与非整数直接报 `E_INVALID_ARGUMENT`。
- `etl replay --limit` 只拒绝 `< 1`；超过 1000 的部分按 1000 执行，实际条数以结果信封的 `count` 为准。
- 失败重试上限默认 3 次，超限进 DLQ 并计数 `fc_dirty_data_total`。
- 消费延迟 = `now - received_at`（最早一条排队事件的排队时长，取 `received` / `processing` 状态的最小 `received_at`），暴露为 `fc_etl_consume_lag_seconds`。不以 `event_time` 为基准：那是发布方的业务时间，历史补录会被算成巨大延迟。

### 3.6 手工接入与回填

```bash
fcoffer ingest event --file event.json [--validate-only]
fcoffer ingest backfill --source tickhub-cn|tickhub-us \
                        --from 2020-01-01 --to 2026-10-26 \
                        [--symbols 600000,000001] [--data-type all] [--dry-run]
fcoffer ingest webhook --host 127.0.0.1 --port 8101 [--token TOKEN]
```

- `ingest backfill` 只用于一次性迁移/对账，不作为运行时取数通道；执行时在 ODS 标记 `source=backfill`。
- `ingest event` 是上游事件格式的契约验证入口，供对照测试使用。
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
- `replay` 只把 `ops.etl_event_log` 中对应事件重置为 `received` 之后**不向 Redis 流重新发布**，需由上游重新投递、消费侧用 `--force` 处理；仍然**不修改 ODS 行**；
  `--all-failed` 选取的是「事件日志状态为 `failed` / `dlq`」的 ODS 行（ODS 的 `etl_status` 落库后不再回填，DEC-0021）；
  `--dry-run` 只打印 manifest（`id` / `event_id` / `data_type` / `symbol` / `source` / `fetch_time` / `etl_status`），零写入。

### 3.8 DWD

```bash
fcoffer dwd show --symbol S [--report-date D] [--report-type income|balance|cashflow] [--version N]
fcoffer dwd versions --symbol S --report-date D --report-type income
fcoffer dwd backfill --from-ods [--symbol S] [--since T] [--dry-run]
fcoffer dwd verify [--symbol S] [--report-date D]
```

- `dwd show` 默认返回 `MAX(version)`；`--version` 可查看历史快照，若同一个版本号在多张 statement 上都存在，按「金额非空多者优先、statement 升序」取一条，`available_versions` 与 `dwd versions` 均按 `(statement, version)` 列出。
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
- 口径全文（字段级映射、NULL/脏数据规则、幂等与对账 SQL）见 `docs/specs/dws-metrics.md`；本节与该文冲突时以该文为准。

### 3.10 向量

```bash
fcoffer vector chunk --symbol S|--all [--chunk-size 512] [--overlap 50] [--dry-run]
fcoffer vector embed [--pending] [--batch-size 32] [--model BAAI/bge-m3]
fcoffer vector index create|status|drop
fcoffer vector search --symbol S --query TEXT [--year-range 2023-2025] [--top-k 10]
fcoffer vector reindex [--symbol S]
fcoffer vector stats [--symbol S]
```

硬性约束：

- `search` 必须提供 `symbol`；缺省直接报参数错误，杜绝全表向量扫描。
- `top-k` 上限 50；`year_range` 上限 10 年。
- `--batch-size` 取值 1–32，与 embedding 客户端的单请求上限一致；超过 32 直接报参数错误，不再静默夹取。
- 切片：512 token，重叠 50 token；`embedding vector(1024)`（bge-m3）；HNSW `vector_cosine_ops`。

### 3.11 冷热分离与 MinIO

```bash
fcoffer archive run [--older-than-years 3] [--min-bytes 8192] [--dry-run] [--limit N] \
                    [--symbol S ...] [--table T ...] [--yes]
fcoffer archive status [--symbol S]
fcoffer archive restore --symbol S [--since T] [--until T] [--dry-run] [--yes]
fcoffer archive verify [--sample 100]
```

- `--symbol` 与 `--table` 可重复，和 `--older-than-years` / `--min-bytes` / `--limit` 取交集，把一次运行限制在部分标的或表上（共享仓库里多个数据 owner 各跑各的）。未知表名报 `E_INVALID_ARGUMENT`。
- **危险动作闸门**：`archive run` 和 `archive restore` 在非 `--dry-run` 时必须显式传 `--yes`。缺省时命令在触碰 PostgreSQL 或 MinIO 之前退出（退出码 2，`E_INVALID_ARGUMENT`），错误信息与 `details.candidates` 给出本次作用域内将被改动的行数；仓库不可达导致无法统计时仍拒绝执行，只是不带数量。
- `--dry-run` 无需确认，且绝不写库、绝不上传对象：`archive run --dry-run` 输出候选行数、字节数和逐行预览（默认最多 50 条，超出时 `candidates_detail_truncated=true`）；`archive restore --dry-run` 只下载并校验，返回 `would_restore`，`restored=0`。
- 转存成功后 PG 大字段置 `NULL`，`is_cold=true`，写入 `minio_url` 与 `content_hash`。
- 失败计数 `fc_missing_chunk_total`；支持断点重跑。
- `archive prune` 未实现：当前版本没有该子命令，清理/删除请求不要按此名调用。

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

`fcoffer health` 的检查项是 `db` / `minio` / `redis` / `etl`，`data` 形如：

```json
{
  "status": "degraded",
  "checks": [
    {"name": "db", "status": "ok", "reason": "PostgreSQL 16.4 with vector extension",
     "details": {"version": "16.4", "extensions": ["vector"], "database": "…"}},
    {"name": "etl", "status": "warn",
     "reason": "consumer group fc-etl is not attached to fc:stockdata:events; the ETL consumer does not appear to be running",
     "details": {"stream": "fc:stockdata:events", "group": "fc-etl"}}
  ],
  "failed": []
}
```

单项 `status` 词表为 `ok` / `warn` / `fail`（与 REST `/health` 的 `ok` / `degraded` / `unreachable` / `not_configured` **刻意不同，互不兼容**）；整体 `status` 为 `ok` / `degraded` / `fail`：任一 `fail` → `E_DEPENDENCY`（退出码 5），只有 `warn` → `degraded`（退出码 0）。CLI 是运维诊断工具，需要覆盖 `minio` 并用退出码把可选依赖异常带进脚本；REST `/health` 是编排探针，只对核心依赖降级。两边的检查项、词表、`etl` 语义（CLI 查 Redis Stream 消费组，REST 查 `ops` 表积压）的逐项对照见 `financecoffer.md` §6.2.1。

指标清单（本产品）：

| 指标 | 类型 | 告警 |
| --- | --- | --- |
| `fc_etl_consume_lag_seconds` | Gauge | > 60s |
| `fc_etl_events_total{status}` | Counter | failed 增长 |
| `fc_dirty_data_total{reason}` | Counter | 持续增长 |
| `fc_missing_chunk_total` | Counter | > 0 |
| `fc_vector_search_seconds` | Histogram | P95 超阈值 |
| `fc_archive_job_total{status}` | Counter | failed 增长 |

`/metrics` 只提供 Prometheus 文本格式，每个家族注册即输出（无数据为 0）；`--format json` 由 CLI 在客户端解析文本后输出，见 `financecoffer.md` §7 与 DEC-0022。

连接池水位指标 `fc_pg_pool_in_use` **不适用**：查询层用 psycopg 直连、无连接池可采样，本产品不暴露该家族（原因见 `financecoffer.md` §7）。

上游指标 `qf_stockdata_source_error_total`、`qf_stockdata_proxy_active_count` 由 TickHub 暴露；FinanceCoffer 不重复实现，只做抓取/告警联动（待确认部署方式）。

### 3.15 Skill

```bash
fcoffer skill install --target claude|cursor|codex [--path DIR] [--force]
```

### 3.16 本地对象存储进程（`minio`）

```bash
fcoffer minio status
fcoffer minio ensure [--wait 20]
fcoffer minio start  [--wait 20]
fcoffer minio stop   [--wait 5]
```

- **只服务于本地进程**：管理的是 §3.11 归档所用的那台本机 MinIO（DEC-0014）。远端 S3、容器内的存储由各自的运维手段负责，不走这组命令。
- `status` 只读：报告端点、控制台端点、`FC_MINIO_AUTOSTART` 取值、健康探测结果与 pid 记录（若有）。不配置任何路径也能正常返回。
- `ensure` 是启动钩子：开关为 `false` 时返回 `{"action":"skipped","reason":"FC_MINIO_AUTOSTART is not enabled"}` 并且**退出码 0**；已健康时返回 `{"action":"noop"}`；否则拉起并返回 `{"action":"started",...}`。
- `start` 无条件尝试拉起：`FC_MINIO_BINARY` / `FC_MINIO_DATA_DIR` / 两个凭据任一缺失时报 `E_CONFIG`（退出码 3）并在 `details.field` 点名缺哪个键；二进制不存在报 `E_DEPENDENCY`。
- `stop` 只结束 pid 文件里记录、且镜像名与记录一致的那个进程；没有 pid 文件时返回 `{"stopped":false,"reason":"no pid file"}`，pid 已消失时清掉陈旧记录并说明原因。
- `--wait` 是等待健康的最长秒数（1..300），超时后 `start` 会结束刚启动的进程并报 `E_DEPENDENCY`，不留半死的子进程。
- **端点必须是回环地址**（`127.0.0.1` / `localhost` / `::1`）：`start` 对 `0.0.0.0`、远端主机名或非本机 IP 报 `E_INVALID_ARGUMENT`（退出码 2）。控制台端点未配置时按 API 端口 +1 推导。
- pid 文件写在数据目录下的 `minio.pid`，记录 pid 与二进制名，供 `status` / `stop` 判断「这个进程是不是我们起的」。

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
| `E_NOT_FOUND` | 1 | 404 | 目标不存在或无数据（含未命中路由） |
| `E_METHOD_NOT_ALLOWED` | 1 | 405 | 路由存在但不支持该 HTTP 方法 |
| `E_RATE_LIMITED` | 1 | 429 | 请求超过限流阈值，响应带 `Retry-After` |

REST 限流默认开启：按客户端 IP 的进程内令牌桶，默认 120 次/分钟，仅作用于 `/v1/*`，`/health` 与 `/metrics` 不限流。`FC_RATE_LIMIT_ENABLED=false` 关闭；`FC_RATE_LIMIT_PER_MINUTE` 调整阈值（1–10000）。
