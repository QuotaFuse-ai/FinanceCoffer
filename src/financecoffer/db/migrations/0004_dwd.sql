-- 0004 dwd: versioned detail tables. Money is NUMERIC only (never FLOAT),
-- quality fields are passed through untouched, and history is append-only:
-- a correction inserts MAX(version)+1 instead of UPDATE.

-- Reject any UPDATE that touches business columns. Archiving may still flip
-- is_cold / minio_url / ingested_at on a historical row.
CREATE OR REPLACE FUNCTION dwd.forbid_history_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (to_jsonb(NEW) - 'is_cold' - 'minio_url' - 'ingested_at')
       IS DISTINCT FROM (to_jsonb(OLD) - 'is_cold' - 'minio_url' - 'ingested_at') THEN
        RAISE EXCEPTION
            'dwd.% is append-only: insert a new version instead of UPDATE', TG_TABLE_NAME
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

-- 4.2 financial snapshots: one row per report + version.
CREATE TABLE IF NOT EXISTS dwd.stock_financial_snapshot (
    symbol               VARCHAR(20)  NOT NULL,
    report_date          DATE         NOT NULL,
    report_type          VARCHAR(20)  NOT NULL,
    version              INTEGER      NOT NULL,
    amount_revenue       NUMERIC(20,4),
    amount_net_profit    NUMERIC(20,4),
    quality_status       VARCHAR(20)  NOT NULL,
    confidence           VARCHAR(20),
    balance_check_result JSONB,
    fetch_time           TIMESTAMPTZ  NOT NULL,
    is_cold              BOOLEAN      NOT NULL DEFAULT FALSE,
    minio_url            VARCHAR(500),
    event_id             VARCHAR(100) NOT NULL,
    provider             VARCHAR(50)  NOT NULL,
    ingested_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, report_date, report_type, version),
    CONSTRAINT uq_fin_snapshot_event UNIQUE (event_id),
    CONSTRAINT ck_fin_snapshot_version CHECK (version >= 1),
    CONSTRAINT ck_fin_snapshot_quality
        CHECK (quality_status IN ('normal', 'suspicious', 'bad'))
);

