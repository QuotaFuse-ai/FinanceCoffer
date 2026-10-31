# DWS 指标口径与对账方案

| 项 | 内容 |
| --- | --- |
| 版本 | v1.1.2（双口径估值 + 事件字段单位增补 + DWS 市场闸） |
| 日期 | 2026-10-27（口径原文）/ 2026-10-30（迁入 `docs/specs/`）/ 2026-10-30（增补 `reference` 口径）/ 2026-10-31（§4.1 金额单位定稿与市场闸） |
| 状态 | 口径已定稿；§8 的 12 项**待人类确认** |
| 依据 | `docs/specs/financecoffer.md` §4.3／§5、`docs/specs/db-schema.md`、`docs/specs/event-contract.md`、`docs/specs/cli.md`、`src/financecoffer/db/migrations/0005_dws.sql`、`src/financecoffer/db/models/dws.py`、`src/financecoffer/query/service.py` |
| 产出物性质 | **文档 + SQL 草案**。不含生产代码，不改 `src/`，不建库、不跑数据。 |

> **迁移说明**
>
> 本文迁自原内部任务文档（源提交 `558c053`，834 行／49,175 B；原文档所在的任务目录已从仓库移除）。迁移日期 **2026-10-30**。**本文档为公开口径，取代原任务文档**：内网任务编号、作者署名、机器路径等内部信息已剥离，口径内容、SQL 草案与 §8 待确认清单保持原意未改。
>
> 与 `docs/specs/cli.md`、`docs/specs/financecoffer.md` 的相关小节交叉引用；**口径冲突时以本文为准**。
>
> **v1.1 变更（2026-10-30）**：估值改为**双口径并存**——构建时同时落 `snapshot`（原 A 方案）与 `reference`（单季扣非年化，对齐外部成熟实现），默认读 `snapshot`，调用方可用 `valuation_basis` 切换。同步新增两个可选上游字段：`financial.amount_net_profit_deducted`（累计扣非净利润）与 `quote.pb_mrq`（上游 PB 透传）。见 §3.2、§4.4.5、§4.4.6。

---

## 0. 结论先行

1. **宽表列已定，口径未定。** `dws.stock_agent_analysis_wide` 的 8 个业务列 + 2 个合规列在 DDL 与 ORM 中完全一致；缺的是每一列"取哪张 DWD 表、按什么规则取"的定义——本文给出。
2. **营收/净利润可以立刻实现。** `dwd.stock_financial_snapshot` 的 `amount_revenue` / `amount_net_profit`（`NUMERIC(20,4)`）按 `(report_date, report_type)` 取 `MAX(version)` 即得，金额全程 `NUMERIC`，不做任何换算与勾稽。
3. **ROE 当前无数据可算；PB 只有厂商透传一条路。** 全库（含事件契约）没有净资产（equity）与总股本（share count）字段。v1.1 起上游可提供累计扣非净利润与厂商 PB（迁移 `0013`），但**缺失时仍按 NULL 处理**，不做替代推算。本文把补字段列为待确认（Q-02/Q-03）。
4. **估值采用"双口径并存"**：构建时同时落 `snapshot` 与 `reference`，默认读 `snapshot`。`snapshot` 沿用 A 方案（优先透传 `pe_ttm`，缺失时 `total_market_cap / 年度净利润` 兜底）；`reference` 对齐外部成熟实现的"单季扣非净利润 × 4"年化口径（`total_market_cap / (单季扣非 × 4)`），PB 透传上游 `pb_mrq`，ROE 仍为 `null`。两套口径只存不算、缺失即 NULL，**绝不互相替代**。见 §4.4.5。
5. **文档与实现曾对不上 3 处，已逐条列出（§2.2）**；其中 `docs/specs/cli.md` §3.9 的宽表字段清单已按 DDL 收敛，构建侧一律以 DDL 为准。

---

## 1. 范围与约束

### 1.1 本文回答什么

只回答"宽表里每个字段怎么来、怎么算、什么时候为空、怎么重算、怎么验"，即：

- 字段级映射（§3）
- 指标口径（§4）
- NULL / 脏数据规则（§5）
- 幂等与重算（§6）
- 对账方案与 SQL 草案（§7）

### 1.2 本文不做什么

- 不写 `financecoffer.dws` 模块代码（本文只定口径，实现属后续开发）；
- 不改 DWD 表结构、不改迁移；
- 不做财务勾稽、不做数据校验（铁律 2）；
- 不决定产品语义（凡属产品/客户决策的，进 §8 待确认）。

### 1.3 四条铁律在 DWS 层的落地

| 铁律 | 在 DWS 构建中的具体含义 |
| --- | --- |
| 只存不算 | 营收/净利润原样搬运；PE 的分子分母都取自 DWD，不在本层推导新事实；不做行业调整、不做口径修正。 |
| 严禁重复校验 | `quality_status='bad'` 照常参与构建并打标，**不丢弃、不降级、不重算**。 |
| 严禁 Update 覆盖 | 宽表按 `symbol` 全量覆盖是"重算快照"，不改写 DWD；DWD 侧永远只插 `MAX(version)+1`。宽表历史不留版本（DDL 无 version 列），`as_of` 是唯一时点标识。 |
| 安全检索 | 宽表本身无检索路径；所有对外查询强制带 `symbol`，本层不引入全表扫描。 |

---

## 2. 现状核对：DDL / ORM / 文档

### 2.1 权威列清单

核对对象：`0005_dws.sql` + `0008_compliance.sql`（DDL）与 `src/financecoffer/db/models/dws.py`（ORM）。

| 列名 | 类型 | 可空 | 默认 | ORM 属性 | 读取方（query/service.py） |
| --- | --- | --- | --- | --- | --- |
| `symbol` | `VARCHAR(20)` | 否 | — | `symbol`（PK） | `structured()` 的 `WHERE` 条件 |
| `as_of` | `TIMESTAMPTZ` | 否 | 无 | `as_of` | 原样返回（`_iso`） |
| `metrics_json` | `JSONB` | 否 | `'{}'::jsonb` | `metrics_json` | `_year_items()` → `_year_record()` |
| `risk_summary` | `JSONB` | 是 | 无 | `risk_summary` | 原样透传 |
| `latest_announcements` | `JSONB` | 是 | 无 | `latest_announcements` | 原样透传 |
| `fiscal_years` | `SMALLINT` | 否 | `5` | `fiscal_years` | 原样透传；CHECK 1–10 |
| `source_version` | `VARCHAR(50)` | 是 | 无 | `source_version` | 原样透传 |
| `built_at` | `TIMESTAMPTZ` | 否 | `now()` | `built_at` | 原样透传（`_iso`） |
| `compliance_tier` | `VARCHAR(1)` | 是 | 无 | `compliance_tier` | `_mark_from_row()`（仅开关开启时 SELECT） |
| `commercial_use` | `BOOLEAN` | 是 | 无 | `commercial_use` | 同上 |

约束与索引（两侧一致）：`ck_dws_wide_fiscal_years`、`ck_dws_wide_metrics`（`jsonb_typeof(metrics_json) = 'object'`）、`ck_dws_wide_compliance_tier`（`S/B/C/D/X` 或 `NULL`）、`idx_dws_wide_as_of (as_of DESC)`。

> **核对结论：`models/dws.py` 与 `0005_dws.sql` + `0008` 逐列一致，无差异。** ORM 中 `commercial_use` 未显式写 `Boolean`（由类型注解推断），不影响 DDL。

### 2.2 与文档不一致清单

