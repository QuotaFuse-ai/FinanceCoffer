# FinanceCoffer Skill 对话示例

三个完整示例：正常查询、空结果、参数超限后的报错恢复。工具名、参数名、返回字段均与 `5c4c141` 的 MCP 实现和 `references/tools.md` 一致。

> 示例中的数值、`trace_id`、`fetched_at` 均为示意，实际以工具返回为准。示例只演示「怎么调用、怎么读、怎么回答」，不代表任何真实标的的投资价值。

---

## 示例 1：正常查询（结构化指标 + 公告语义检索）

**用户：** 600519 在 2023–2025 年的营收是多少？顺便看看 ROE。

**Agent 调用：**

```text
query_structured_data(symbol="600519", year_range="2023-2025")
```

**工具返回（节选）：**

```json
{
  "ok": true,
  "data": {
    "symbol": "600519",
    "as_of": "2026-10-26T00:00:00Z",
    "built_at": "2026-10-26T18:03:11Z",
    "fiscal_years": 5,
    "source_version": "dws-1",
    "metric": null,
    "year_range": [2023, 2025],
    "years": [
      {"year": "2023", "revenue": "150560000000.0000", "net_profit": "74734000000.0000", "roe": null, "pe": null, "pb": null},
      {"year": "2024", "revenue": "170899000000.0000", "net_profit": "86228000000.0000", "roe": null, "pe": null, "pb": null},
      {"year": "2025", "revenue": null, "net_profit": null, "roe": null, "pe": null, "pb": null}
    ],
    "risk_summary": null,
    "latest_announcements": []
  },
  "meta": {
    "source": "dws",
    "fetched_at": "2026-10-27T02:11:05Z",
    "quality_status": null,
    "confidence": null,
    "compliance_tier": null,
    "trace_id": "a1b2c3d4e5f6478899aabbccddeeff00"
  },
  "error": null
}
```

**Agent 回答：**

> 600519 的营业收入（单位：元）：
>
> - 2023：150,560,000,000.0000（约 1,505.60 亿元）
> - 2024：170,899,000,000.0000（约 1,708.99 亿元）
> - 2025：**暂无数据**（宽表未收录，返回 `null`）
>
> ROE 三个年度均为 `null`：库内没有净资产（equity）数据源，无法计算（原因码 `equity_unavailable`，口径仍在确认中）。**没有数据就是没有，不能估算。**
>
> 数据来源：DWS 宽表，截止日 `as_of=2026-10-26`，版本 `dws-1`。

**同一对话继续——用户：** 那 2023 年以来有没有商誉减值相关公告？

**Agent 调用：**

```text
search_announcements_semantically(
  symbol="600519",
  query_text="商誉减值",
  year_range="2023-2025"
)
```

**工具返回（节选）：**

```json
{
  "ok": true,
  "data": {
    "symbol": "600519",
    "query": "商誉减值",
    "year_range": [2023, 2025],
    "top_k": 10,
    "count": 1,
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
  },
  "meta": {
    "source": "vector",
    "fetched_at": "2026-10-27T02:12:40Z",
    "quality_status": "normal",
    "confidence": "high",
    "compliance_tier": null,
    "trace_id": "b2c3d4e5f6a7488990aabbccddeeff01"
  },
  "error": null
}
```

**Agent 回答：**

> 检索到 1 条相关公告：
>
> - 2024-03-29《2023 年年度报告》中出现「计提商誉减值准备」表述（切片 3，相似度 0.83，数据质量 `normal`，置信度 `high`）。
> - 原文：`……计提商誉减值准备……`（归档：`s3://financecoffer/ann/2024/…`）
>
> 这是语义检索命中，代表公告中有相关表述；具体金额以公告原文为准，本次没有返回金额。

**要点：**

