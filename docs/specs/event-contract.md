# FinanceCoffer 事件契约（Event Contract v1）

版本：v0.10（金额单位、成交量单位与字段代次已定稿，2026-10-31；其余待确认项见 §7）
适用范围：TickHub-CN / TickHub-US → FinanceCoffer 的唯一数据入口。
原则：事件即证据。FinanceCoffer 只做校验、落库、索引，不补算、不改写上游判定。

## 1. 传输层

| 项 | 值 |
| --- | --- |
| 默认载体 | Redis Streams |
| Stream | `fc:stockdata:events` |
| 消费组 | `fc-etl` |
| 死信 Stream | `fc:stockdata:dlq` |
| 备选载体 | Kafka（`FC_EVENT_CONSUMER=kafka`）、Webhook（`POST /v1/ingest/event`） |
| Stream 字段 | 单字段 `payload`，值为完整 Event JSON 字符串 |
| 保留策略 | `XADD MAXLEN ~ 100000`；Redis 必须开启 AOF，否则重启即丢事件 |
| 编码 | UTF-8 JSON，单条上限 8 MB（超出走 MinIO，事件内只传引用） |

投递语义：至少一次。消费端以 `event_id` 做幂等，写入 `ops.etl_event_log` 唯一索引。

## 2. 信封字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | string | 是 | 首版 `"1.0"`；不兼容变更升主版本 |
| `event_id` | string | 是 | 确定性生成：`evt_` + `sha256(provider\|data_type\|symbol\|biz_key)` 前 32 位十六进制；同一业务事实重发必须得到同一 ID |
| `biz_key` | string | 是 | 业务主键，参与 `event_id` 计算：公告=公告 ID，财报=`report_date\|statement`，K 线=`trade_date\|period\|adjust\|g2`，行情=`trade_date\|ts\|g2`，研报=研报 ID，北向=`trade_date\|channel`（**不加代次**，见 §2.3），风险=`occur_date\|risk_type\|ref`，心跳=`run_id`；尾缀代次见 §2.3 |
| `event_time` | string | 是 | RFC3339 UTC，**业务发生时间**（公告发布时间 / 成交时间 / 报告期），不是发送时间 |
| `data_type` | string | 是 | `financial` / `announcement` / `kline` / `quote` / `risk` / `research` / `northbound` / `heartbeat` |
| `symbol` | string | 是 | A 股 6 位代码；美股 `AAPL` / `BRK.B`；`heartbeat` 为 `null` |
| `market` | string | 是 | `CN` / `US` / `HK` |
| `source_info` | object | 是 | 见 2.1 |
| `content` | object | 是 | 见第 3 节按类型定义 |
| `trace_id` | string | 否 | 跨系统排查用 |

### 2.1 source_info

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `provider` | string | 是 | 上游数据商，例如 `eastmoney` / `cninfo` / `sec_edgar`；**不是** Hub 名 |
| `hub` | string | 否 | 发布该事件的 Hub：`tickhub-cn` / `tickhub-us`。ODS `source` 以此为准；缺省时消费端按 `provider` → Hub 映射推断 |
| `crawl_url` | string | 否 | 原始地址，落 ODS 便于回溯 |
| `crawl_time` | string | 否 | RFC3339 |
| `compliance_tier` | string | 否 | 复用 TickHub-US 的 `S/B/C/D/X`；CN 数据统一记 `C` |
| `commercial_use` | bool | 否 | 是否允许商用；CN / US 数据事件均为 `false`，D 级数据落库后查询端默认屏蔽 |

**ODS `source` 的判定顺序**：`source_info.hub` → `provider` 本身是 Hub 名（`heartbeat` 即如此）→ `provider` → Hub 映射表 → 兜底 `manual`。`provider` 是数据商，直接拿它当来源会把每一条真实事件都标成 `manual`。

### 2.2 发布端要求（上游必须满足）