| 编号 | 位置 | 问题 | 处理 |
| --- | --- | --- | --- |
| **M-01** | `docs/specs/cli.md` §3.9 | 声称宽表字段为 `revenue_5y`、`net_profit_5y`、`roe_5y`、`pe`、`pb`、`risk_summary`、`latest_announcement_ids`、`quality_status`、`confidence`、`as_of_date`。**10 个字段与 DDL 零个同名**（`latest_announcement_ids` vs `latest_announcements`、`as_of_date` vs `as_of` 也不一致）。 | 以 DDL 为准。本文所有映射按 DDL。**已收敛**：现行 `docs/specs/cli.md` §3.9 已按 DDL 对齐，字段名与 DDL 一致。 |
| **M-02** | `docs/specs/cli.md` §3.9 | 曾要求宽表有顶层 `quality_status` / `confidence` 列，DDL 中不存在；`structured()` 因此返回 `quality_status=None`。 | **已收敛**：现行 cli.md 不再要求顶层列。本方案用 `metrics_json.quality` + 逐年 `quality_status`/`confidence` 承载；是否加列进 Q-10。 |
| **M-03** | 早年草稿 | 写"与 `NUMERIC(24,4)` 对齐"。实际财务列是 `NUMERIC(20,4)`；`NUMERIC(24,4)` 只用于 `volume` / `amount` / `total_market_cap`。 | 本文按 `NUMERIC(20,4)` 定义金额精度（4 位小数，整数位 16 位）。 |
| **M-04** | `financecoffer.md` §4.3 | 写"`metrics_json` 存过去 5 年逐年指标**数组**"，DDL CHECK 要求 **object**。 | **已收敛**：构建侧一律写 object（内含 `years` 数组）；读取层 `_year_items()` 兼容数组只是容错，不作为构建依据。 |
| **M-05** | 读取层 `_YEAR_CONTAINERS` | 接受 `metrics` / `years` / `annual` / `fiscal_years` 四种容器名，且对"年份为键的对象"也能读。 | 本文固定用 `years`，其余形态不写，避免同一份数据出现两种形状。 |
| **M-06** | `0005_dws.sql` | `as_of` 无默认值且 NOT NULL；`fiscal_years` 无构建侧校验。 | build 必须显式写 `as_of`；`fiscal_years` 必须在 1–10，由 CLI `--years` 校验后再落库。 |
| **M-07** | 全库 | 原**无净资产、无总股本、无 PB 字段**（`dwd.stock_financial_snapshot` 只有 `amount_revenue` / `amount_net_profit`；`dwd.stock_quote_snapshot` 有 `pe_ttm`、`total_market_cap`，无 PB）。 | **部分收敛（v1.1）**：迁移 `0013` 给 financial 补 `amount_net_profit_deducted`、给 quote 补 `pb_mrq`（均为可选上游字段）；净资产仍缺，ROE 与 `snapshot` 口径 PB 继续 `NULL` + 原因码（Q-02/Q-03）。 |
| **M-08** | `event-contract.md` §3.4 / G-02 / G-13 | `dwd.stock_quote_snapshot` 已建表但**无写入方**（`quote` 事件未定稿，TickHub-CN 也未接 kline/financial）。 | PE 源数据当前为空；本方案定义"A 方案 + 缺失即 NULL"，不阻塞建表。 |
| **M-09** | `event-contract.md` §3.2 | `report_type` 无枚举、无 `fiscal_year` 字段，样本只有 `"Q3"`；同一 `report_date` 是否可能同时有年报与季报，契约未说明。 | 年度归属按 `report_date` 的年份；年报识别用白名单；两者均进 Q-04/Q-05。 |
| **M-10** | `0005_dws.sql` | 宽表无 `version` 列，无法保留历史快照。 | 设计如此（DWS 是可重算的派生层）；`as_of` + `built_at` 承担审计。 |

---

## 3. 字段级映射表

### 3.1 顶层列 → DWD 来源

"取最新版本"统一指：在同一业务键上 `ORDER BY version DESC LIMIT 1`（等价 `DISTINCT ON (业务键) ... ORDER BY 业务键, version DESC`），**不使用 `max(version)` 再回表**，避免同版本并发写入时的歧义。

| 宽表列 | 来源表 | 来源字段 | 过滤 / 取数规则 | 空值条件 |
| --- | --- | --- | --- | --- |
| `symbol` | 触发构建的入参（须与 DWD 中一致） | — | 归一化：去空格 → 大写；A 股 6 位数字，美股 `[A-Z][A-Z0-9.\-]{0,9}`；与 `query.service.normalize_symbol` 同一正则 | 不合法直接拒绝，不落行 |
| `as_of` | — | — | `date_trunc('day', now() AT TIME ZONE 'UTC')`，即构建日 UTC 00:00；`--as-of YYYY-MM-DD` 可覆盖 | NOT NULL |
| `metrics_json` | `dwd.stock_financial_snapshot`（+ `dwd.stock_quote_snapshot` 仅 A 方案取 PE） | `report_date`、`report_type`、`version`、`amount_revenue`、`amount_net_profit`、`quality_status`、`confidence` | 逐财年取一条；`report_date <= as_of`；结构见 §3.2 | 某个财年无任何报告 → 该年 `data_status="missing"`、指标全 `null` |
| `risk_summary` | `dwd.stock_risk_event` | `occur_date`、`risk_type`、`severity`、`summary`、`event_id`、`source_announcement_ref`、`quality_status` | 窗口 `(as_of-3年, as_of]`；`severity` 四级统计、high/critical 进摘要；结构见 §3.3 | 窗口内无事件 → `counts` 全 0、`highlights=[]`、`highest_severity=null` |
| `latest_announcements` | `dwd.stock_announcement` | `id`、`ann_date`、`ann_type`、`title`、`minio_url`、`quality_status`、`confidence` | `ann_date <= as_of`；`ORDER BY ann_date DESC, id DESC LIMIT 20`；结构见 §3.4 | 无公告 → `[]`（不写 `null`） |
| `fiscal_years` | CLI 参数 `--years` | — | 默认 5，取值 1–10，落库前校验 | 非法值拒绝 |
| `source_version` | 本文口径版本 | — | 常量 `"dws-1"`；口径变更时递增（§6.4） | 可为 `NULL`，但本方案恒写 |
| `built_at` | — | — | 落库时刻，由 DB `now()` 或应用时间写入；**不参与确定性比较** | NOT NULL |
| `compliance_tier` | 参与构建的所有 DWD 行的 `compliance_tier` | — | 仅 `FC_COMPLIANCE_ENABLED=true` 时写；所有来源行一致则透传，不一致则构建失败（fail-closed），不做跨等级合并（Q-12） | 开关关闭恒 `NULL` |
| `commercial_use` | 同上 | — | 同上 | 开关关闭恒 `NULL` |

### 3.2 `metrics_json` 结构（canonical）

必须满足：`jsonb_typeof = 'object'`（CHECK）、`years` 为数组（读取层 `_YEAR_CONTAINERS` 第 2 项）、每个元素含整数 `year`（否则 `structured()` 的年过滤会把该行当"无年份"永久保留）。

```json
{
  "schema": "dws.metrics.v1",
  "basis": "annual",
  "window": {"from_year": 2022, "to_year": 2026, "fiscal_years": 5, "as_of": "2026-10-27"},
  "years": [
    {
      "year": 2026,
      "report_date": "2026-09-30",
      "report_type": "Q3",
      "version": 1,
      "report_basis": "period",
      "revenue": "123456789.0000",
      "net_profit": "9876543.2100",
      "roe": null,
      "roe_reason": "equity_unavailable",
      "pe": null,
      "pe_reason": "no_price",
      "pb": null,
      "pb_reason": "no_equity",
      "quality_status": "normal",
      "confidence": "high",
      "data_status": "present"
    },
    {
      "year": 2025,
      "report_date": null,
      "report_type": null,
      "version": null,
      "report_basis": null,
      "revenue": null,
      "net_profit": null,
      "roe": null,
      "roe_reason": null,
      "pe": null,
      "pe_reason": null,
      "pb": null,
      "pb_reason": null,
      "quality_status": null,
      "confidence": null,
      "data_status": "missing"
    }
  ],
  "gaps": [2025],
  "valuation": {
    "as_of": "2026-10-27",
    "price_date": null,
    "price_close": null,
    "default_basis": "snapshot",
    "pe_source": null,
    "pe": null,
    "pe_reason": "no_price",
    "pb": null,
    "pb_reason": "no_equity",
    "bases": {
      "snapshot": {
        "pe": null,
        "pe_source": null,
        "pe_reason": "no_price",
        "pb": null,
        "pb_reason": "no_equity",
        "roe": null,
        "roe_reason": "equity_unavailable"
      },
      "reference": {
        "pe": null,
        "pe_source": null,
        "pe_reason": "no_deducted_earnings",
        "pe_cross_source": "quote.pe_ttm",
        "pe_cross": null,
        "pb": null,
        "pb_reason": "no_vendor_pb",
        "roe": null,
        "roe_reason": "equity_unavailable",
        "report_date": null,
        "report_type": null,
        "quarter_basis": "single_quarter_deducted_x4"
      }
    }
  },
  "quality": {
    "bad_years": [],
    "suspicious_years": [],
    "missing_years": [2025]
  }
}
```

约定：

