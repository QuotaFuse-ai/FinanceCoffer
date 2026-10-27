# FinanceCoffer 事件契约（Event Contract v1）

版本：v0.4（草案，待 TickHub 团队确认）
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
| `biz_key` | string | 是 | 业务主键，参与 `event_id` 计算：公告=公告 ID，财报=`report_date\|statement`，K 线=`trade_date\|period\|adjust`，行情=`trade_date\|ts`，研报=研报 ID，北向=`trade_date\|channel`，风险=`occur_date\|risk_type\|ref`，心跳=`run_id` |
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
| `compliance_tier` | string | 否 | 复用 TickHub-US 的 `S/B/C/D/X`；CN 数据统一记 `C`（DEC-0009） |
| `commercial_use` | bool | 否 | 是否允许商用；CN / US 数据事件均为 `false`（DEC-0006 / DEC-0009），D 级数据落库后查询端默认屏蔽 |

**ODS `source` 的判定顺序**（DEC-0017）：`source_info.hub` → `provider` 本身是 Hub 名（`heartbeat` 即如此）→ `provider` → Hub 映射表 → 兜底 `manual`。`provider` 是数据商，直接拿它当来源会把每一条真实事件都标成 `manual`。

### 2.2 发布端要求（上游必须满足）

1. **只发新行**：钩子必须挂在落盘之后，且只对本次真正新写入的记录发事件；重跑同一时间窗口不得重复推送。
2. **确定性 `event_id`**：同一业务事实的任何重发（outbox 重试、补跑、双实例）必须生成同一个 ID，由消费端幂等去重。
3. **失败隔离**：Redis 不可用时写本地 outbox（SQLite 表，含 `event_id`、`payload`、`attempts`、`next_retry_at`），后台有界重试；发布失败不得让采集任务失败。
4. **不阻塞采集**：发布是旁路，异步执行；单条发布超时（建议 2s）即转入 outbox。
5. **有界**：`XADD` 带 `MAXLEN ~ 100000`；outbox 积压与投递失败要有计数，能被监控发现。
6. **单实例**：外部调度器与常驻服务不得同时在跑，否则重复调用上游接口会触发限流。

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
      "amount_revenue": "123456789.0000",
      "amount_net_profit": "9876543.2100"
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
- `quality_status` 必填，只接受 `normal/suspicious/bad`；`bad` 仍落库但打标，不拒绝。
- 版本号由 FinanceCoffer 生成（`MAX(version)+1`），上游不传。

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
      "pe_ttm": "5.1200"
    },
    "quality_metrics": {"confidence": "high", "quality_status": "normal"}
  }
}
```

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
      "currency": "CNY"
    },
    "quality_metrics": {"confidence": "high", "quality_status": "normal"}
  }
}
```

规则：`symbol` 为 `null`（市场级指标）；`biz_key` = `trade_date|channel`。ODS 原样保留 `symbol=null`（DEC-0017），DWD `dwd.northbound_flow` 以 `(trade_date, channel)` 为键。

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
2. G-02：`financial` / `kline` / `quote` / `risk` 事件样例需 TickHub 团队确认字段口径。
3. G-10：已确认（DEC-0009）——CN 数据事件统一输出 `compliance_tier=C` 与 `commercial_use=false`，与 DEC-0006 的「个人研究级、仅内部使用」一致；`heartbeat` 不承载上游数据，维持只写 `provider`。
4. G-11：大文本走 MinIO 的引用格式（`minio://bucket/key` vs presigned URL）。
5. G-13：TickHub-CN 爬虫只覆盖 `announcements / reports / risks / northbound` 四类，`kline` 与 `financial_report` 归档表无写入方；财报与 K 线需要新增采集任务或 sidecar（见 `tickhub-integration.md`）。
6. G-14：已确认 `commercial_use=false`（DEC-0006）——数据可完整采集，每条事件记 `source_info.compliance_tier` 与 `commercial_use=false`，查询端按级门禁；若后续转对外商用需重新采集。覆盖面差异见 `tickhub-integration.md` §5.4。

## 8. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-26 | v0.1 | 首版：信封、五类事件、校验、重试 |
| 2026-10-26 | v0.2 | `event_id` 改为确定性哈希并新增 `biz_key`；补 `research` / `northbound` / `heartbeat`；补发布端要求、Stream 保留与 AOF；补 G-13/G-14 |
| 2026-10-27 | v0.3 | G-10 收口：CN 数据事件补 `compliance_tier=C` 与 `commercial_use=false`（DEC-0009）；可选字段，不升 `schema_version` |
| 2026-10-28 | v0.4 | 补 `source_info.hub` 与 ODS `source` 判定顺序（DEC-0017）；明确 ODS `symbol` 可空，市场级事件原样落 `null`；`heartbeat` 写明 upsert 语义、校验项与 backfill 排除 |