1. **只发新行**：钩子必须挂在落盘之后，且只对本次真正新写入的记录发事件；重跑同一时间窗口不得重复推送。
2. **确定性 `event_id`**：同一业务事实的任何重发（outbox 重试、补跑、双实例）必须生成同一个 ID，由消费端幂等去重。
3. **失败隔离**：Redis 不可用时写本地 outbox（SQLite 表，含 `event_id`、`payload`、`attempts`、`next_retry_at`），后台有界重试；发布失败不得让采集任务失败。
4. **不阻塞采集**：发布是旁路，异步执行；单条发布超时（建议 2s）即转入 outbox。
5. **有界**：`XADD` 带 `MAXLEN ~ 100000`；outbox 积压与投递失败要有计数，能被监控发现。
6. **单实例**：外部调度器与常驻服务不得同时在跑，否则重复调用上游接口会触发限流。

### 2.3 键上的字段代次（定稿）

> §2.3 / §3.3 / §3.4.2 / §3.7 / §7 G-02 中标注「定稿」的代码块逐字取自上游
> `TickHub-CN:UNIT-AUDIT.md` §9.5（commit `83cc70e`）；
> 块内的 `§9.2` / `§9.4` 等编号指该文档的章节。

```text
  K 线   = trade_date|period|adjust|g2
  quote  = trade_date|ts|g2
  北向   = trade_date|channel              （该表 insert_once，换键无收益，见 §9.2）
  尾缀 g2 = 字段代次：市值为元 + pb_mrq + 扣非归母（quote/financial），或成交量由手改为股
            （kline）。代次是常量，不随运行时间变化；event_id 只哈希
            provider|data_type|symbol|biz_key，DWD 按 event_id 去重，所以单位/口径变化必须
            体现为键变化，否则修正会被当成重放丢弃。
```

代次只写在 `biz_key` 上，不是新字段：信封 §2 的 `biz_key` 仍是单值字符串。`g2` 之前的键（`trade_date|period|adjust`、`trade_date|ts`）属于上一代字段单位，见 §3.3.1。

## 3. 按类型的内容结构

### 3.1 `announcement`（文档原始样例）

```json
{
  "schema_version": "1.0",
  "event_id": "evt_uuid_001",
  "event_time": "2023-10-25T10:00:00Z",
  "data_type": "announcement",
  "symbol": "600000",
  "market": "CN",
  "source_info": {
    "provider": "eastmoney",
    "crawl_url": "http://...",
    "compliance_tier": "S"
  },
  "content": {
    "raw_html": "<html>...</html>",
    "clean_content": "清洗后的正文文本...",
    "structured_fields": {
      "ann_type": "解禁",
      "publish_date": "2023-10-24"
    },
    "quality_metrics": {
      "confidence": "high",
      "quality_status": "normal"
    }
  }
}
```

落库映射：

| 事件字段 | 目标 |
| --- | --- |
| `content.raw_html` / 原件 | ODS `raw_json` 或 MinIO `raw_file_path` |
| `content.clean_content` | `dwd.stock_announcement.clean_content`（> 8KB 立即转 MinIO） |
| `structured_fields.ann_type` / `publish_date` | `dwd.stock_announcement.ann_type` / `ann_date` |
| `quality_metrics.*` | 原样写入，不改写 |
| `clean_content` 切片 | `vector.announcement_chunk` |

### 3.2 `financial`

```json
{
  "schema_version": "1.0",
  "event_id": "evt_uuid_002",
  "event_time": "2023-10-25T10:00:00Z",
  "data_type": "financial",
  "symbol": "600000",
  "market": "CN",
  "source_info": {"provider": "eastmoney"},
  "content": {
    "raw_html": "<html>...</html>",
    "structured_fields": {
      "report_date": "2023-09-30",
      "report_type": "Q3",
      "statement": "income",
      "amount_revenue": "123456789.0000",
      "amount_net_profit": "9876543.2100",
      "amount_net_profit_deducted": "9123456.7800"
    },
    "quality_metrics": {
      "confidence": "high",
      "quality_status": "normal",
      "balance_check_result": {"assets": true, "liabilities": true}
    }
  }
}
```

