# FinanceCoffer MCP 工具参考

- 对齐基线：MCP 实现 `5c4c141`（T-011）。工具目录以 `fcoffer mcp tools --json` 的实际输出为准。
- 覆盖工具：`query_structured_data`、`search_announcements_semantically`、`get_risk_history`。
- 只读：三个工具都只查询仓库，不写入、不触发采集或重算。

## 1. 传输与工具目录

```bash
fcoffer mcp stdio
fcoffer mcp http --host 127.0.0.1 --port 8100 [--token TOKEN] [--allowed-hosts h]
fcoffer mcp tools [--json]
```

- stdio：`--db-url` 等全局参数必须写在 `mcp stdio` **之前**；stdout 只承载 JSON-RPC 帧，日志走 stderr。
- HTTP：Streamable HTTP，路径固定 `/mcp`；非回环地址必须同时配置 Bearer token 与 Host 白名单，否则启动即 `E_CONFIG`（退出码 3）。
- `mcp tools --json` 不需要数据库，也不依赖 `mcp` SDK。

工具目录（参数名逐字固定）：

| 工具 | 必填参数 | 可选参数 |
| --- | --- | --- |
| `query_structured_data` | `symbol` | `metric`、`year_range` |
| `search_announcements_semantically` | `symbol`、`query_text` | `year_range` |
| `get_risk_history` | `symbol` | — |

目录中的 `inputSchema` 声明 `additionalProperties=false`，但 MCP SDK 从函数签名推导实时 schema 时会静默忽略未知参数。**调用方仍应只传上表列出的参数**，不要依赖服务端拒绝未知字段。

## 2. 统一信封

### 2.1 成功

```json
{
  "ok": true,
  "data": {},
  "meta": {
    "source": "dws",
    "fetched_at": "2026-10-27T06:47:18Z",
    "quality_status": null,
    "confidence": null,
    "compliance_tier": null,
    "trace_id": "92068a40d6024aa495354c16223bac77"
  },
  "error": null
}
```

顶层字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | boolean | 成功恒为 `true` |
| `data` | object | 各工具的业务负载，见第 3–5 节 |
| `meta` | object | 六个字段固定存在，见 2.3 |
| `error` | null | 成功时恒为 `null` |

### 2.2 失败

工具执行失败时，MCP 结果是 `isError=true`，`content[0].text` 是一个**扁平**错误对象：

```json
{
  "code": "E_NOT_FOUND",
  "message": "no pre-computed wide row for this symbol",
  "details": {"symbol": "600519", "hint": "run `fcoffer dws build`"},
  "trace_id": "5d1f0c2a9b7e4f1aa8c3d6e2b4f70911"
}
```

- 失败负载**没有** `ok` / `data` / `meta` / `error` 外壳；不要按成功信封解析。
- `trace_id` 在顶层，与同一次调用的审计记录一致。
- 传输层鉴权失败（HTTP 401）返回的是**完整失败信封**（`ok=false`、`data=null`、`meta`、`error.code=E_AUTH`），不是上面的扁平对象。
- 启动期配置错误（`E_CONFIG`）由 CLI 输出完整失败信封并以退出码 3 结束，不会进入工具调用。

### 2.3 `meta` 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `source` | string | `dws`（结构化）/ `dwd`（风险）/ `vector`（公告）；`fcoffer mcp tools --json` 自身为 `mcp` |
| `fetched_at` | string | 响应生成时间，UTC、秒精度、`Z` 结尾，例如 `2026-10-27T06:47:18Z`。**不是**数据截止日 |
| `quality_status` | string \| null | `normal` / `suspicious` / `bad`；无记录或未标注时为 `null`。取首条返回记录的上游标记 |
| `confidence` | string \| null | `high` / `medium` / `low`；无记录或未标注时为 `null`。取首条返回记录 |
| `compliance_tier` | string \| null | `S` / `B` / `C` / `D` / `X`；`FC_COMPLIANCE_ENABLED` 关闭或未记录时为 `null` |
| `trace_id` | string | 32 位十六进制，每次调用新生成；同时写入 `financecoffer.audit` 日志 |

