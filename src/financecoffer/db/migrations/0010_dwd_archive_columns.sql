-- 0010 dwd archive columns: kline / quote / risk become archivable (T-012).
-- The archive trigger on the three versioned tables already permits writes to
-- is_cold / minio_url / ingested_at, but the columns itself were only present
-- on stock_financial_snapshot and stock_announcement. Adding nullable/defaulted
-- columns changes no existing value, so history stays byte-identical.
-- is_cold=false with minio_url NULL is the hot state.

ALTER TABLE dwd.stock_kline
    ADD COLUMN IF NOT EXISTS is_cold BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS minio_url VARCHAR(500);

ALTER TABLE dwd.stock_quote_snapshot
    ADD COLUMN IF NOT EXISTS is_cold BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS minio_url VARCHAR(500);

ALTER TABLE dwd.stock_risk_event
    ADD COLUMN IF NOT EXISTS is_cold BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS minio_url VARCHAR(500);

COMMENT ON COLUMN dwd.stock_kline.is_cold IS
    'Cold copy lives in MinIO; the row itself is never rewritten.';
COMMENT ON COLUMN dwd.stock_quote_snapshot.is_cold IS
    'Cold copy lives in MinIO; the row itself is never rewritten.';
COMMENT ON COLUMN dwd.stock_risk_event.is_cold IS
    'Cold copy lives in MinIO; summary may be cleared only for cold rows.';