规则：

- 金额一律以**字符串**传输，避免 JSON float 精度丢失；落库转 `NUMERIC(20,4)`。
- `amount_net_profit_deducted`（可选）是**年初至今累计的扣非净利润**，与上游披露口径一致，不做单季拆分、不做年化；FinanceCoffer 在构建估值时按季度差额还原单季值。缺失时落 `NULL`，**绝不**用 `amount_net_profit` 顶替。
- `quality_status` 必填，只接受 `normal/suspicious/bad`；`bad` 仍落库但打标，不拒绝。
- `statement` 可选，取值是上游原样的报表名（`balance` / `income` / `cashflow` 等），最多 20 字符；
 一次财报会按报表分别发布，只有利润表带金额。存档层把它作为业务键的一部分，每个报表各自一条版本链。
  上游不发时落库为空串，不推断、不补默认值。
- 版本号由 FinanceCoffer 生成（`MAX(version)+1`，按 `(symbol, report_date, report_type, statement)` 各自计数），上游不传。

### 3.3 `kline`

```json
{
  "data_type": "kline",
  "symbol": "600000",
  "content": {
    "structured_fields": {
      "trade_date": "2023-10-24",
      "open": "7.1200", "high": "7.3000",
      "low": "7.0500", "close": "7.2500",
      "volume": "12345600", "amount": "89000000.0000",
      "adjust": "none"
    },
    "quality_metrics": {"confidence": "high", "quality_status": "normal"}
  }
}
```

单位规则（定稿）：

```text
  volume  string  成交量，单位**股**。A 股由采集端按 1 手 = 100 股换算，US 本来就是股；
                  消费端不得再乘 100，也不得按行业惯例理解为手。
```

`volume` 是整数字符串，落 `dwd.stock_kline_daily.volume`（`NUMERIC(24,4)`，`db/models/dwd.py:148`）原值；FinanceCoffer 只存不算，DWS 没有任何公式消费 `volume`。

#### 3.3.1 存量单位分界（切流前为「手」）

`volume` 的单位切换**不能靠改旧行完成**，因此存量里存在两种单位：

- `dwd.stock_kline` 受 append-only 触发器保护，只放行 `is_cold` / `minio_url` / `ingested_at` 的更新（`src/financecoffer/db/migrations/0004_dwd.sql:7`）；
- 上游爬虫只回看 250 个交易日、历史日不重播 ⇒ 切流前的存量 `volume` 改不掉；
- `ingested_at` 会被归档流程翻动，不能作为分界；`version` 也不是分界——切流后同一天重播两次得到 v2/v3，两代都是「股」；
- **唯一可靠的分界**是 `event_id` 回连 `ods.stock_raw_data.biz_key`（`src/financecoffer/db/models/ods.py:57`）是否含 `|g2`。

硬规则：**切流前的 `dwd.stock_kline.volume` 单位是手；在存量重播 / 迁移完成之前，禁止任何跨切流边的平均、求和、同比**（跨边聚合必然混 1 与 100 两种刻度）。存量重播挂在后续迁移任务（需人类批准生产窗口）；完成前所有 K 线成交量只做单日展示，不进指标。

### 3.4 `quote`（文档未定义，为 PE 预计算补齐，见 G-05）

```json
{
  "data_type": "quote",
  "symbol": "600000",
  "content": {
    "structured_fields": {
      "trade_date": "2023-10-24",
      "close": "7.2500",
      "total_market_cap": "212000000000.0000",
      "pe_ttm": "5.1200",
      "pb_mrq": "0.8500"
    },
    "quality_metrics": {"confidence": "high", "quality_status": "normal"}
  }
}
```