补充说明：

- `meta.compliance_tier` 只代表首条记录。逐条等级在 `data.compliance`（结构化）、`results[].compliance`（公告）、`events[].compliance`（风险），仅在开关开启时出现。
- `query_structured_data` 的 `meta.quality_status` / `meta.confidence` 目前**恒为 `null`**：DWS 宽表没有顶层质量列，读取层也没有投影逐年的 `quality_status`（T-008 Q-10 待确认）。不要据此声称「质量正常」。
- 审计日志只记录元数据，不记录 `query_text` 原文。

## 3. `query_structured_data`

读取某只标的的 DWS 宽表；可选只投影一个指标、限定财年窗口。

### 3.1 入参

| 参数 | 必填 | 类型 | 规则 |
| --- | --- | --- | --- |
| `symbol` | 是 | string | A 股 6 位数字，或美股 ticker（`[A-Z][A-Z0-9.\-]{0,9}`）；服务端去空格并转大写 |
| `metric` | 否 | string \| null | 枚举：`revenue` / `net_profit` / `roe` / `pe` / `pb`；省略返回全部指标 |
| `year_range` | 否 | string \| null | `YYYY-YYYY`，两端都含，跨度最多 10 年；省略返回宽表内全部年份 |

校验规则（全部在服务层执行，MCP 层不重复校验、不静默截断）：

| 场景 | 结果 |
| --- | --- |
| `symbol` 缺失、空串或格式不符 | `E_INVALID_ARGUMENT`，`details.field="symbol"` |
| `metric` 不在枚举内 | `E_INVALID_ARGUMENT`，`details.field="metric"` |
| `year_range` 不是 `YYYY-YYYY`、结束早于开始或跨度超过 10 年 | `E_INVALID_ARGUMENT`，`details.field="year_range"` |

### 3.2 出参 `data`

```json
{
  "symbol": "600519",
  "as_of": "2026-10-26T00:00:00Z",
  "built_at": "2026-10-26T18:03:11Z",
  "fiscal_years": 5,
  "source_version": "dws-1",
  "metric": "revenue",
  "year_range": [2023, 2025],
  "years": [
    {"year": "2023", "revenue": "150560000000.0000", "net_profit": "74734000000.0000", "roe": null, "pe": null, "pb": null},
    {"year": "2024", "revenue": "170899000000.0000", "net_profit": "86228000000.0000", "roe": null, "pe": null, "pb": null}
  ],
  "series": [
    {"year": "2023", "value": "150560000000.0000"},
    {"year": "2024", "value": "170899000000.0000"}
  ],
  "risk_summary": {
    "window": {"since": "2023-10-26", "until": "2026-10-26"},
    "as_of": "2026-10-26",
    "counts": {"critical": 0, "high": 1, "medium": 0, "low": 2, "total": 3},
    "highest_severity": "high",
    "latest_occur_date": "2026-08-30",
    "highlights": [],
    "truncated": false,
    "text": "近 3 年共 3 条风险事件……"
  },
  "latest_announcements": [
    {
      "id": 12345,
      "ann_date": "2026-10-24",
      "ann_type": "定期报告",
      "title": "2026 年第三季度报告",
      "minio_url": "s3://financecoffer/ann/2026/…",
      "quality_status": "normal",
      "confidence": "high"
    }
  ]
}
```