- **金额一律字符串**（`"123456789.0000"`），沿用 `event-contract.md` §3.2"金额以字符串传输，避免 JSON float 精度丢失"。JSONB 中不做 float 往返。
- **比率一律 JSON 数字**，4 位小数：ROE 用小数（`0.0831` = 8.31%），PE/PB 用倍数。
- `valuation.bases` **必填**：至少含 `snapshot`，通常也含 `reference`；`valuation.default_basis` 必须指向其中一个。顶层 `pe` / `pe_source` / `pe_reason` / `pb` / `pb_reason` 是 `default_basis` 的**镜像**，只供旧读取方兼容；两者冲突时以 `bases[default_basis]` 为准。
- `reference` 口径的 `pe_cross` 是厂商 `pe_ttm` 的对照值，**不参与** `pe` 的计算；`quarter_basis` 固定为 `single_quarter_deducted_x4`，是口径版本标记。
- 年份记录内除 `year` 外的 5 个指标键名必须与 `STRUCTURED_METRICS` 完全一致（`revenue` / `net_profit` / `roe` / `pe` / `pb`），否则 `structured()` 读不到（`_year_record` 只拷贝这 5 个键 + `year`）。
- `roe_reason` / `pe_reason` / `pb_reason` 是补充键：仅当对应指标为 `null` 时给出原因码（`equity_unavailable` / `no_price` / `no_earnings` / `non_positive_earnings` / `out_of_range` / `no_equity`），有值时写 `null`。读取层不消费，供消费方与排查使用；`data_status="missing"` 的年份三者均为 `null`。
- `report_date` / `report_type` / `version` / `quality_status` / `confidence` 是**溯源字段**：`structured()` 不返回它们，但对账 SQL 直接读 JSONB（§7.3 R1、R5），这是"逐指标可回溯"的抓手，必须写。
- `years` 按 `year` **降序**排列（最新在前），`gaps` 升序。
- 禁止顶层出现标量键 `metrics` / `annual` / `fiscal_years`（读取层会把它们当容器尝试解析）。

### 3.3 `risk_summary` 结构

```json
{
  "window": {"since": "2023-10-27", "until": "2026-10-27"},
  "as_of": "2026-10-27",
  "counts": {"critical": 1, "high": 3, "medium": 5, "low": 2, "total": 11},
  "highest_severity": "critical",
  "latest_occur_date": "2026-09-12",
  "highlights": [
    {
      "event_id": "evt_5f1c...",
      "occur_date": "2026-09-12",
      "severity": "critical",
      "risk_type": "goodwill_impairment",
      "summary": "计提商誉减值 3.2 亿元",
      "source_announcement_ref": "dwd:stock_announcement:12345",
      "quality_status": "suspicious",
      "confidence": "medium"
    }
  ],
  "truncated": false,
  "text": "近 3 年共 11 条风险事件：critical 1 条、high 3 条、medium 5 条、low 2 条。最近一次为 2026-09-12 的商誉减值（critical）：计提商誉减值 3.2 亿元。"
}
```

本表无 DDL 约束，结构由本文定义（待评审）。

### 3.4 `latest_announcements` 结构

数组，元素字段固定白名单：

```json
[
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
```

- `id` 即 `dwd.stock_announcement.id`，与 `vector.announcement_chunk.dwd_ref_id` 同源，可据此回溯源公告与切片。
- 不返回 `clean_content`（大文本走 `minio_url`），`title` 截断至 200 字符（超长以 `…` 结尾并置 `title_truncated: true`）。
- 空结果写 `[]`，不写 `null`，便于消费方直接迭代。

---

## 4. 口径定义

### 4.1 通用约定

| 项 | 定义 |
| --- | --- |
| 时间基准 | `as_of` = 构建日（或 `--as-of`）UTC 00:00:00；所有窗口边界以此为锚，`DATE` 类型字段（`report_date` / `occur_date` / `ann_date`）无时区，按自然日直接比较 |
| 版本规则 | 同一业务键 `ORDER BY version DESC LIMIT 1`；`version` 由 FinanceCoffer 生成，上游不传 |
| 金额单位 | **元（CNY）**，与 DWD `amount_*` 原样一致，不做万元/亿元换算、不做汇率换算。该口径只适用于 A 股（`market=CN`）；US/HK 行缺少币种列，`reference`/`snapshot` 一律 null + 原因码（`currency_unknown` / `unsupported_market`）。展示层需要万元/亿元时自行换算（Q-09） |
| 金额精度 | 4 位小数（对齐 `NUMERIC(20,4)`）；不四舍五入到整数，不科学计数 |
| 比率精度 | 4 位小数；ROE 用小数（非百分数） |
| 浮点 | 全链路禁用 float：SQL 侧 `NUMERIC`，Python 侧 `Decimal`，JSONB 侧金额用字符串 |
| 年度基准 | 一个财年一条记录，`year = EXTRACT(year FROM report_date)`；年报优先（Q-04/Q-05） |
| 同比 | **v1 不落 YoY**。消费方可从 `years[].revenue` 自算；落 YoY 会引入"哪一年是基期/缺失年怎么处理"的第二套规则（Q-06） |
| 5 年窗口 | `fiscal_years` 个自然年，截止 `as_of` 所在年，向前连续；窗口内无报告的年份显式置 `data_status="missing"` |

#### 4.1.1 事件金额/比率字段的单位与缺失语义（增补，逐字合入上游 `UNIT-AUDIT.md` §9）

- `total_market_cap`：事件层为**基本货币单位**（A 股=元；US=美元；HK=港元）。厂商刻度差异在采集端归一（CN 的腾讯 45 号位为亿元，只在事件构造处 ×1e8 一次），DWS **不得再乘任何系数**。
- `pb_mrq`：最新报告季市净率（倍数），与 `pb` 同值。**仅当 `> 0` 时出现**；0 / 负 / 缺失 ⇒ 字段缺席、DWD 落 `NULL`，`reference` 口径写 `no_vendor_pb`，不自行补算。
- `amount_net_profit_deducted`：扣非归母净利润，年初至今累计，基本货币单位。缺失落 `NULL`；**不得**用 `amount_net_profit`（归母）顶替——两者在存在非经常性损益时不等，顶替即一次静默的估值重述。
- `biz_key` 代次：`quote` / `financial` 的键尾带字段代次标记（当前 `|g2`）。`event_id` 只哈希 `provider|data_type|symbol|biz_key`，DWD append-only 且按 `event_id` 去重，因此字段单位/口径变化必须体现为键变化，否则修正会被当成重放丢弃；标记是常量，不随运行时间变化。
- 币种：`dwd.stock_quote_snapshot` 没有币种列（`db/models/dwd.py:200-234`，`currency` 只存在于 `NorthboundFlow`），A 股之外的金额事件缺少自证币种的落库位置；本期 `reference` 只承诺 A 股，US/HK 走 `null` + 原因码，不是缺陷。
- 市场闸（本口径的代码执行点）：DWS 构建的唯一汇合点 `dws/builder.py::_build_one` 只接受 6 位数字 symbol（判据是形状，不是交易所前缀白名单）。非 A 股 symbol **不进公式**：`build --all` / `rebuild` 枚举到它、`--symbol` 显式指定它，都进 `BuildReport.skipped`，原因码 `non_a_share_symbol`（跳过不是失败，`rebuild` 不得因含 US 行而退出非零）。因此上文的 `currency_unknown` / `unsupported_market` 在闸之后不会真的产出 US/HK 宽表行；它们保留为**切流前遗留行**与未来跨市场需求的口径承诺。
- 遗留行：闸不删除此前落下的非 A 股宽表行。`dws show AAPL` 仍返回该行原文，但输出 `valuation.available=false` + 原因码 `non_a_share_symbol`，不静默展示估值（`cli.md` §3.9）。
- 精度：事件金额一律**字符串**、4 位小数，全链路 `Decimal`；`float` 乘 1e8 会把大市值的尾数变成噪声，`NUMERIC(24,4)` 侧会当成精度损失拒绝入库。

### 4.2 `revenue` / `net_profit`

**定义**：某财年的营业收入 / 归母（或净利润，见下）净利润，单位元，直接取自上一年报类报告的 `amount_revenue` / `amount_net_profit`。

**取数**：

1. 候选集：`dwd.stock_financial_snapshot WHERE symbol = :symbol AND report_date <= as_of`。
2. 按 `(report_date, report_type)` 聚组，每组取 `version` 最大的那一行。
3. 每个财年（`report_date` 的年份）从候选中选一条：
   - 优先级 1：`report_type ∈ ('FY','ANNUAL','年报','A')`（白名单，Q-04 确认）——全年口径，`report_basis="annual"`；
   - 优先级 2：无年报时取该年 `report_date` 最大、`version` 最大的期间报告——`report_basis="period"`，**不做年化、不做累加**（Q-04）；
   - 同优先级多条时取 `report_date DESC, version DESC` 第一条（全序，保证确定性）。
