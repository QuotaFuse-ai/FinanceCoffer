---
name: financecoffer
description: 查询 FinanceCoffer 历史金融事实库：结构化财报指标、公告语义检索、历史风险事件。用于历史长周期数据、模糊语义搜索和风险回溯；不含实时行情、预测或数据采集。
---

# FinanceCoffer Skill

FinanceCoffer 是**只读的历史事实库**：只返回库中已有的数据，不推算、不补值、不预测。三个 MCP 工具都要求先给出 `symbol`。

## 何时使用

- 用户询问**历史**财报指标（营收、净利润、ROE、PE、PB），尤其是跨季度、跨年的序列。
- 用户做**模糊语义**检索，例如「找出所有关于商誉减值的公告」。
- 用户要求**历史风险回溯**（监管处罚、商誉减值、诉讼、退市风险等）。

## 何时不使用

- 今日 / 盘中实时行情 → 使用 TickHub-CN / TickHub-US。
- 数据抓取、补抓、重算勾稽 → 不在 FinanceCoffer 职责内。
- 预测、评级、研报观点、投资建议 → 本库只存历史事实。
- 没有 `symbol` 的全市场筛选或行业搜索 → 工具不支持，先向用户确认标的。

## 硬约束（违反即答错）

1. **必须先有 `symbol`，禁止全库搜索。** 用户只给公司名时先问代码，不许猜、不许拿相近代码代替。三个工具都要求 `symbol`；服务端对缺失或非法代码返回 `E_INVALID_ARGUMENT`。
2. **严禁脑补数字。** 返回 `null`、空数组或 `E_NOT_FOUND` 都是有效答案：「没有查到」。不得用记忆、估算、同业均值、同比推算或「大概」填补；不得把缺失年份写成 0 或插值。
3. **超限就拆分请求，不要硬试。** 服务端对超限参数直接返回 `E_INVALID_ARGUMENT`，不会静默截断。

## 工具与参数

工具名和参数名逐字固定，不要改名、不要加参数：

| 工具 | 必填 | 可选 | 用途 |
| --- | --- | --- | --- |
| `query_structured_data(symbol, metric, year_range)` | `symbol` | `metric`、`year_range` | 取 DWS 结构化指标 |
| `search_announcements_semantically(symbol, query_text, year_range)` | `symbol`、`query_text` | `year_range` | 在单只标的的公告里做语义检索 |
| `get_risk_history(symbol)` | `symbol` | — | 取历史风险事件 |

- `symbol`：A 股 6 位数字（如 `600519`）或美股 ticker（如 `AAPL`），服务端会去空格并转大写。
- `metric`：`revenue` / `net_profit` / `roe` / `pe` / `pb`；省略则返回全部指标。
- `year_range`：`YYYY-YYYY`，两端都含；**最多 10 年**。
  - 省略时：`query_structured_data` 返回宽表内全部年份；`search_announcements_semantically` 默认最近 5 个自然年。
- `query_text`：自然语言，**最多 2000 字符**。
- `top_k`：服务层上限 **50**。MCP 工具不暴露该参数，`search_announcements_semantically` 每次固定返回最多 10 条；50 的上限适用于 CLI / REST 等价入口。

| 参数 | 上限 | 超限行为 | Agent 该怎么做 |
| --- | --- | --- | --- |
| `year_range` | 10 年 | `E_INVALID_ARGUMENT` | 拆成多个不超过 10 年的窗口分别查询，再按年份合并 |
| `query_text` | 2000 字符 | `E_INVALID_ARGUMENT` | 缩短问题，或拆成多次检索 |
| `top_k` | 50 | `E_INVALID_ARGUMENT` | 收窄检索范围或分多次请求；MCP 单次最多 10 条 |

## 结果怎么读

### 成功信封

```json
{"ok": true, "data": {"...": "..."}, "meta": {"...": "..."}, "error": null}
```

`meta` 六个字段：

| 字段 | 含义 |
| --- | --- |
| `source` | 数据来源：`dws`（结构化）/ `dwd`（风险）/ `vector`（公告） |
| `fetched_at` | 本次响应生成时间（UTC）；**不是**数据截止日，数据日期看 `data.as_of` 或事件日期 |
| `quality_status` | `normal` / `suspicious` / `bad` / `null`（未标注或无记录） |
| `confidence` | `high` / `medium` / `low` / `null`（未标注） |
| `compliance_tier` | `S` / `B` / `C` / `D` / `X` / `null`（开关关闭或未记录） |
| `trace_id` | 本次调用唯一 ID，也写入审计日志；用户报障时请提供 |

完整字段与每个 `data` 的形状见 `references/tools.md`。