（数值为示意，实际以库内数据为准。）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `symbol` | string | 归一化后的代码 |
| `as_of` | string | 宽表构建截止日，UTC 00:00 |
| `built_at` | string | 宽表落库时间；不参与确定性比较 |
| `fiscal_years` | integer | 构建时选的年数，1–10，默认 5 |
| `source_version` | string \| null | 指标口径版本，当前为 `dws-1` |
| `metric` | string \| null | 回显本次投影的指标；未传时为 `null` |
| `year_range` | `[integer, integer]` \| null | 回显本次窗口；未传时为 `null` |
| `years` | array | 按年份**升序**；元素见 3.3 |
| `series` | array | 仅当传入 `metric` 时出现，与 `years` 一一对应：`{year, value}` |
| `risk_summary` | object \| null | 宽表预先算好的近 3 年风险摘要；未构建该列时为 `null` |
| `latest_announcements` | array \| null | 最多 20 条，按 `ann_date DESC, id DESC`；未构建时为 `null` |
| `compliance` | object \| null | 仅 `FC_COMPLIANCE_ENABLED=true` 时出现：`{compliance_tier, commercial_use}` |

### 3.3 `years[]` 元素

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `year` | string | 财年，例如 `"2024"` |
| `revenue` | string \| null | 营业收入，4 位小数、单位元；缺失为 `null` |
| `net_profit` | string \| null | 净利润，4 位小数、单位元；缺失为 `null` |
| `roe` | number \| null | 小数（`0.0831` = 8.31%）；当前恒为 `null` |
| `pe` | number \| null | 倍数；可能为 `null` |
| `pb` | number \| null | 倍数；当前恒为 `null` |

读取层只投影 `year` 与五个指标键；宽表 `metrics_json` 中的 `data_status`、`roe_reason` / `pe_reason` / `pb_reason`、`report_date` / `report_type` / `quality_status` 等补充键**不会出现在本工具的 `years[]` 中**。因此：

- `null` 就是「该年该指标没有数据」，不要补 0、不要插值。
- ROE / PB 为 `null` 的原因是缺少净资产 / 总股本数据源（宽表原因码分别为 `equity_unavailable` / `no_equity`，T-008 Q-02 / Q-03 待确认）。
- 逐年质量标记也不在本工具的返回里；不要声称结构化结果是 `normal`。

### 3.4 错误

| 错误码 | 触发条件 | `details` 示例 |
| --- | --- | --- |
| `E_INVALID_ARGUMENT` | `symbol` / `metric` / `year_range` 校验失败 | `{"field": "year_range", "value": "2010-2025"}` |
| `E_NOT_FOUND` | 该标的没有宽表行（或宽表未构建） | `{"symbol": "600519", "hint": "run `fcoffer dws build`"}` |
| `E_STORAGE` | PostgreSQL 不可用 | 服务端决定 |
| `E_INTERNAL` | 未预期错误 | `null` |

## 4. `search_announcements_semantically`

在**单只标的**的公告切片里做语义检索：先按 `symbol` 与财年过滤，再在候选集内做向量排序。

### 4.1 入参

| 参数 | 必填 | 类型 | 规则 |
| --- | --- | --- | --- |
| `symbol` | 是 | string | 同上；必填，禁止全库搜索 |
| `query_text` | 是 | string | 非空，最多 2000 字符；不做截断 |
| `year_range` | 否 | string \| null | `YYYY-YYYY`，最多 10 年；省略时默认最近 5 个自然年（当年 − 4 至当年） |

- 工具**不暴露** `top_k`；服务端固定取 10。服务层 `top_k` 上限为 50，该上限适用于 CLI `fcoffer search announcements --top-k` 与 REST 等价入口。
- 如需更多结果，收窄问题或拆成多次检索；单次调用最多返回 10 条。

### 4.2 出参 `data`