4. `year` 取值见 §4.1（Q-05：3 月披露的 2025 年报 `report_date` 是 2025-12-31，年份归属天然正确；但若上游把 `report_date` 填成披露日则不成立）。

**规则**：

- 负值（亏损、营收为负的极端行业）**原样保留**，不改符号、不置零。
- 值为 `NULL` 的原始行照常落 `null`，不补 0、不插值。
- 不做任何勾稽（不校验"净利润 ≤ 营收"等），完全信任上游 `quality_status`。
- `quality_status='bad'` 的行**照常参与**并被记录（§5.1）。

### 4.3 ROE

**公式（建议，待 Q-02 确认）**：

```
ROE(年) = 净利润(年) / ((期初归母净资产 + 期末归母净资产) / 2)
```

- 分母用**平均净资产**（期初期末均值）为行业惯例；期初缺失时退化为期末净资产，并在该年记录写 `roe_basis="ending_equity"`；否则 `roe_basis="average_equity"`。
- **不年化**：年度口径本身就是年化值；期间口径（`report_basis="period"`）**不推算年化 ROE**（会把 9 个月利润乘 4/3，属于凭空造数）。
- 单位：小数，4 位；分母 ≤ 0 或缺失 → `null` + `roe_reason`。
- 净利润是否要求"归母"：DWD 只有单一 `amount_net_profit`，无归母/少数股东拆分 → 与分子同源即可，需在文档口径中注明（Q-02）。

**当前状态：不可计算。** 全库无净资产字段（M-07）。处理：

- 每年 `roe = null`；
- `metrics_json.quality` 之外，在 `valuation` 同级增补 `"roe_reason": "equity_unavailable"`（或逐年 `roe_reason`，本文采用**逐年**，避免与估值块混淆）；
- 一旦上游补 `amount_equity`（建议同时补 `amount_equity_prior` 或允许用上一年 `amount_equity` 推导期初），本口径即可启用，构建代码的 SQL 骨架见 §7.3 R1 的注释。

### 4.4 PE

#### 4.4.1 可计算性核对

| 候选公式 | 分子 | 分母 | 可行性 |
| --- | --- | --- | --- |
| 总市值 / 净利润 | `dwd.stock_quote_snapshot.total_market_cap` | `dwd.stock_financial_snapshot.amount_net_profit` | **可行** |
| 收盘价 / EPS | `close` | 需要总股本 → 无 | 不可行 |
| 直接透传 | `dwd.stock_quote_snapshot.pe_ttm` | — | **可行（优先）** |

#### 4.4.2 两个方案

| 维度 | **A：构建时快照**（推荐） | **B：查询时计算** |
| --- | --- | --- |
| 取价时点 | `as_of` 当日，`ORDER BY trade_date DESC, version DESC LIMIT 1` | 查询发生时（`now()`）最新收盘 |
| 存放 | `metrics_json.valuation` + 最新年度的 `pe` | 宽表不存 PE，查询层 JOIN quote + financial |
| 读取成本 | 单表单行（与宽表定位一致） | 每次多两次 JOIN，AI 高频调用时放大 |
| 时效性 | 停在 `as_of`，需要每日重建 | 始终最新 |
| 可复现性 | **强**：同一 `as_of` 同一输入恒定，适合审计与回归测试 | **弱**：同一天不同时刻结果不同，测试无法固定期望值 |
| 与铁律/定位 | 契合"宽表解决跨表 JOIN 慢" | 部分违背 |
| 失败面 | quote 缺失 → 该股 PE 为 `null` 直到下次重建 | quote 缺失 → 每次查询都退化 |
| 额外成本 | 需要 `dws build` 的调度频率 | 无需调度 |
| 当前可行性 | quote 表无写入方（M-08），两者都取不到价 | 同左 |

#### 4.4.3 推荐与切换条件

**推荐 A**。理由：宽表的存在意义就是"预计算换查询速度"；PE 是慢变量（随财报变），日频重建完全够用；可复现性对"AI 不能脑补历史数据"这条产品红线更重要——同一 `as_of` 下两个 Agent 必须看到同一个 PE。

**满足以下任一条件时改用 B（或 A+B 混合）**，并回到本文修订：

1. 客户明确要求 PE **当日有效**（T 日收盘后 30 分钟内可用），且 `dws build` 的调度频率低于每日；
2. quote 数据源改为高频推送（事件级），预计算快照的滞后被明确判定为不可接受；
3. 对 PE 的读取频率高到"每日重建一次仍不够"（例如盘中按分钟刷新）。

混合方案（备选）：宽表存年末 PE 序列，查询时若有更新的 quote 则覆盖最新年度——代价是查询路径重新引入 JOIN 与不确定性，**不建议在 v1 采用**。

#### 4.4.4 `snapshot` 口径的计算与 NULL 规则

```
pe_year(year) =
  quote.pe_ttm                                   -- 优先透传（只存不算）
  ?? round(total_market_cap / net_profit_year, 4) -- 兜底
  ?? null
```

- **绝不落负数**：`net_profit <= 0` 时 PE 无经济含义，落 `null` + `pe_reason="non_positive_earnings"`。负数 PE 会让 AI 得出"估值便宜"的错误结论。
- **绝不落 0**：`null` 与 `0` 必须区分。
- 取价日期与该年 `report_date` 的间隔超过 1 年（历史年份）→ 该年 PE 落 `null` + `pe_reason="no_price"`；**不做价格回填、不插值**。历史年 PE 仅在"该年年末或次年初存在 quote 快照"时才有值。
- 价格缺失（quote 无行 / `close` 与 `total_market_cap` 均为 `NULL`）→ `null` + `"no_price"`。
- 净利润缺失 → `null` + `"no_earnings"`。
- 除法必须用 `NUMERIC`，`round(..., 4)`，禁止转 float。
- `valuation.pe` 取**最新年度记录**的 `pe`，并在 `valuation.pe_source` 记录 `"quote.pe_ttm"` 或 `"market_cap/net_profit"`。

#### 4.4.5 `reference` 口径（单季扣非年化）

**动机**：`snapshot` 口径的 PE 完全依赖厂商 `pe_ttm`；上游一旦给的是"最近年度"或口径混杂的值，AI 读到的估值就会漂移。`reference` 用更窄、更可复核的分子分母，并保留厂商值做对照：

```
quarter        = 单季扣非净利润
                   Q1: 当期累计
                   Q2/Q3/Q4: 当期累计 - 同一财年上一季累计
pe_reference   = round(total_market_cap / (quarter * 4), 4)
pb_reference   = 上游 quote.pb_mrq（透传）
roe_reference  = null + "equity_unavailable"
```

- 扣非净利润来源：`dwd.stock_financial_snapshot.amount_net_profit_deducted`（累计值，迁移 `0013` 新增列），**缺失时绝不回退**到 `amount_net_profit`。
- 单季拆分只接受季末报告期（3/6/9/12 月）；Q1 直接取累计，其余季度要求同财年上一季累计存在，否则 `pe=null` + `pe_reason="no_prior_cumulative"`。非季末报告期 → `"unsupported_report_period"`。
- `pe` 的 NULL 原因码：`no_price`（无 quote / 无价无市值）、`no_deducted_earnings`、`no_prior_cumulative`、`unsupported_report_period`、`non_positive_earnings`（单季扣非 ≤ 0）、`no_market_cap`。
- `pb` 只在 `quote.pb_mrq > 0` 时透传，否则 `null` + `"no_vendor_pb"`；**不自行用净资产计算**。
- `pe_cross` 保存厂商 `pe_ttm` 原值（可为 `null`），仅作对照，不参与主值计算。
- `report_date` / `report_type` 记录所用扣非数据的报告期，`quarter_basis="single_quarter_deducted_x4"` 标记年化方式。

#### 4.4.6 读取时选择口径

宽表同时存两套口径，读取方按需选择，**不在查询时重算**：

| 入口 | 参数 | 说明 |
| --- | --- | --- |
| CLI | `fcoffer query structured --symbol S --valuation-basis snapshot\|reference` | 缺省取 `default_basis`（`snapshot`），大小写与首尾空格归一化 |
| REST | `GET /v1/structured/{symbol}?valuation_basis=reference` | 同上；非法值返回 `400 E_INVALID_ARGUMENT`，`details.field="valuation_basis"` |
| MCP | `query_structured_data(..., valuation_basis="reference")` | 同上，`anyOf` 枚举或 `null` |

