---
name: financecoffer
description: 查询 FinanceCoffer 金融数据金库中的历史财报、公告语义检索与风险事件。适用于用户询问历史长周期数据、模糊语义搜索、历史风险回溯；不适用于今日实时行情。
---

# FinanceCoffer Skill

## 何时使用

- 用户询问**历史**财报指标（营收、净利润、ROE、PE），且时间跨度超过一个季度。
- 用户做**模糊语义**检索，例如「找出所有关于商誉减值的公告」。
- 用户要求**历史风险回溯**（监管处罚、商誉减值、诉讼、退市风险等）。

## 何时不使用

- 今日/盘中实时行情 → 使用 TickHub-CN / TickHub-US。
- 数据抓取、补抓、重算勾稽 → 不在 FinanceCoffer 职责内。
- 需要预测或研报观点 → 本库只存事实数据。

## 调用规范

1. **必须先确定 `symbol`**。所有语义检索都要求 symbol 作为前置过滤，禁止无 symbol 的全库搜索。
2. 优先调用 `query_structured_data(symbol, metric, year_range)` 取结构化指标；只有需要原文语境时才调用 `search_announcements_semantically`。
3. `year_range` 不超过 10 年；`top_k` 不超过 50。超限应拆分请求。
4. 返回结果必须带出处（公告日期、类型、`dwd_ref_id` 或 MinIO 路径）。回答用户时保留这些出处。
5. 若结果 `quality_status` 为 `suspicious` / `bad`，必须向用户显式提示数据质量存疑，不得当作确定事实。
6. `compliance_tier` 为 `D` 的数据默认不可商用，回答时应提示。

## 严禁

- 严禁大模型脑补历史财务数字；没有查到就回答没有。
- 严禁把 `suspicious` 数据平滑成正常值。
- 严禁在未取得 symbol 时猜测代码。

## 示例

用户：帮我看看 600000 近三年有没有商誉减值相关公告。

1. `query_structured_data(symbol="600000", metric="net_profit", year_range="2023-2025")` 取基线。
2. `search_announcements_semantically(symbol="600000", query_text="商誉减值", year_range="2023-2025")`。
3. 汇总时逐条标注公告日期、类型与质量状态。