- `pe_ttm` / `total_market_cap` 用于 `snapshot` 估值口径。
- `pb_mrq`（可选）是厂商原样的 MRQ 市净率，只做**透传**，供 `reference` 估值口径使用；FinanceCoffer 不用净资产自行计算，缺失即 `NULL`。

#### 3.4.1 金额单位、新字段与缺失语义（增补，逐字合入上游 `UNIT-AUDIT.md` §9）

```
quote.structured_fields
  total_market_cap  string 基本货币单位（A 股=元；US=美元；HK=港元）。厂商刻度差异在采集端
                           归一，消费端不得再乘任何系数。
  pb                string 厂商市净率原值（倍数）。契约不消费，仅为存档兼容保留。
  pb_mrq            string 最新报告季市净率（倍数），与 pb 同值。仅当 > 0 时出现；
                           0 / 负 / 缺失 ⇒ 字段缺席并进 quality_metrics.missing_fields。
financial.structured_fields
  amount_net_profit_deducted  string 扣非归母净利润，年初至今累计，基本货币单位。
                           缺失 ⇒ 字段缺席；消费端不得用 amount_net_profit（归母）顶替，
                           两者在存在非经常性损益时不等，顶替即一次静默的估值重述。
biz_key
  quote / financial 的键尾带字段代次标记（当前 `|g2`）。event_id 只哈希
  provider|data_type|symbol|biz_key，DWD append-only 且按 event_id 去重，因此字段单位/口径
  变化必须体现为键变化，否则修正会被当成重放丢弃。标记是常量，不随运行时间变化。
```

本节与 §3.2（`financial`）重复处，以本节字段语义为准；`financial.structured_fields` 的其余规则（字符串传输、`NUMERIC(20,4)`、`quality_status` 必填、`statement` 业务键）仍见 §3.2。

#### 3.4.2 币种列缺失的限制（定稿）

```text
  total_market_cap / close 为**基本货币单位**（A 股=元，US=美元，HK=港元），换算在采集端完成。
  structured_fields 里的 currency/market 仅供追溯：dwd.stock_quote_snapshot 没有这两列，
  非 A 股行的金额没有币种可依，不得参与任何估值或跨市场比较。
```

落库侧的对应约束（`dws-metrics.md` §4.1）：三张 DWD 表（`stock_quote_snapshot` / `stock_kline_daily` / `stock_financial_snapshot`）的金额列**只按 A 股口径理解为元**；本期不加 `currency` / `market` 列，扩表触发条件见本节末尾。这条限制不靠文档自觉执行——DWS 构建侧有市场闸，非 6 位数字的 symbol 一律跳过并记原因码 `non_a_share_symbol`（`fcoffer dws build` 报告、`cli.md` §3.9）。

扩表触发条件（满足任意一条就必须加 `currency CHAR(3) NOT NULL DEFAULT 'CNY'` 与 `market CHAR(2)`，不受本期约束）：任一 US/HK 标的进入 `reference`/`snapshot` 的符号域；任一消费方需要跨市场比较市值、成交额、PE/PB；对外接口或看板展示 US/HK 金额且不附带市场上下文；FinanceCoffer 把 `amount` / `volume` 纳入任何公式。

### 3.5 `risk`（文档未定义，为 `get_risk_history` 补齐，见 G-04）

```json
{
  "data_type": "risk",
  "symbol": "600000",
  "content": {
    "structured_fields": {
      "risk_type": "goodwill_impairment",
      "occur_date": "2023-04-28",
      "severity": "high",
      "summary": "计提商誉减值 3.2 亿元"
    },
    "source_announcement_ref": "dwd:stock_announcement:12345",
    "quality_metrics": {"confidence": "medium", "quality_status": "suspicious"}
  }
}
```

### 3.6 `research`（研报，TickHub-CN 归档已有）