返回的 `data.valuation` 固定带 `basis`（实际口径）、`default_basis`、`available_bases`（该行已构建的口径列表），以及该口径的 `pe` / `pb` / `roe` 与原因码。请求一个该行未构建的口径（例如旧宽表只有 `snapshot`）时**不报错**：返回 `pe=null`、`pe_reason="basis_not_built"`，同时 `available_bases` 会说明可选范围。

#### 4.4.7 方案切换条件

`default_basis` 保持 `snapshot`；满足以下任一条件时可改默认（或让客户侧固定选择），并回到本文修订：

#### 4.4.7 默认口径的切换

`default_basis` 保持 `snapshot`。需要改默认口径时，触发条件沿用 §4.4.3 的第 1–3 条，并同步修订本节与 §3.2 的示例；客户侧也可以不全局改默认，只在调用时传 `reference`。

### 4.5 PB

**公式**：`PB = 总市值 / 归母净资产`；`reference` 口径另有**透传路径**。

- 自算路径：分子可得（`total_market_cap`），**分母不存在**（M-07）→ `snapshot` 口径 `pb = null`、`pb_reason="no_equity"`，与 ROE 同批阻塞（Q-03）。
- 透传路径：quote 事件新增可选 `pb_mrq`（厂商 MRQ 市净率），`reference` 口径按"只存不算"直接透传，且仅当 `pb_mrq > 0`；缺失或非正 → `pb = null`、`pb_reason="no_vendor_pb"`。
- **两条路径互不替代**：没有 `pb_mrq` 时不拿净资产推算，有 `pb_mrq` 时也不反向填充 `snapshot` 口径。

### 4.6 `risk_summary` 口径

| 项 | 定义 |
| --- | --- |
| 时间窗口 | **滚动 3 年**：`occur_date > as_of - INTERVAL '3 years' AND occur_date <= as_of`（左开右闭，避免与上一窗口重复计数）。Q-07 确认是否改为"近 5 年"或"全历史"。 |
| 统计范围（`counts`） | 四级全量：`low` / `medium` / `high` / `critical`，外加 `total` |
| 摘要范围（`highlights`） | 仅 `high` + `critical`；按 `occur_date DESC, id DESC`；最多 **10** 条 |
| 最高等级 | `highest_severity` 取 `counts` 中非零的最高档（`critical > high > medium > low`）；全 0 时 `null` |
| 单条文本 | `summary` 截断至 **200** 字符；`NULL` 时用 `"<risk_type>（未提供摘要）"` 占位 |
| 拼接规则 | 固定模板 + 固定顺序（按 `counts` 的 critical→low 输出），不随数据顺序变化；`text` 总长 ≤ **2000** 字符，超长截断并置 `truncated=true` |
| 去重 | 按 `event_id` 去重（DWD 已 `UNIQUE(event_id)`，此处是防御性去重）；同一 `source_announcement_ref` 允许多条（不同类型风险可来自同一公告） |
| 质量标记 | `highlights` 中每条带 `quality_status` / `confidence`；`bad` 仍参与统计与摘要（铁律 2），但会在 `quality` 中计数 |

### 4.7 `latest_announcements` 口径

| 项 | 定义 |
| --- | --- |
| 条数 N | **20**（Q-08 确认） |
| 排序 | `ann_date DESC, id DESC`（全序，保证确定性） |
| 去重 | `content_hash` 在 DWD 已 `UNIQUE`，天然去重；额外按 `id` 去重防御 |
| 时间过滤 | `ann_date <= as_of`（不设下界，最早的 20 条之外的公告不进宽表） |
| 质量过滤 | **不过滤** `quality_status='bad'` 的公告，但透传标记 |
| 字段 | 见 §3.4 白名单，不含正文 |

### 4.8 窗口与 `fiscal_years`

- 窗口 = `[as_of_year - fiscal_years + 1, as_of_year]`，共 `fiscal_years` 个自然年。
- `fiscal_years` 默认 5，`--years` 允许 1–10；与 `ck_dws_wide_fiscal_years` 一致，超限 400，不静默截断（对齐 `financecoffer.md` §5 上限原则）。
- 窗口外的财务数据一律不进 `metrics_json`，但**不删除**——下次改窗口重建即可恢复。

### 4.9 `as_of` / `source_version` / `built_at`

| 列 | 语义 | 取值 |
| --- | --- | --- |
| `as_of` | **数据截止时点**（口径时间） | 构建日 UTC 00:00，或 `--as-of` 指定；决定"哪些 DWD 行参与" |
| `source_version` | **口径版本**（代码 + 规则） | `"dws-1"`；§4/§5 任一条规则变更时递增为 `dws-2`……与 `built_at` 正交 |
| `built_at` | **物理构建时刻**（审计） | 实际落库时间；两次重建即使内容相同也会变 |

三者必须分开：只用 `built_at` 无法解释"为什么数值变了"（可能只是重跑），只用 `as_of` 又无法区分口径升级。

---

## 5. NULL 与脏数据规则

### 5.1 质量字段

| `quality_status` | 是否参与 | 是否标记 | 说明 |
| --- | --- | --- | --- |
| `normal` | 是 | 否 | — |
| `suspicious` | 是 | 是（逐年 `quality_status`，且年份进 `quality.suspicious_years`） | 不降权、不修正、不排除 |
| `bad` | **是** | 是（逐年 `quality_status`，且年份进 `quality.bad_years`） | 上游 `bad` 仍落库（`event-contract.md` §3.2），本层同样不拒绝；仅打标供消费方判断 |
| `NULL` | 不可能出现 | — | DDL 强制 NOT NULL |

- **不因为 `bad` 把整年指标置 `null`**——那等于本层做了"是否可信"的二次判断，违反铁律 2。
- `confidence` 原样透传（`high` / `medium` / `low` / `NULL`），不做换算。

### 5.2 缺失年份

| 场景 | 处理 |
| --- | --- |
| 窗口内某年完全无报告 | 输出该年记录：`year` 有值，其余指标与溯源字段全 `null`，`data_status="missing"`；年份同时进 `gaps` 与 `quality.missing_years` |
| 某年有报告但 `amount_revenue` 为 `NULL` | 该年仅 `revenue=null`，`net_profit` 正常；`data_status="present"` |
| 整只股票无任何财务数据 | **不建宽表行**（没有可服务的内容），`dws build` 记 `skipped` 计数；查询侧沿用 `structured()` 的 `NotFoundError` + 提示 `run fcoffer dws build` |
| 窗口内超过一半年份缺失 | 照常构建，`quality.missing_years` 体现；不降级为 `null` 行 |

**禁止**：补 0、前值填充、行业均值填充、线性插值、按比例拆分。缺失就是缺失，显式 `null` + `data_status="missing"`。

### 5.3 异常值

| 场景 | 处理 |
| --- | --- |
| 营收/净利润为负 | 原样保留；PE 因此可能为 `null`（`non_positive_earnings`） |
| 数值超出 `NUMERIC(20,4)` 范围 | 不可能（DWD 已约束）；若发生则落库失败并计入 `failed`，不截断、不改写 |
| `report_date > as_of`（未来报告） | 排除，不进窗口 |
| 同一 `(report_date, report_type)` 多版本 | 只取 `MAX(version)`，其余版本不参与（DWD 保留全部，本层只读最新） |
| 同一财年存在 `Q3` 与 `FY` 两条 | 按 §4.2 优先级选年报；未选中的不写入（但可通过 `version` 溯源回查） |
| 同一 `report_date` 出现两种 `report_type`（如 `Q3` 与 `Q1-Q3`） | 按 `report_basis` 与 `report_date` 全序选一条；若并列则取 `version` 大者，并在 `quality` 记 `ambiguous_report=True`（Q-04） |
| `symbol` 大小写/空格 | 归一化后匹配；不匹配即视为无数据 |
| 归档（`is_cold=true`） | 财务金额列不因归档置空，照常读取；若未来归档动作把金额置 `NULL`，则按"缺失"处理，不推测 |

### 5.4 单位与精度校验（构建后自检）

构建完成后对每行做断言，任一失败即不落库（fail-closed）：

