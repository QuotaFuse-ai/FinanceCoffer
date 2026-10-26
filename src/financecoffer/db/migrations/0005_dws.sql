-- 0005 dws: pre-computed wide table for AI queries (5 fiscal years + risk +
-- latest announcements). Rebuilt from DWD only; nothing writes back to DWD.

CREATE TABLE IF NOT EXISTS dws.stock_agent_analysis_wide (
    symbol               VARCHAR(20) PRIMARY KEY,
    as_of                TIMESTAMPTZ NOT NULL,
    metrics_json         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    risk_summary         JSONB,
    latest_announcements JSONB,
    fiscal_years         SMALLINT    NOT NULL DEFAULT 5,
    source_version       VARCHAR(50),
    built_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_dws_wide_fiscal_years CHECK (fiscal_years BETWEEN 1 AND 10),
    CONSTRAINT ck_dws_wide_metrics CHECK (jsonb_typeof(metrics_json) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_dws_wide_as_of
    ON dws.stock_agent_analysis_wide (as_of DESC);