```json
{
  "schema_version": "1.0",
  "event_id": "evt_5f1c...",
  "event_time": "2026-10-24T08:00:00Z",
  "data_type": "research",
  "symbol": "600000",
  "market": "CN",
  "source_info": {"provider": "eastmoney", "crawl_url": "https://..."},
  "content": {
    "structured_fields": {
      "report_id": "R20261024001",
      "title": "三季度点评：息差企稳",
      "institution": "某证券",
      "analyst": "张三",
      "rating": "买入",
      "target_price": "12.5000",
      "publish_date": "2026-10-24",
      "pdf_url": "https://..."
    },
    "quality_metrics": {"confidence": "medium", "quality_status": "normal"}
  }
}
```

规则：`pdf_url` 只存引用，FinanceCoffer 不代下载；若上游已上传 MinIO，改传 `object_ref`。
`publish_date` 是必填项（`dwd.stock_research_report` 上该列 NOT NULL）：缺失即按 `reason="schema"` 拒绝，不拿 `event_time` 顶替——补算日期属于「只存不算」的禁区。

### 3.7 `northbound`（北向资金，TickHub-CN 归档已有）

```json
{
  "schema_version": "1.0",
  "event_id": "evt_9a2b...",
  "event_time": "2026-10-24T08:00:00Z",
  "data_type": "northbound",
  "symbol": null,
  "market": "CN",
  "source_info": {"provider": "eastmoney_datacenter"},
  "content": {
    "structured_fields": {
      "trade_date": "2026-10-24",
      "channel": "沪股通",
      "net_buy_amount": "123456789.0000",
      "buy_amount": "987654321.0000",
      "sell_amount": "864197532.0000",
      "deal_amount": "1851851853.0000",
      "currency": "CNY"
    },
    "quality_metrics": {"confidence": "high", "quality_status": "normal"}
  }
}
```

规则：`symbol` 为 `null`（市场级指标）；`biz_key` = `trade_date|channel`。ODS 原样保留 `symbol=null`，DWD `dwd.northbound_flow` 以 `(trade_date, channel)` 为键。

字段口径（定稿）：

```text
  net_buy_amount string  当日净买入额，单位**元（CNY）**。上游 2024-08-19 起停止披露时字段
                         缺席，不补 0、不外推。
  deal_amount    string  当日成交额，单位**元（CNY）**。旧名 deal_amount_wan（万元）作废，
                         不再发送。
  currency       string  固定 "CNY"。
  biz_key        trade_date|channel
  消费端注意：dwd.northbound_flow 以 (trade_date, channel) 插入一次、不可变；同一天换单位后
  重新发布的事件不会覆盖旧行。
```

- `deal_amount` **没有 FinanceCoffer 消费方**：除本契约文档外全仓无该键（改名核对时 `git grep deal_amount` 零命中），`NorthboundFields` 是 `extra="allow"`（`src/financecoffer/events/models.py:263`），`dwd.northbound_flow` 无此列（只有 `net_buy_amount` / `buy_amount` / `sell_amount` / `currency`，`db/models/dwd.py:281-284`）——它只进 ODS 存档，后续读到该字段时不要假设它有落库位置。
- `net_buy_amount` 落 `dwd.northbound_flow.net_buy_amount`；单位由万元改为元**不换 `biz_key`**（该表 `insert_once`，换键只会在 ODS 留下同日两条不同量级的事件，DWD 仍只认第一条，等于制造双份真相而不是修正，见 §9.2）。新日期自然用新单位；存量的盘点与定向修数属生产写操作，本期不做。

### 3.8 `heartbeat`（上游存活信号）

```json
{
  "schema_version": "1.0",
  "event_id": "evt_3c7d...",
  "event_time": "2026-10-26T07:35:12Z",
  "data_type": "heartbeat",
  "symbol": null,
  "market": "CN",
  "source_info": {"provider": "tickhub-cn"},
  "content": {
    "structured_fields": {
      "run_id": "cn-20261026-1535",
      "status": "ok",
      "items_collected": 128,
      "started_at": "2026-10-26T07:30:00Z",
      "finished_at": "2026-10-26T07:35:10Z",
      "next_run_at": "2026-10-26T13:00:00Z"
    },
    "quality_metrics": {"confidence": "high", "quality_status": "normal"}
  }
}
```