1. `metrics_json` 是 object，`years` 是数组，长度 ≤ `fiscal_years`；
2. 每个 `year` 为整数且在窗口内；
3. 金额字段为字符串且匹配 `^-?\d{1,16}(\.\d{1,4})?$`；
4. 比率字段为数字或 `null`，`pe` 不为负，`pb` 不为负，`roe` 在 `[-10, 10]`（超过视为异常，置 `null` 并记 `roe_reason="out_of_range"`）；
5. `data_status="missing"` 的记录，5 个指标必须全为 `null`；
6. `years` 按 `year` 严格降序、无重复年份；
7. `valuation.bases` 是非空 object，键名只能是 `snapshot` / `reference`；`default_basis` 必须指向其中一个；每个口径的 `pe` / `pb` / `roe` 为数字或 `null`，有值时不为负。

### 5.5 合规列

- `FC_COMPLIANCE_ENABLED=false`（默认）：**不读取、不写入**这两列，落库时显式 `NULL`（与 `ComplianceMark.as_dict()` 的语义一致）。
- `FC_COMPLIANCE_ENABLED=true`：参与构建的 DWD 行必须都带 `compliance_tier`；全部一致则透传，出现不一致则**构建失败**（不猜、不取"最宽松"），由人工确认后重跑。理由： 的取向是"开启即不允许静默缺口"。
- 该派生规则属待确认（Q-12），实现前需客户确认"宽表等级 = 最严格来源 / 必须一致"。

---

## 6. 幂等与重算

### 6.1 构建粒度

- **per symbol 全量重算**：不增量合并。理由：宽表是"快照"，增量合并会残留已失效的年份/事件；`AgentAnalysisWideRepository.upsert()` 已实现 `ON CONFLICT (symbol) DO UPDATE`，天然是"整行替换"。
- `dws build --all` = 遍历 `SELECT DISTINCT symbol FROM dwd.stock_financial_snapshot`（或 `--symbols` 列表），逐股独立事务；单股失败不影响其他股，最终输出 `built/skipped/failed` 计数。
- `dws rebuild --since T`：把 `as_of` 固定为新时点，对 `T` 之后有新版本的 symbol 重算；**这不是增量构建**，只是缩小循环范围。

### 6.2 确定性要求（同输入 → 同输出）

给定同一份 DWD 数据、同一 `as_of`、同一 `source_version`，两次构建的 `metrics_json` / `risk_summary` / `latest_announcements` **必须字节级相同**（`built_at` 除外）。实现约束：

1. 所有取数 SQL 带**全序** `ORDER BY`（`occur_date DESC, id DESC`；`ann_date DESC, id DESC`；`report_date DESC, version DESC`；`year ASC/DESC` 固定）。
2. JSON 序列化时键序固定（`sort_keys=True` 或按本文示例顺序构造 `dict`），PG 的 `jsonb` 会规范化键序，但数组顺序原样保留。
3. 金额用 `Decimal` → 定点字符串（`quantize(Decimal("0.0001"))`，`str()`），禁止经过 float。
4. 比率用 `Decimal` 计算 → `round(..., 4)` → 转 float 写 JSON 数字（4 位小数在 float 中可精确表达 `<1e-12` 误差，不影响 1e-4 容差）；若要求绝对精确，可改为字符串，但会破坏消费方数值计算，故不采用。
5. `built_at` 不参与比较；`as_of` 参与。
6. 时间函数只用 `as_of`，**构建代码内禁止直接调用 `now()`**（`now()` 只允许出现在 `built_at` 默认值）。

**验证方式**：对同一 symbol 用固定 `--as-of` 连跑两次，比较 `md5(metrics_json::text)`、`md5(risk_summary::text)`、`md5(latest_announcements::text)`，三者必须相同。

### 6.3 重算触发

| 触发 | 是否必须重建该股 |
| --- | --- |
| 该股新增财务版本 | 是 |
| 该股新增风险事件 / 公告 | 是（影响摘要与列表） |
| quote 新增（A 方案） | 是（PE 变化） |
| 口径版本 `source_version` 变更 | 是（全量） |
| `--years` 变化 | 是（该股全部） |
| 仅 `built_at` 到期（无新数据） | 否；重跑结果相同，无收益 |

### 6.4 并发

- 两个构建任务同时处理同一 symbol：结果为"最后写入者胜出"，但因为 `as_of` 与内容确定，**只要 as_of 相同结果就相同**；`as_of` 不同则以 `as_of` 大者为准（upsert 会覆盖，需构建侧用 `WHERE dws.as_of <= EXCLUDED.as_of` 保护，避免旧任务覆盖新任务）。
- 建议实现时对该股加 `pg_advisory_xact_lock(hashtext('dws:' || symbol))`，或依赖上述 `as_of` 单调保护。

### 6.5 构建自检清单（落库前）

1. `fiscal_years ∈ [1,10]`；
2. `as_of` 为 UTC 00:00；
3. 五个指标键名与 `STRUCTURED_METRICS` 一致；
4. 无 float 泄漏（金额全为字符串）；
5. `risk_summary.counts.total` = 四级之和；
6. `latest_announcements` 已按 `ann_date DESC, id DESC` 且长度 ≤ 20；
7. 合规开关关闭时两列值为 `NULL`。

---

## 7. 对账方案

### 7.1 样本选择

要求：覆盖"有多版本财务""有风险事件""有公告""无价格源"四类路径。建议样本（**执行时先用 R0 确认覆盖度，无数据的替换并记录**）：

| 样本 | 覆盖路径 |
| --- | --- |
| `600000`（浦发银行） | A 股、金融业、多版本财务、风险事件密集 |
| `600519`（贵州茅台） | A 股、高 PE、年报齐全、公告多 |
| `AAPL` | 美股 ticker、验证 symbol 归一化与跨市场、验证 quote 缺失路径 |

### 7.2 容差

| 指标 | 容差 | 比较方式 |
| --- | --- | --- |
| 金额（revenue / net_profit） | **0（精确相等）** | `IS DISTINCT FROM`（NULL 安全） |
| 比率（roe / pe / pb） | **≤ 1e-4** | `abs(wide - src) <= 0.0001`，双方皆 `NULL` 视为通过 |
| 计数（风险事件） | 0 | 整数精确 |
| 列表（公告 id 序列） | 0 | 顺序与元素完全一致 |
| 结构（年份集合、`gaps`） | 0 | 集合精确 |
| `null` 一致性 | 0 | `NULL` 对 `NULL` 通过，`NULL` 对数值失败 |

### 7.3 SQL 草案

> 全部为 **psql 可执行草案**，尚未在真实数据库上实测（当前开发环境无 PostgreSQL）。变量用 psql 的 `:'symbol'` / `:'as_of'`。
>
> 执行方式：
> ```bash
> psql "$FC_DB_URL" -v symbol=600000 -v as_of=2026-10-27 -f reconcile.sql
> ```

#### R0 样本覆盖度预检

```sql
-- 选出真正有数据的样本；fin_rows 为 0 的 symbol 不参与对账
SELECT f.symbol,
       count(*)                                                    AS fin_rows,
       count(DISTINCT f.report_date)                               AS report_dates,
       max(f.version)                                              AS max_version,
       (SELECT count(*) FROM dwd.stock_risk_event r  WHERE r.symbol = f.symbol) AS risk_rows,
       (SELECT count(*) FROM dwd.stock_announcement a WHERE a.symbol = f.symbol) AS ann_rows,
       (SELECT count(*) FROM dwd.stock_quote_snapshot q WHERE q.symbol = f.symbol) AS quote_rows
FROM dwd.stock_financial_snapshot f
GROUP BY f.symbol
ORDER BY fin_rows DESC, f.symbol;
```

#### R1 财务：逐年营收 / 净利润 vs DWD 最新版本

```sql
-- 通过宽表里存的溯源字段 (report_date, report_type, version) 精确回表，
-- 不重算"哪条是最新版本"，只验证"宽表写的就是它"
WITH wide AS (
    SELECT (y->>'year')::int                   AS fiscal_year,
           (y->>'report_date')::date           AS report_date,
            y->>'report_type'                  AS report_type,
           (y->>'version')::int                AS version,
           (y->>'revenue')::numeric(20,4)      AS wide_revenue,
           (y->>'net_profit')::numeric(20,4)   AS wide_net_profit,
            y->>'data_status'                  AS data_status
    FROM dws.stock_agent_analysis_wide w
    CROSS JOIN LATERAL jsonb_array_elements(w.metrics_json->'years') AS y
    WHERE w.symbol = :'symbol'
)
SELECT w.fiscal_year,
       w.report_date,
       w.report_type,
       w.version,
       w.data_status,
       w.wide_revenue,
       s.amount_revenue,
       w.wide_net_profit,
       s.amount_net_profit,
       CASE
         WHEN w.data_status = 'missing' THEN
              CASE WHEN w.wide_revenue IS NULL AND w.wide_net_profit IS NULL
                   THEN 'OK(missing)' ELSE 'FAIL(missing_has_value)' END
         WHEN s.symbol IS NULL                       THEN 'FAIL(source_row_missing)'
         WHEN w.wide_revenue    IS DISTINCT FROM s.amount_revenue    THEN 'FAIL(revenue)'
         WHEN w.wide_net_profit IS DISTINCT FROM s.amount_net_profit THEN 'FAIL(net_profit)'
         ELSE 'OK'
       END AS verdict
FROM wide w
LEFT JOIN dwd.stock_financial_snapshot s
       ON s.symbol      = :'symbol'
      AND s.report_date = w.report_date
      AND s.report_type = w.report_type
      AND s.version     = w.version
ORDER BY w.fiscal_year DESC;
```