CREATE INDEX IF NOT EXISTS idx_fin_snapshot_symbol_date
    ON dwd.stock_financial_snapshot (symbol, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_fin_snapshot_symbol_type_version
    ON dwd.stock_financial_snapshot (symbol, report_type, version DESC);

DROP TRIGGER IF EXISTS trg_fin_snapshot_append_only ON dwd.stock_financial_snapshot;
CREATE TRIGGER trg_fin_snapshot_append_only
    BEFORE UPDATE ON dwd.stock_financial_snapshot
    FOR EACH ROW EXECUTE FUNCTION dwd.forbid_history_update();

-- 4.2 announcements: deduplicated by content_hash so chunks are never doubled.
CREATE TABLE IF NOT EXISTS dwd.stock_announcement (
    id             BIGSERIAL PRIMARY KEY,
    symbol         VARCHAR(20)  NOT NULL,
    ann_date       DATE         NOT NULL,
    ann_type       VARCHAR(50),
    title          TEXT,
    content_hash   VARCHAR(64)  NOT NULL,
    clean_content  TEXT,
    minio_url      VARCHAR(500),
    quality_status VARCHAR(20)  NOT NULL,
    confidence     VARCHAR(20),
    fetch_time     TIMESTAMPTZ  NOT NULL,
    is_cold        BOOLEAN      NOT NULL DEFAULT FALSE,
    event_id       VARCHAR(100) NOT NULL,
    provider       VARCHAR(50)  NOT NULL,
    ingested_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_ann_content_hash UNIQUE (content_hash),
    CONSTRAINT uq_ann_event UNIQUE (event_id),
    CONSTRAINT ck_ann_quality
        CHECK (quality_status IN ('normal', 'suspicious', 'bad')),
    CONSTRAINT ck_ann_content_hash CHECK (content_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_ann_symbol_date
    ON dwd.stock_announcement (symbol, ann_date DESC);
CREATE INDEX IF NOT EXISTS idx_ann_type_date
    ON dwd.stock_announcement (ann_type, ann_date DESC);

-- 4.2 kline: versioned by (symbol, trade_date, adjust) for upstream corrections.
CREATE TABLE IF NOT EXISTS dwd.stock_kline (
    symbol         VARCHAR(20)  NOT NULL,
    trade_date     DATE         NOT NULL,
    adjust         VARCHAR(20)  NOT NULL DEFAULT 'none',
    version        INTEGER      NOT NULL DEFAULT 1,
    open           NUMERIC(20,4),
    high           NUMERIC(20,4),
    low            NUMERIC(20,4),
    close          NUMERIC(20,4),
    volume         NUMERIC(24,4),
    amount         NUMERIC(24,4),
    quality_status VARCHAR(20)  NOT NULL,
    confidence     VARCHAR(20),
    fetch_time     TIMESTAMPTZ  NOT NULL,
    event_id       VARCHAR(100) NOT NULL,
    provider       VARCHAR(50)  NOT NULL,
    ingested_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trade_date, adjust, version),
    CONSTRAINT uq_kline_event UNIQUE (event_id),
    CONSTRAINT ck_kline_version CHECK (version >= 1),
    CONSTRAINT ck_kline_quality
        CHECK (quality_status IN ('normal', 'suspicious', 'bad')),
    CONSTRAINT ck_kline_high_low CHECK (high IS NULL OR low IS NULL OR high >= low)
);

CREATE INDEX IF NOT EXISTS idx_kline_symbol_date
    ON dwd.stock_kline (symbol, trade_date DESC);

DROP TRIGGER IF EXISTS trg_kline_append_only ON dwd.stock_kline;
CREATE TRIGGER trg_kline_append_only
    BEFORE UPDATE ON dwd.stock_kline
    FOR EACH ROW EXECUTE FUNCTION dwd.forbid_history_update();

-- 4.2 risk events: source for get_risk_history and the DWS risk summary.
CREATE TABLE IF NOT EXISTS dwd.stock_risk_event (
    id                      BIGSERIAL PRIMARY KEY,
    symbol                  VARCHAR(20)  NOT NULL,
    occur_date              DATE         NOT NULL,
    risk_type               VARCHAR(50)  NOT NULL,
    severity                VARCHAR(20)  NOT NULL,
    summary                 TEXT,
    source_announcement_ref VARCHAR(200),
    quality_status          VARCHAR(20)  NOT NULL,
    confidence              VARCHAR(20),
    fetch_time              TIMESTAMPTZ  NOT NULL,
    event_id                VARCHAR(100) NOT NULL,
    provider                VARCHAR(50)  NOT NULL,
    ingested_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_risk_event UNIQUE (event_id),
    CONSTRAINT ck_risk_severity
        CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    CONSTRAINT ck_risk_quality
        CHECK (quality_status IN ('normal', 'suspicious', 'bad'))
);

CREATE INDEX IF NOT EXISTS idx_risk_symbol_date
    ON dwd.stock_risk_event (symbol, occur_date DESC);
CREATE INDEX IF NOT EXISTS idx_risk_severity_date
    ON dwd.stock_risk_event (severity, occur_date DESC);

-- 4.2 quote snapshots: prices for PE pre-computation in DWS.
CREATE TABLE IF NOT EXISTS dwd.stock_quote_snapshot (
    symbol           VARCHAR(20)  NOT NULL,
    trade_date       DATE         NOT NULL,
    version          INTEGER      NOT NULL DEFAULT 1,
    close            NUMERIC(20,4),
    total_market_cap NUMERIC(24,4),
    pe_ttm           NUMERIC(20,4),
    quality_status   VARCHAR(20)  NOT NULL,
    confidence       VARCHAR(20),
    fetch_time       TIMESTAMPTZ  NOT NULL,
    event_id         VARCHAR(100) NOT NULL,
    provider         VARCHAR(50)  NOT NULL,
    ingested_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trade_date, version),
    CONSTRAINT uq_quote_event UNIQUE (event_id),
    CONSTRAINT ck_quote_version CHECK (version >= 1),
    CONSTRAINT ck_quote_quality
        CHECK (quality_status IN ('normal', 'suspicious', 'bad'))
);

CREATE INDEX IF NOT EXISTS idx_quote_symbol_date
    ON dwd.stock_quote_snapshot (symbol, trade_date DESC);

DROP TRIGGER IF EXISTS trg_quote_append_only ON dwd.stock_quote_snapshot;
CREATE TRIGGER trg_quote_append_only
    BEFORE UPDATE ON dwd.stock_quote_snapshot
    FOR EACH ROW EXECUTE FUNCTION dwd.forbid_history_update();

-- 3.6 research reports (event-contract addition): PDF stays as a reference.
CREATE TABLE IF NOT EXISTS dwd.stock_research_report (
    id             BIGSERIAL PRIMARY KEY,
    symbol         VARCHAR(20)  NOT NULL,
    report_id      VARCHAR(100),
    title          TEXT,
    institution    VARCHAR(100),
    analyst        VARCHAR(100),
    rating         VARCHAR(50),
    target_price   NUMERIC(20,4),
    publish_date   DATE         NOT NULL,
    pdf_url        VARCHAR(500),
    quality_status VARCHAR(20)  NOT NULL,
    confidence     VARCHAR(20),
    fetch_time     TIMESTAMPTZ  NOT NULL,
    event_id       VARCHAR(100) NOT NULL,
    provider       VARCHAR(50)  NOT NULL,
    ingested_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_research_event UNIQUE (event_id),
    CONSTRAINT ck_research_quality
        CHECK (quality_status IN ('normal', 'suspicious', 'bad'))
);

CREATE INDEX IF NOT EXISTS idx_research_symbol_date
    ON dwd.stock_research_report (symbol, publish_date DESC);

-- 3.7 northbound flows: market-level rows, no symbol.
CREATE TABLE IF NOT EXISTS dwd.northbound_flow (
    trade_date      DATE         NOT NULL,
    channel         VARCHAR(50)  NOT NULL,
    market          VARCHAR(10)  NOT NULL DEFAULT 'CN',
    net_buy_amount  NUMERIC(24,4),
    buy_amount      NUMERIC(24,4),
    sell_amount     NUMERIC(24,4),
    currency        VARCHAR(3)   NOT NULL DEFAULT 'CNY',
    quality_status  VARCHAR(20)  NOT NULL,
    confidence      VARCHAR(20),
    fetch_time      TIMESTAMPTZ  NOT NULL,
    event_id        VARCHAR(100) NOT NULL,
    provider        VARCHAR(50)  NOT NULL,
    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, channel),
    CONSTRAINT uq_northbound_event UNIQUE (event_id),
    CONSTRAINT ck_northbound_quality
        CHECK (quality_status IN ('normal', 'suspicious', 'bad'))
);

CREATE INDEX IF NOT EXISTS idx_northbound_date
    ON dwd.northbound_flow (trade_date DESC);