规则：

- `status` 取 `ok` / `partial` / `failed`；即使 `failed` 也必须发，否则无法区分「没在跑」与「跑了但没数据」。
- `biz_key` = `run_id`；同一轮重发得到同一 `event_id`。
- 消费端以 `source` 为键 upsert `ops.source_heartbeat(source, last_seen, status, items, run_id)`，暴露 `fc_source_last_seen_seconds`；超过 2 个采集周期未更新即告警。
- `heartbeat` 不落 ODS，不参与 DWD/DWS；`fcoffer ingest backfill` 也不回放它（回放的是数据，不是存活信号）。
- `status` 与 `items_collected` 都参与校验：`status` 只接受三个枚举值，`items_collected` 不得为负；`started_at` / `finished_at` / `next_run_at` 统一归一到 UTC。

## 4. 校验与拒绝规则

| 规则 | 失败处理 |
| --- | --- |
| `schema_version` 不认识 | 直接进 DLQ，不猜测 |
| 缺 `event_id` / `symbol` / `data_type` | 拒绝，计 `fc_dirty_data_total{reason="schema"}`（`heartbeat` / `northbound` 允许 `symbol=null`） |
| `event_id` 与 `sha256(provider\|data_type\|symbol\|biz_key)` 重算不一致 | 拒绝，计 `fc_dirty_data_total{reason="event_id"}` |
| `data_type` 不在枚举内 | 拒绝 |
| 金额字段为 JSON number | 拒绝，要求字符串（防精度丢失） |
| `quality_status` 不在枚举内 | 拒绝，不回退默认值 |
| 单条 > 8 MB 且未提供对象引用 | 拒绝，提示上游先上传 MinIO |
| `event_id` 已处理 | 跳过，返回 `duplicate`，不报错 |

禁止行为：不得替上游补 `quality_status`、不得重算勾稽、不得修改 `raw_json`。

## 5. 重试与死信

- 处理失败重试 3 次（指数退避 1s/5s/25s）。
- 仍失败写入 `fc:stockdata:dlq`，并落 `ops.etl_dlq`（原始事件 + 错误栈 + 重试次数）。
- `fcoffer etl dlq list|show|retry|purge` 管理；`retry` 重放原事件，`event_id` 幂等保证不重复。
- 消费位点存 `ops.etl_consumer_offset`，进程重启从位点续跑。

## 6. 版本兼容

- 新增可选字段：不升版本。
- 新增 `data_type`：升次版本，旧消费者进 DLQ 而不是崩溃。
- 字段改名/删除、语义变化：升主版本，双写过渡期由上游决定。

## 7. 待确认

1. G-01：业务方已确认走 A（TickHub 侧发布、FinanceCoffer 只消费），待 TickHub 侧排期与实现。
2. G-02：`kline` / `northbound` 的**单位口径已定稿**（定稿）：

   ```text
     kline 成交量单位已定：股（1 手 = 100 股）；northbound 金额单位已定：元。两项均为上游定稿
     产品定稿（2026-10-31），TickHub 侧按此实现，契约不再保留「待确认」。
   ```

   落库侧对应 §3.3（`volume`）、§3.3.1（存量分界）、§3.7（`net_buy_amount` / `deal_amount`）。`financial` / `quote` / `risk` 事件样例中**除单位以外**的字段口径仍待 TickHub 侧确认。