### 失败

工具执行失败时 MCP 返回 `isError=true`，内容为 `{code, message, details, trace_id}`（**没有** `ok` / `data` / `meta`）。不要继续读 `data`，按 `code` 处理。

| 错误码 | 含义 | Agent 该怎么做 |
| --- | --- | --- |
| `E_INVALID_ARGUMENT` | 参数或请求体校验失败 | 看 `details.field` 改参数；超限就拆分请求，不要原样重试 |
| `E_NOT_FOUND` | 该代码没有结构化宽表数据 | 如实回答「该代码暂无结构化数据」；不要换相近代码、不要估算 |
| `E_DEPENDENCY` | embedding 服务不可用，语义检索暂不可用 | 告知语义检索暂不可用；可改用 `query_structured_data` 或稍后重试，`details.url` 是依赖地址 |
| `E_RATE_LIMITED` | 请求超过限流阈值（REST / 网关路径，MCP 传输本身不限流） | 按 `Retry-After` 退避重试，不要并发硬刷 |
| `E_STORAGE` | 存储不可用 | 告知暂时不可用，稍后重试；不要通过改参数绕过 |
| `E_INTERNAL` | 未预期错误（响应不含堆栈） | 告知失败并附 `trace_id`；不要反复重试 |
| `E_AUTH` | HTTP 传输鉴权失败（401） | 检查 MCP 客户端的 Bearer token；同一 token 重试无意义 |
| `E_CONFIG` | 服务端配置非法（如非回环监听缺 token） | 联系部署方；Agent 无法自行修复 |

### 出处

回答时保留能拿到的出处，不要编造没返回的字段：

- 结构化：`data.as_of`、`data.built_at`、`data.source_version`。
- 风险：每条事件的 `occur_date`、`event_id`、`source_announcement_ref`。
- 公告：每条结果的 `ann_date`、`ann_type`、`title`、`dwd_ref_id`、`minio_url`。

### 缺失值与 null

- **某年某指标为 `null` = 没有数据。** 宽表契约用逐年 `data_status="missing"` 标记缺失年份；当前 `query_structured_data` 的 `years[]` 只回传 `year` 和五项指标，不包含 `data_status`，所以看到 `null` 就按缺失处理。不要写 0，不要插值，不要用相邻年份推断。
- **ROE / PB 当前恒为 `null`**：全库没有净资产（equity）与总股本数据源，无法计算。宽表中的原因字段为 `roe_reason="equity_unavailable"`、`pb_reason="no_equity"`（T-008 Q-02 / Q-03，仍待上游确认；当前查询工具不返回原因键）。回答「暂无数据 / 无法计算」，**不许估算**。
- **PE 也可能为 `null`**（无价格、无盈利等）。PE 的价格来源方案（T-008 Q-01）尚未最终确认，回答时只说库中没有该值，不要断言计算口径。

### 数据质量

- 字段位置：`meta.quality_status`（风险、公告的首条记录）、`search` 结果每条 `results[].quality_status`、`risk` 每条 `events[].quality_status`。
- 只要 `quality_status` 为 `suspicious` 或 `bad`，必须向用户显式提示「该条数据质量存疑」，不得当作确定事实，不得平滑成正常值。
- `quality_status = null` 表示未标注，不要推断为 `normal`，也不要声称「数据质量正常」。

### 合规

- 默认关闭合规标记（DEC-0010）：此时 `meta.compliance_tier` 与记录上的 `compliance` 均为 `null`，表示「未记录」，**不等于可商用**。
- 开启后，若 `compliance_tier = "D"` 或 `commercial_use = false`，必须提示该数据不可商用 / 不可再分发（仅限内部研究）。
- 只按返回的字段提示，不要自行推断等级。

## 回答规范

- 先给结论，再列明细；每个数字带年份和可用的出处。
- 查不到就说查不到，并说明是「库内无记录」，不要说成「不存在」或「没有风险」。
- 不生成预测、评级或投资建议；本库只提供历史事实。
- 用户问的区间超过 10 年时，主动说明会分段查询，再合并结果。

## 示例

完整对话（含空结果与报错恢复）见 `references/examples.md`。最小示例：

```text
用户：600519 近三年有没有商誉减值相关公告？
→ search_announcements_semantically(symbol="600519", query_text="商誉减值", year_range="2023-2025")
→ 逐条标注 ann_date / ann_type / quality_status；若 count=0，回答「未检索到相关公告」。
```

## 参考文件

- `references/tools.md` — 3 个工具的完整入参 / 出参、信封与 meta 字段、错误明细。
- `references/examples.md` — 3 个完整对话示例（正常、空结果、参数超限后拆分）。