- 一次调用拿全指标；`metric` 省略时返回五项指标，`series` 不出现。
- `2025` 为 `null` 就是说「没有数据」，不写 0、不插值。
- ROE 恒为 `null` 是数据源缺失，不是查询失败；原因码来自宽表契约，当前工具不返回该键。
- `meta.fetched_at` 是响应时间，不是数据截止日；数据截止看 `data.as_of`。
- 语义检索结果保留 `ann_date`、`title`、`quality_status`、`score`（相似度，不是概率）。

---

## 示例 2：空结果（查到「没有」，但别答成「不存在」）

**用户：** AAPL 历史上有什么风险事件？

**Agent 调用：**

```text
get_risk_history(symbol="AAPL")
```

**工具返回：**

```json
{
  "ok": true,
  "data": {
    "symbol": "AAPL",
    "since": null,
    "count": 0,
    "events": []
  },
  "meta": {
    "source": "dwd",
    "fetched_at": "2026-10-27T02:20:13Z",
    "quality_status": null,
    "confidence": null,
    "compliance_tier": null,
    "trace_id": "c3d4e5f6a7b8489901aabbccddeeff02"
  },
  "error": null
}
```

**Agent 回答：**

> FinanceCoffer 的历史风险事件库中，**没有查到 AAPL 的记录**（`count=0`）。
>
> 这只代表本库尚未收录该标的的风险事件，**不等于该公司没有风险**。风险事件来自上游采集与质检流程，覆盖范围以库内记录为准。

**用户追问：** 那你能确定它没有违规吗？

**Agent 回答：**

> 不能。空结果只说明库内没有对应记录，不能据此推断「没有违规」或「风险为零」。如需结论，需要查监管披露原文或其他数据源。

**要点：**

- `get_risk_history` 的空结果是**成功**（`ok=true`、`count=0`），不是 `E_NOT_FOUND`。
- 措辞必须是「库内没有查到记录」，不能说「不存在」「没有风险」「风险为零」。
- 不因为空结果就换代码、扩大范围或调用其他工具「凑」数据。
- `meta.quality_status=null` 表示无记录/未标注，不要写成「质量正常」。

---

## 示例 3：报错恢复（`year_range` 超限后拆分请求）

**用户：** 把 600519 从 2015 到 2025 年每年的营收列一下。

**Agent 判断：** 2015–2025 共 11 年，超过 10 年上限，先按上限拆成 `2015-2024` 与 `2025-2025` 两个窗口，而不是直接发一个超限请求。为演示错误路径，下面先展示服务端会怎么拒绝。

**（错误示范）Agent 调用：**

```text
query_structured_data(symbol="600519", metric="revenue", year_range="2015-2025")
```

**工具返回（`isError=true`，扁平错误对象）：**

```json
{
  "code": "E_INVALID_ARGUMENT",
  "message": "year_range must span at most 10 years",
  "details": {"field": "year_range", "value": "2015-2025"},
  "trace_id": "d4e5f6a7b8c9499002aabbccddeeff03"
}
```

**Agent 处理：** 原样重试没有意义；按 `details.field=year_range` 拆分。

**（正确做法）Agent 调用 1：**

```text
query_structured_data(symbol="600519", metric="revenue", year_range="2015-2024")
```

**工具返回（节选，10 年，按年升序；为便于阅读 `years[]` 只保留 `revenue` 键，实际每条含五项指标）：**

```json
{
  "ok": true,
  "data": {
    "symbol": "600519",
    "metric": "revenue",
    "year_range": [2015, 2024],
    "years": [
      {"year": "2015", "revenue": "32660000000.0000"},
      {"year": "2016", "revenue": "38862000000.0000"},
      {"year": "2017", "revenue": "58218000000.0000"},
      {"year": "2018", "revenue": "73639000000.0000"},
      {"year": "2019", "revenue": "85430000000.0000"},
      {"year": "2020", "revenue": "94915000000.0000"},
      {"year": "2021", "revenue": "106190000000.0000"},
      {"year": "2022", "revenue": "124100000000.0000"},
      {"year": "2023", "revenue": "150560000000.0000"},
      {"year": "2024", "revenue": "170899000000.0000"}
    ],
    "series": [
      {"year": "2015", "value": "32660000000.0000"},
      {"year": "2016", "value": "38862000000.0000"},
      {"year": "2017", "value": "58218000000.0000"},
      {"year": "2018", "value": "73639000000.0000"},
      {"year": "2019", "value": "85430000000.0000"},
      {"year": "2020", "value": "94915000000.0000"},
      {"year": "2021", "value": "106190000000.0000"},
      {"year": "2022", "value": "124100000000.0000"},
      {"year": "2023", "value": "150560000000.0000"},
      {"year": "2024", "value": "170899000000.0000"}
    ]
  },
  "meta": {
    "source": "dws",
    "fetched_at": "2026-10-27T02:31:44Z",
    "quality_status": null,
    "confidence": null,
    "compliance_tier": null,
    "trace_id": "e5f6a7b8c9da499103aabbccddeeff04"
  },
  "error": null
}
```