```sql
-- R1b 版本正确性：宽表引用的 version 必须是该 (report_date, report_type) 的最大版本
WITH wide AS (
    SELECT (y->>'year')::int          AS fiscal_year,
           (y->>'report_date')::date  AS report_date,
            y->>'report_type'         AS report_type,
           (y->>'version')::int       AS version
    FROM dws.stock_agent_analysis_wide w
    CROSS JOIN LATERAL jsonb_array_elements(w.metrics_json->'years') AS y
    WHERE w.symbol = :'symbol' AND (y->>'version') IS NOT NULL
)
SELECT w.fiscal_year, w.report_date, w.report_type,
       w.version AS wide_version, max(s.version) AS dwd_max_version
FROM wide w
JOIN dwd.stock_financial_snapshot s
  ON s.symbol = :'symbol'
 AND s.report_date = w.report_date
 AND s.report_type = w.report_type
GROUP BY w.fiscal_year, w.report_date, w.report_type, w.version
HAVING w.version <> max(s.version);
-- 期望：0 行
```

```sql
-- R1c 年度唯一性：同一 fiscal_year 只能有一条非 missing 记录
SELECT w.metrics_json
FROM dws.stock_agent_analysis_wide w
WHERE w.symbol = :'symbol'
  AND (SELECT count(*) FROM jsonb_array_elements(w.metrics_json->'years') y
        WHERE y->>'data_status' <> 'missing') <>
      (SELECT count(DISTINCT (y->>'year')) FROM jsonb_array_elements(w.metrics_json->'years') y
        WHERE y->>'data_status' <> 'missing');
-- 期望：0 行
```

#### R2 PE：宽表估值 vs quote + 财报重算

```sql
WITH wide AS (
    SELECT (w.metrics_json->'valuation'->>'pe')::numeric(20,4)          AS wide_pe,
            w.metrics_json->'valuation'->>'pe_source'                   AS pe_source,
           (w.metrics_json->'valuation'->>'price_date')::date           AS price_date,
           (w.metrics_json->'valuation'->>'pe_reason')                  AS pe_reason
    FROM dws.stock_agent_analysis_wide w
    WHERE w.symbol = :'symbol'
),
quote AS (
    SELECT q.trade_date, q.version, q.close, q.total_market_cap, q.pe_ttm
    FROM dwd.stock_quote_snapshot q
    WHERE q.symbol = :'symbol' AND q.trade_date <= DATE :'as_of'
    ORDER BY q.trade_date DESC, q.version DESC
    LIMIT 1
),
earn AS (
    SELECT f.amount_net_profit
    FROM dwd.stock_financial_snapshot f
    WHERE f.symbol = :'symbol' AND f.report_date <= DATE :'as_of'
    ORDER BY f.report_date DESC, f.version DESC
    LIMIT 1
)
SELECT w.wide_pe,
       w.pe_source,
       w.price_date,
       q.trade_date        AS src_trade_date,
       q.pe_ttm            AS src_pe_ttm,
       q.total_market_cap  AS src_market_cap,
       e.amount_net_profit AS src_net_profit,
       round(q.total_market_cap / NULLIF(e.amount_net_profit, 0), 4) AS recomputed_pe,
       CASE
         WHEN q.trade_date IS NULL                                  THEN 'NO_PRICE(expected null)'
         WHEN e.amount_net_profit IS NULL OR e.amount_net_profit <= 0 THEN 'NO_EARNINGS(expected null)'
         WHEN w.wide_pe IS NULL                                     THEN 'FAIL(unexpected null)'
         WHEN abs(w.wide_pe - COALESCE(q.pe_ttm,
                round(q.total_market_cap / NULLIF(e.amount_net_profit, 0), 4))) <= 0.0001
                                                                    THEN 'OK'
         ELSE 'FAIL(pe)'
       END AS verdict
FROM wide w
LEFT JOIN quote q ON TRUE
LEFT JOIN earn  e ON TRUE;
-- 注：若 quote 无行，宽表必须为 null 且 pe_reason='no_price'
```

#### R3 风险：`risk_summary.counts` vs 源表计数

```sql
WITH p AS (SELECT :'symbol'::varchar AS symbol, DATE :'as_of' AS as_of),
win AS (
    SELECT r.severity, count(*) AS dwd_count
    FROM dwd.stock_risk_event r, p
    WHERE r.symbol = p.symbol
      AND r.occur_date >  p.as_of - INTERVAL '3 years'
      AND r.occur_date <= p.as_of
    GROUP BY r.severity
),
wide AS (
    SELECT k.key AS severity, k.value::int AS wide_count
    FROM dws.stock_agent_analysis_wide w, p,
         LATERAL jsonb_each_text(w.risk_summary->'counts') AS k
    WHERE w.symbol = p.symbol
      AND k.key IN ('low', 'medium', 'high', 'critical')
)
SELECT COALESCE(win.severity, wide.severity)              AS severity,
       COALESCE(win.dwd_count, 0)                         AS dwd_count,
       COALESCE(wide.wide_count, 0)                       AS wide_count,
       COALESCE(win.dwd_count, 0) - COALESCE(wide.wide_count, 0) AS diff
FROM win
FULL OUTER JOIN wide USING (severity)
ORDER BY severity;
-- 期望：所有 diff = 0
```

```sql
-- R3b highlights 必须全部来自 high/critical、按 occur_date DESC 排序、≤10 条
SELECT w.symbol,
       jsonb_array_length(w.risk_summary->'highlights') AS n,
       (SELECT count(*) FROM jsonb_array_elements(w.risk_summary->'highlights') h
         WHERE h->>'severity' NOT IN ('high','critical')) AS bad_severity,
       (SELECT count(*) FROM jsonb_array_elements(w.risk_summary->'highlights') h
         WHERE (h->>'event_id') IS NULL)                  AS missing_event_id
FROM dws.stock_agent_analysis_wide w
WHERE w.symbol = :'symbol';
-- 期望：n <= 10，bad_severity = 0，missing_event_id = 0
```

#### R4 公告：`latest_announcements` vs 源表 Top-20

```sql
WITH p AS (SELECT :'symbol'::varchar AS symbol, DATE :'as_of' AS as_of),
src AS (
    SELECT a.id, a.ann_date, a.ann_type, left(coalesce(a.title, ''), 200) AS title
    FROM dwd.stock_announcement a, p
    WHERE a.symbol = p.symbol AND a.ann_date <= p.as_of
    ORDER BY a.ann_date DESC, a.id DESC
    LIMIT 20
),
wide AS (
    SELECT (e->>'id')::bigint AS id, (e->>'ann_date')::date AS ann_date
    FROM dws.stock_agent_analysis_wide w, p,
         LATERAL jsonb_array_elements(w.latest_announcements) AS e
    WHERE w.symbol = p.symbol
)
SELECT COALESCE(src.id, wide.id) AS id,
       src.ann_date, src.title, wide.ann_date AS wide_ann_date,
       CASE WHEN src.id IS NULL  THEN 'FAIL(extra_in_wide)'
            WHEN wide.id IS NULL THEN 'FAIL(missing_in_wide)'
            WHEN src.ann_date IS DISTINCT FROM wide.ann_date THEN 'FAIL(ann_date)'
            ELSE 'OK' END AS verdict
FROM src
FULL OUTER JOIN wide USING (id)
ORDER BY src.ann_date DESC NULLS LAST, id DESC;
```

#### R5 结构不变量（防补零 / 防插值 / 防负数 PE）

