-- 0002 ops: heartbeat, idempotency log, consumer offsets, DLQ and archive jobs.
-- ops.schema_version is owned by the migration runner and already exists here.

-- Latest heartbeat per upstream source (event-contract §3.8). Heartbeats never
-- land in ODS/DWD; this table is the only trace they leave.
CREATE TABLE IF NOT EXISTS ops.source_heartbeat (
    source      VARCHAR(50) PRIMARY KEY,
    last_seen   TIMESTAMPTZ NOT NULL,
    status      VARCHAR(20) NOT NULL,
    items       BIGINT      NOT NULL DEFAULT 0,
    run_id      VARCHAR(100),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_source_heartbeat_status CHECK (status IN ('ok', 'partial', 'failed')),
    CONSTRAINT ck_source_heartbeat_items CHECK (items >= 0)
);

CREATE INDEX IF NOT EXISTS idx_source_heartbeat_last_seen
    ON ops.source_heartbeat (last_seen DESC);

-- At-least-once delivery is deduplicated here. `event_id` is the deterministic
-- sha256 of (provider|data_type|symbol|biz_key); the tuple unique constraint is
-- the equivalent natural key and also blocks a recomputed-id collision.
CREATE TABLE IF NOT EXISTS ops.etl_event_log (
    event_id     VARCHAR(100) PRIMARY KEY,
    provider     VARCHAR(50)  NOT NULL,
    data_type    VARCHAR(50)  NOT NULL,
    symbol       VARCHAR(20),
    biz_key      VARCHAR(200) NOT NULL,
    event_time   TIMESTAMPTZ,
    status       VARCHAR(20)  NOT NULL DEFAULT 'received',
    attempts     INTEGER      NOT NULL DEFAULT 0,
    error        TEXT,
    trace_id     VARCHAR(128),
    received_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    CONSTRAINT ck_etl_event_log_status CHECK (
        status IN ('received', 'processing', 'done', 'failed', 'dlq', 'duplicate')
    ),
    CONSTRAINT ck_etl_event_log_attempts CHECK (attempts >= 0),
    CONSTRAINT uq_etl_event_identity
        UNIQUE NULLS NOT DISTINCT (provider, data_type, symbol, biz_key)
);

CREATE INDEX IF NOT EXISTS idx_etl_event_log_status
    ON ops.etl_event_log (status, received_at);
CREATE INDEX IF NOT EXISTS idx_etl_event_log_type
    ON ops.etl_event_log (provider, data_type, received_at DESC);

-- Resume point per consumer group / stream / shard.
CREATE TABLE IF NOT EXISTS ops.etl_consumer_offset (
    consumer_group   VARCHAR(100) NOT NULL,
    stream           VARCHAR(100) NOT NULL,
    shard            VARCHAR(100) NOT NULL DEFAULT '0',
    last_event_id    VARCHAR(100),
    last_event_time  TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_group, stream, shard)
);

-- Dead letters: raw event + error + retry counter (event-contract §5).
CREATE TABLE IF NOT EXISTS ops.etl_dlq (
    id           BIGSERIAL PRIMARY KEY,
    event_id     VARCHAR(100),
    stream       VARCHAR(100) NOT NULL,
    entry_id     VARCHAR(100),
    reason       VARCHAR(100) NOT NULL,
    raw_event    JSONB        NOT NULL,
    error_detail TEXT,
    retry_count  INTEGER      NOT NULL DEFAULT 0,
    status       VARCHAR(20)  NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_etl_dlq_status CHECK (status IN ('pending', 'retried', 'purged')),
    CONSTRAINT ck_etl_dlq_retry_count CHECK (retry_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_etl_dlq_status_created
    ON ops.etl_dlq (status, created_at);
CREATE INDEX IF NOT EXISTS idx_etl_dlq_event_id
    ON ops.etl_dlq (event_id);

-- MinIO archive jobs (cold/heat split, spec §4.5), resumable by status.
CREATE TABLE IF NOT EXISTS ops.archive_job (
    id             BIGSERIAL PRIMARY KEY,
    table_name     VARCHAR(100) NOT NULL,
    symbol         VARCHAR(20),
    object_key     VARCHAR(500) NOT NULL,
    status         VARCHAR(20)  NOT NULL DEFAULT 'pending',
    rows_archived  BIGINT       NOT NULL DEFAULT 0,
    bytes_archived BIGINT       NOT NULL DEFAULT 0,
    attempts       INTEGER      NOT NULL DEFAULT 0,
    error          TEXT,
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_archive_job_status CHECK (
        status IN ('pending', 'running', 'done', 'failed')
    ),
    CONSTRAINT ck_archive_job_rows CHECK (rows_archived >= 0 AND bytes_archived >= 0)
);

CREATE INDEX IF NOT EXISTS idx_archive_job_status
    ON ops.archive_job (status, created_at);
CREATE INDEX IF NOT EXISTS idx_archive_job_symbol
    ON ops.archive_job (symbol, created_at DESC);