```json
{
  "symbol": "600519",
  "query": "商誉减值",
  "year_range": [2023, 2025],
  "top_k": 10,
  "count": 2,
  "results": [
    {
      "id": 987,
      "symbol": "600519",
      "ann_date": "2024-03-29",
      "ann_type": "定期报告",
      "chunk_index": 3,
      "chunk_text": "……计提商誉减值准备……",
      "token_count": 480,
      "embedding_model": "BAAI/bge-m3",
      "dwd_ref_id": 12345,
      "title": "2023 年年度报告",
      "minio_url": "s3://financecoffer/ann/2024/…",
      "quality_status": "normal",
      "confidence": "high",
      "score": 0.831742
    }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `symbol` | string | 归一化后的代码 |
| `query` | string | 回显检索问题（原文），**不写审计日志** |
| `year_range` | `[integer, integer]` | 实际生效窗口；省略入参时也会回填默认窗口 |
| `top_k` | integer | 固定 `10` |
| `count` | integer | `results` 条数；`0` 表示没有命中，不是错误 |
| `results` | array | 命中切片，按相似度降序 |

`results[]` 元素：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | integer | 向量切片主键 |
| `symbol` | string | 标的代码 |
| `ann_date` | string | 公告日期 `YYYY-MM-DD` |
| `ann_type` | string \| null | 公告类型 |
| `chunk_index` | integer | 该公告内的切片序号 |
| `chunk_text` | string | 命中的原文切片 |
| `token_count` | integer \| null | 切片 token 数 |
| `embedding_model` | string \| null | 生成向量的模型 |
| `dwd_ref_id` | integer \| null | 关联 `dwd.stock_announcement.id` |
| `title` | string \| null | 公告标题 |
| `minio_url` | string \| null | 冷数据归档地址 |
| `quality_status` | string \| null | `normal` / `suspicious` / `bad` |
| `confidence` | string \| null | `high` / `medium` / `low` |
| `score` | number \| null | `1 − 余弦距离`，保留 6 位小数；越高越相似，**不是概率** |
| `compliance` | object \| null | 仅开关开启时出现 |

空结果是成功：`ok=true`、`count=0`、`results=[]`。回答「未检索到相关公告」，同时说明这只代表库内没有命中。

### 4.3 错误

| 错误码 | 触发条件 | `details` 示例 |
| --- | --- | --- |
| `E_INVALID_ARGUMENT` | `symbol` 非法；`query_text` 为空或超过 2000 字符；`year_range` 非法 | `{"field": "query_text", "length": 2001}` |
| `E_DEPENDENCY` | embedding 服务不可用、返回非法 JSON、维度不符或零向量 | `{"url": "http://127.0.0.1:8080/v1/embeddings", "error": "…"}` |
| `E_STORAGE` | PostgreSQL 不可用 | 服务端决定 |
| `E_INTERNAL` | 未预期错误 | `null` |

## 5. `get_risk_history`

读取某只标的的历史风险事件（DWD）。

### 5.1 入参

| 参数 | 必填 | 类型 | 规则 |
| --- | --- | --- | --- |
| `symbol` | 是 | string | 同上 |

- 工具**不暴露** `since` 与 `limit`。服务端固定按 `occur_date DESC, id DESC` 取**最近 50 条**；更早的事件无法通过该工具取得。
- 如果用户需要更早的记录，说明工具的 50 条上限，并请其缩小问题范围（当前 MCP 没有按日期过滤的参数）。

### 5.2 出参 `data`

```json
{
  "symbol": "600519",
  "since": null,
  "count": 1,
  "events": [
    {
      "id": 42,
      "symbol": "600519",
      "occur_date": "2025-04-25",
      "risk_type": "goodwill_impairment",
      "severity": "high",
      "summary": "计提商誉减值准备……",
      "source_announcement_ref": "dwd:stock_announcement:12345",
      "quality_status": "normal",
      "confidence": "high",
      "fetch_time": "2025-04-26T01:20:00Z",
      "event_id": "evt_5f1c…",
      "provider": "tickhub-cn"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `symbol` | string | 归一化后的代码 |
| `since` | null | MCP 工具不传 `since`，恒为 `null` |
| `count` | integer | `events` 条数；`0` 表示库内无记录，不是错误 |
| `events` | array | 按 `occur_date DESC, id DESC`，最多 50 条 |

`events[]` 元素：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | integer | 风险事件行 ID |
| `symbol` | string | 标的代码 |
| `occur_date` | string | 事件发生日期 `YYYY-MM-DD` |
| `risk_type` | string | 风险类型 |
| `severity` | string | `low` / `medium` / `high` / `critical` |
| `summary` | string \| null | 事件摘要 |
| `source_announcement_ref` | string \| null | 来源公告引用（如 `dwd:stock_announcement:12345`） |
| `quality_status` | string | `normal` / `suspicious` / `bad` |
| `confidence` | string \| null | `high` / `medium` / `low` |
| `fetch_time` | string | 数据抓取时间 |
| `event_id` | string | 上游事件唯一 ID |
| `provider` | string | 上游提供方 |
| `compliance` | object \| null | 仅开关开启时出现 |

空结果是成功：`ok=true`、`count=0`、`events=[]`。回答「库内没有查到风险事件记录」，不要说成「公司没有风险」。

### 5.3 错误

| 错误码 | 触发条件 | `details` 示例 |
| --- | --- | --- |
| `E_INVALID_ARGUMENT` | `symbol` 非法 | `{"field": "symbol"}` |
| `E_STORAGE` | PostgreSQL 不可用 | 服务端决定 |
| `E_INTERNAL` | 未预期错误 | `null` |

`get_risk_history` **不会**返回 `E_NOT_FOUND`；无记录是成功空结果。

## 6. 错误码汇总

| 错误码 | 可由哪些工具返回 | 含义 |
| --- | --- | --- |
| `E_INVALID_ARGUMENT` | 三个工具 | 参数校验失败 |
| `E_NOT_FOUND` | 仅 `query_structured_data` | 该代码没有宽表行 |
| `E_DEPENDENCY` | 仅 `search_announcements_semantically` | embedding 依赖不可用 |
| `E_STORAGE` | 三个工具 | PostgreSQL / MinIO 等存储不可用 |
| `E_INTERNAL` | 三个工具 | 未预期错误（固定文案，不泄漏堆栈） |
| `E_AUTH` | HTTP 传输 | Bearer token 缺失或错误（401） |
| `E_CONFIG` | 启动期 | 非回环监听缺少 token / Host 白名单（退出码 3） |
| `E_RATE_LIMITED` | REST `/v1/*` 或前置网关 | MCP 传输本身不限流；按 `Retry-After` 退避 |

错误码是 CLI / REST / MCP 共用契约（`docs/specs/cli.md` 第 5 节）。未知错误码按 `E_INTERNAL` 处理。

## 7. 读取层现状（与规格书的差异）

以下不是调用方式问题，但会影响你读到的字段，写回答时要注意：

1. **`years[]` 只含 `year` + 五个指标**：`data_status`、`*_reason`、`report_date`、逐年 `quality_status` 留在宽表 JSONB 里，读取层不返回。缺失值只看 `null`。
2. **结构化结果的 `meta.quality_status` / `meta.confidence` 恒为 `null`**（宽表无顶层质量列，T-008 Q-10 待确认）。
3. **ROE / PB 当前恒为 `null`**：无净资产 / 总股本数据源，原因码为 `equity_unavailable` / `no_equity`（Q-02 / Q-03 待确认）。
4. **公告返回键是 `results`**（不是 `hits`）；`count = len(results)`。
5. **`search_announcements_semantically` 没有 `top_k` 参数**，固定 10 条；`top_k ≤ 50` 是服务层 / CLI / REST 的上限。
6. **工具错误是扁平对象** `{code, message, details, trace_id}`，不是成功信封。
7. **MCP 不限流**：`E_RATE_LIMITED` 只可能来自 REST 或网关。
8. **合规默认关闭**：`compliance_tier=null` 表示「未记录」，不能推断为可商用（DEC-0010）。