```sql
SELECT w.symbol,
       w.fiscal_years,
       jsonb_array_length(w.metrics_json->'years')                       AS year_rows,
       (SELECT count(*) FROM jsonb_array_elements(w.metrics_json->'years') y
         WHERE (y->>'year') IS NULL)                                     AS missing_year_key,
       (SELECT count(*) FROM jsonb_array_elements(w.metrics_json->'years') y
         WHERE (y->>'revenue')::numeric = 0)                             AS zero_revenue_years,
       (SELECT count(*) FROM jsonb_array_elements(w.metrics_json->'years') y
         WHERE (y->>'pe')::numeric < 0)                                  AS negative_pe_years,
       (SELECT count(*) FROM jsonb_array_elements(w.metrics_json->'years') y
         WHERE y->>'data_status' = 'missing'
           AND ((y->>'revenue') IS NOT NULL OR (y->>'net_profit') IS NOT NULL))
                                                                         AS filled_missing_years,
       (w.metrics_json->'quality'->'missing_years')                      AS missing_years,
       (w.metrics_json->'window'->>'fiscal_years')::int                  AS window_years
FROM dws.stock_agent_analysis_wide w
WHERE w.symbol = :'symbol';
-- 期望：year_rows = fiscal_years = window_years；
--       missing_year_key = 0；filled_missing_years = 0；negative_pe_years = 0；
--       zero_revenue_years 只允许与真实 0 收入一致（需与 R1 交叉确认）
```

### 7.4 判定与报告

- 单股全部 `verdict='OK%'` 且 R1b/R1c/R3/R3b 返回 0 行 → **PASS**。
- 任一 `FAIL` → 记录 `symbol + 指标 + wide 值 + src 值 + 差值`，按表回填对账报告；**不允许"人工调宽容差"**。
- 三只样本全 PASS 后，再抽 10 只随机股票跑 R5（只看结构不变量），作为回归护栏。
- 对账产物建议落一份对账报告，含：执行时间、`as_of`、样本、每项结果、偏差汇总、结论。

---

## 8. 待确认清单（待人类确认）

> **本节 12 项全部待人类确认**，实现方不得自行拍板。以下每项都需要产品/客户或上游决策，**本文不替其决定**。标注"本文默认"的项，是未确认前用于不阻塞实现的临时行为，确认后可能反转。

| 编号 | 事项 | 需谁决策 | 影响 | 本文默认（未确认时） |
| --- | --- | --- | --- | --- |
| **Q-01** | **PE 价格来源（G-07）**：构建时快照（A）还是查询时计算（B） | 客户 / 产品 | PE 是否落库、是否需要每日 `dws build`、MCP 返回是否可复现 | **A**（§4.4.3），满足 §4.4.3 三个条件之一时改 B。v1.1 已落**双口径**（`snapshot` + `reference`），默认 `snapshot`，无需再选存储形态 |
| **Q-02** | **ROE 分母数据源**：需上游在 `financial` 事件补 `amount_equity`（归母净资产），并把列加到 `dwd.stock_financial_snapshot` | 客户 + TickHub 上游 | ROE **完全不可计算**（当前 100% NULL） | ROE 恒 `null`，`roe_reason="equity_unavailable"` |
| **Q-03** | **PB 分母数据源**：需净资产或总股本；或由 quote 直接提供 `pb` | 客户 + TickHub 上游 | PB **完全不可计算**（当前 100% NULL） | 自算路径仍缺净资产；v1.1 已给 quote 加 `pb_mrq`，`reference` 口径透传，缺失即 `null` + `no_vendor_pb` |
| **Q-04** | `report_type` 枚举与年报识别；期间报告是否年化 | 上游（event-contract）+ 产品 | 年度口径正确性；年化会引入造数风险 | 年报白名单 `FY/ANNUAL/年报/A`；期间报告不年化，标 `report_basis="period"` |
| **Q-05** | 财年归属：用 `report_date` 年份，还是需要独立 `fiscal_year` 字段（3 月披露的 2025 年报） | 上游 + 产品 | 跨年错位会导致整条序列偏移一年 | `year = EXTRACT(year FROM report_date)` |
| **Q-06** | 是否在宽表落 YoY（同比） | 产品 | 增加一套缺失年/基期规则 | **不落**，由消费方自算 |
| **Q-07** | 风险摘要窗口与 `severity` 范围 | 产品 | `risk_summary` 内容与体积 | 滚动 3 年；统计四级、摘要 high+critical |
| **Q-08** | `latest_announcements` 的 N 与是否剔除 `bad` | 产品 | 宽表体积、AI 上下文成本 | N=20；不剔除，透传质量标记 |
| **Q-09** | 金额单位展示：存储恒为元，是否需要同时给万元/亿元字段 | 产品 / 前端 | 仅影响展示层，不改存储 | 只存元（4 位小数） |
| **Q-10** | 是否给宽表补 `quality_status` / `confidence` 顶层列（cli.md 要求，DDL 没有） | 架构 | 需新增迁移 0009；影响 `structured()` 的 Meta 返回 | 不加列，用 `metrics_json.quality` + 逐年字段 |
| **Q-11** | `docs/specs/cli.md` §3.9 宽表字段清单如何收敛（与 DDL 零同名） | 产品 / 文档负责人 | 文档与实现的一致性验收 | 以 DDL 为准，建议直接改写 cli.md §3.9 |
| **Q-12** | 合规开启时宽表 `compliance_tier` 的派生规则（全部一致 / 取最严格） | 客户（补充） | 开关开启时能否构建 | 全部来源一致才透传，不一致构建失败 |

**阻塞关系**：Q-02 / Q-03 决定 ROE、PB 是否需要在本期交付；Q-01 决定 PE 的存储形态；其余不阻塞"营收 + 净利润 + 风险 + 公告"四块的实现。

---

## 9. 实现方验收自检

实现 `fcoffer dws build` 时，逐条对照：

- [ ] 写出的 `metrics_json` 是 object，含 `years` 数组，年份降序、无重复；
- [ ] 每个 `year` 是整数，5 个指标键名与 `STRUCTURED_METRICS` 完全一致；
- [ ] 金额是 4 位小数的字符串，未经过 float；
- [ ] 逐年带 `report_date` / `report_type` / `version` 溯源字段；
- [ ] 缺失年份是显式 `null` + `data_status="missing"`，没有补 0 / 插值；
- [ ] `quality_status='bad'` 的年份照常写入并打标；
- [ ] `pe` 永不为负；无价无盈利时为 `null` 且有 `pe_reason`；
- [ ] `risk_summary.counts.total` = 四级之和，`highlights` ≤ 10 条且只含 high/critical；
- [ ] `latest_announcements` ≤ 20 条，`ann_date DESC, id DESC`，字段白名单；
- [ ] `as_of` 为 UTC 00:00，`--as-of` 可覆盖且可复现；
- [ ] 同输入 + 同 `as_of` 连跑两次，三个 JSONB 的 `md5` 相同；
- [ ] `built_at` 每次变化且不参与确定性比较；
- [ ] 合规开关关闭时两列为 `NULL`，开启时按 §5.5 处理；
- [ ] 单股失败不影响其他股，最终输出 `built/skipped/failed` 计数；
- [ ] 能通过 §7.3 的 R1–R5。

---

## 10. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-27 | v1.0 | 首版：字段映射、口径定义、NULL 规则、幂等与重算、对账 SQL 草案、12 项待确认 |
| 2026-10-30 | v1.0（迁移版） | 从原任务文档迁入 `docs/specs/dws-metrics.md`：剥离内部任务编号/作者/机器路径，增补迁移说明与交叉引用；口径、SQL 与 §8 未改动；M-01/M-02/M-04 标注已收敛 |
| 2026-10-30 | v1.1.1 | §4.1.1 合入上游 `UNIT-AUDIT.md` §9 草案：`total_market_cap` 为基本货币单位且消费端不再换算、`pb_mrq` 仅 `>0`、`amount_net_profit_deducted` 不得用归母顶替、`biz_key` 代次标记 `\|g2`、无币种列的已知限制与精度要求 |
| 2026-10-31 | v1.1.2 | 定稿（上游 `UNIT-AUDIT.md` §9.4 / §9.5）：§4.1「金额单位」行写明**口径只适用 A 股**、US/HK 缺币种列时 `reference`/`snapshot` 一律 `null` + 原因码；§4.1.1 补 DWS 市场闸（`_build_one` 只接受 6 位数字 symbol，非 A 股进 `BuildReport.skipped` + `non_a_share_symbol`）与遗留宽表行的 `dws show` 表态规则。不改表、不改公式、不加 `currency`/`market` 列 |