**Agent 调用 2：**

```text
query_structured_data(symbol="600519", metric="revenue", year_range="2025-2025")
```

**工具返回（节选）：**

```json
{
  "ok": true,
  "data": {
    "symbol": "600519",
    "metric": "revenue",
    "year_range": [2025, 2025],
    "years": [
      {"year": "2025", "revenue": null}
    ],
    "series": [
      {"year": "2025", "value": null}
    ]
  },
  "meta": {
    "source": "dws",
    "fetched_at": "2026-10-27T02:32:02Z",
    "quality_status": null,
    "confidence": null,
    "compliance_tier": null,
    "trace_id": "f6a7b8c9daeb499204aabbccddeeff05"
  },
  "error": null
}
```

**Agent 回答：**

> 说明：2015–2025 共 11 年，超过单次 10 年上限，已拆成 `2015-2024` 与 `2025-2025` 两次查询并按年份合并。
>
> 600519 营业收入（单位：元）：
>
> | 年份 | 营业收入 |
> | --- | --- |
> | 2015 | 32,660,000,000.0000 |
> | 2016 | 38,862,000,000.0000 |
> | 2017 | 58,218,000,000.0000 |
> | 2018 | 73,639,000,000.0000 |
> | 2019 | 85,430,000,000.0000 |
> | 2020 | 94,915,000,000.0000 |
> | 2021 | 106,190,000,000.0000 |
> | 2022 | 124,100,000,000.0000 |
> | 2023 | 150,560,000,000.0000 |
> | 2024 | 170,899,000,000.0000 |
> | 2025 | **暂无数据**（返回 `null`，宽表未收录） |
>
> 数据来源：DWS 宽表，窗口截止 `2026-10-26`，版本 `dws-1`。

**要点：**

- 超限参数服务端**直接拒绝**（`E_INVALID_ARGUMENT` + `details.field`），不会静默截断，也不会自动返回前 10 年。
- 不要原样重试；按字段拆分窗口，各自不超过 10 年，再按年份合并。
- 拆分后每个窗口独立成功/失败：某一窗失败只需说明该窗缺失，不要用另一窗数据推算。
- `2025` 为 `null` 就是没有数据，如实写「暂无数据」，不补 0、不插值。

**同类处理——`query_text` 超过 2000 字符：**

```json
{
  "code": "E_INVALID_ARGUMENT",
  "message": "query_text must be at most 2000 characters",
  "details": {"field": "query_text", "length": 2431},
  "trace_id": "a7b8c9daebfc499305aabbccddeeff06"
}
```

处理方式：把问题压缩成核心检索词（例如保留主体、时间范围、事件类型），或拆成多次检索；不做截断后原样重发。`query_text` 原文不会写入审计日志。

---

## 三个示例的共同底线

1. 回答里的每个数字都能在工具返回中找到原值；没有的一律写「暂无数据 / 未检索到」。
2. 空结果是成功，措辞限定在「库内记录」，不外推成事实否定。
3. 超限先拆再查，不重试、不截断、不换参数绕过。
4. 始终带出处（`as_of` / `ann_date` / `occur_date`），`quality_status` 为 `suspicious` / `bad` 时显式提示。
