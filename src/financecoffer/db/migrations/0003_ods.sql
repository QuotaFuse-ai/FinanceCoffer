-- 0003 ods: immutable raw events, monthly RANGE partitions on fetch_time.
-- Raw payloads are append-only; replay reads ODS and never re-crawls upstream.

CREATE TABLE IF NOT EXISTS ods.stock_raw_data (
    id              BIGSERIAL,
    event_id        VARCHAR(100) NOT NULL,
    data_type       VARCHAR(50)  NOT NULL,
    symbol          VARCHAR(20)  NOT NULL,
    market          VARCHAR(10),
    source          VARCHAR(50)  NOT NULL,
    biz_key         VARCHAR(200) NOT NULL,
    fetch_time      TIMESTAMPTZ  NOT NULL,
    request_params  JSONB,
    raw_json        JSONB,
    raw_file_path   VARCHAR(500),
    etl_status      VARCHAR(20)  NOT NULL DEFAULT 'pending',
    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (id, fetch_time),
    CONSTRAINT ck_ods_etl_status CHECK (etl_status IN ('pending', 'done', 'failed')),
    CONSTRAINT ck_ods_raw_payload CHECK (raw_json IS NOT NULL OR raw_file_path IS NOT NULL)
) PARTITION BY RANGE (fetch_time);

-- event_id is unique at the ops.etl_event_log level; here it is a lookup key
-- (a partitioned table cannot carry a unique index that omits fetch_time).
CREATE INDEX IF NOT EXISTS idx_ods_raw_event_id
    ON ods.stock_raw_data (event_id);
CREATE INDEX IF NOT EXISTS idx_ods_raw_symbol_time
    ON ods.stock_raw_data (symbol, fetch_time DESC);
CREATE INDEX IF NOT EXISTS idx_ods_raw_type_time
    ON ods.stock_raw_data (data_type, fetch_time DESC);
CREATE INDEX IF NOT EXISTS idx_ods_raw_status
    ON ods.stock_raw_data (etl_status, fetch_time);