3. G-10：已确认——CN 数据事件统一输出 `compliance_tier=C` 与 `commercial_use=false`，与的「个人研究级、仅内部使用」一致；`heartbeat` 不承载上游数据，维持只写 `provider`。
4. G-11：大文本走 MinIO 的引用格式（`minio://bucket/key` vs presigned URL）。
5. G-13：已关闭——上游已补齐 `kline` 与 `financial` 采集（`TickHub-CN: src/tickhub_cn/crawler/store.py:395`、`:448`；`TickHub-US: us_collector/publisher.py:49-51` 的 `DATA_TYPES` 声明 8 类），财报与 K 线无需另行新增采集任务。
6. G-14：已确认 `commercial_use=false`——数据可完整采集，每条事件记 `source_info.compliance_tier` 与 `commercial_use=false`，查询端按级门禁；若后续转对外商用需重新采集。覆盖面差异见 `tickhub-integration.md` §5.4。

## 8. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-26 | v0.1 | 首版：信封、五类事件、校验、重试 |
| 2026-10-26 | v0.2 | `event_id` 改为确定性哈希并新增 `biz_key`；补 `research` / `northbound` / `heartbeat`；补发布端要求、Stream 保留与 AOF；补 G-13/G-14 |
| 2026-10-27 | v0.3 | G-10 收口：CN 数据事件补 `compliance_tier=C` 与 `commercial_use=false`；可选字段，不升 `schema_version` |
| 2026-10-28 | v0.4 | 补 `source_info.hub` 与 ODS `source` 判定顺序；明确 ODS `symbol` 可空，市场级事件原样落 `null`；`heartbeat` 写明 upsert 语义、校验项与 backfill 排除 |
| 2026-10-28 | v0.5 | `financial.structured_fields` 补可选 `statement`（报表名），版本链按报表切分；可选字段，不升 `schema_version` |
| 2026-10-29 | v0.6 | 版本行与 §7 G-02 的外部协作方表述统一为「TickHub 侧」（公开仓库规则：`docs/` 不出现团队称谓） |
| 2026-10-30 | v0.7 | §7 G-13 关闭：上游已补齐 `kline` 与 `financial` 采集（`TickHub-CN: src/tickhub_cn/crawler/store.py:395`、`:448`；`TickHub-US: us_collector/publisher.py:49-51` 声明 8 类 `data_type`），「CN 无 kline / 财报写入方」的假设不再成立 |
| 2026-10-30 | v0.8 | `financial` 补可选 `amount_net_profit_deducted`（累计扣非净利润），`quote` 补可选 `pb_mrq`（厂商 PB 透传），支撑双口径估值；均为可选字段，不升 `schema_version` |
| 2026-10-30 | v0.9 | §3.4.1 合入上游 `UNIT-AUDIT.md` §9 草案：写清 `total_market_cap` 为基本货币单位（采集端归一、消费端不再换算）、`pb_mrq` 仅 `>0` 才出现、`amount_net_profit_deducted` 缺失不得用归母顶替、`biz_key` 尾带字段代次标记 `|g2` |
| 2026-10-31 | v0.10 | 定稿（上游 `UNIT-AUDIT.md` §9.4 / §9.5，commit `83cc70e`）逐字合入：§2 表与新增 §2.3 写明 `kline`=`trade_date\|period\|adjust\|g2`、`quote`=`trade_date\|ts\|g2`、北向不加代次，代次是常量、不随运行时间变化；新增 §3.3 单位规则 `volume` 为**股**（A 股 1 手 = 100 股，US 本来就是股）；新增 §3.3.1 存量分界——切流前 `dwd.stock_kline.volume` 单位是手，存量重播/迁移完成前禁止任何跨切流边的平均、求和、同比；新增 §3.4.2 写明 `total_market_cap`/`close` 为基本货币单位、DWD 无 `currency`/`market` 列、非 A 股金额不得进估值，并给出扩表触发条件；§3.7 `net_buy_amount` 定为元、新增 `deal_amount`（元；旧名 `deal_amount_wan` 作废，且该字段无 FC 消费方）、补 `(trade_date, channel)` 插入一次且不可变的消费端提示；§7 G-02 的单位口径移除「待确认」。版本号承接 `origin/main` 上已记录的 v0.9，本次增量记为 v0.10 |
