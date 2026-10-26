-- 0008 compliance: G-11 marking columns, runtime-switched by FC_COMPLIANCE_ENABLED
-- (DEC-0010). Schema is static, the switch is not: with the switch off (default)
-- no write path fills these columns and no read path selects them, so every row
-- stays NULL. With it on, ingest writes source_info.compliance_tier and
-- source_info.commercial_use and the query layer returns the tier in meta.
-- NULL therefore means "not recorded", never "unknown tier"; an enabled ingest
-- rejects a source it cannot mark.

ALTER TABLE ods.stock_raw_data
    ADD COLUMN IF NOT EXISTS compliance_tier VARCHAR(1),
    ADD COLUMN IF NOT EXISTS commercial_use BOOLEAN;
ALTER TABLE ods.stock_raw_data
    ADD CONSTRAINT ck_ods_raw_compliance_tier
    CHECK (compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X'));

ALTER TABLE dwd.stock_financial_snapshot
    ADD COLUMN IF NOT EXISTS compliance_tier VARCHAR(1),
    ADD COLUMN IF NOT EXISTS commercial_use BOOLEAN;
ALTER TABLE dwd.stock_financial_snapshot
    ADD CONSTRAINT ck_fin_snapshot_compliance_tier
    CHECK (compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X'));

ALTER TABLE dwd.stock_announcement
    ADD COLUMN IF NOT EXISTS compliance_tier VARCHAR(1),
    ADD COLUMN IF NOT EXISTS commercial_use BOOLEAN;
ALTER TABLE dwd.stock_announcement
    ADD CONSTRAINT ck_ann_compliance_tier
    CHECK (compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X'));

ALTER TABLE dwd.stock_kline
    ADD COLUMN IF NOT EXISTS compliance_tier VARCHAR(1),
    ADD COLUMN IF NOT EXISTS commercial_use BOOLEAN;
ALTER TABLE dwd.stock_kline
    ADD CONSTRAINT ck_kline_compliance_tier
    CHECK (compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X'));

ALTER TABLE dwd.stock_risk_event
    ADD COLUMN IF NOT EXISTS compliance_tier VARCHAR(1),
    ADD COLUMN IF NOT EXISTS commercial_use BOOLEAN;
ALTER TABLE dwd.stock_risk_event
    ADD CONSTRAINT ck_risk_compliance_tier
    CHECK (compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X'));

ALTER TABLE dwd.stock_quote_snapshot
    ADD COLUMN IF NOT EXISTS compliance_tier VARCHAR(1),
    ADD COLUMN IF NOT EXISTS commercial_use BOOLEAN;
ALTER TABLE dwd.stock_quote_snapshot
    ADD CONSTRAINT ck_quote_compliance_tier
    CHECK (compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X'));

ALTER TABLE dwd.stock_research_report
    ADD COLUMN IF NOT EXISTS compliance_tier VARCHAR(1),
    ADD COLUMN IF NOT EXISTS commercial_use BOOLEAN;
ALTER TABLE dwd.stock_research_report
    ADD CONSTRAINT ck_report_compliance_tier
    CHECK (compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X'));

ALTER TABLE dwd.northbound_flow
    ADD COLUMN IF NOT EXISTS compliance_tier VARCHAR(1),
    ADD COLUMN IF NOT EXISTS commercial_use BOOLEAN;
ALTER TABLE dwd.northbound_flow
    ADD CONSTRAINT ck_northbound_compliance_tier
    CHECK (compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X'));

ALTER TABLE dws.stock_agent_analysis_wide
    ADD COLUMN IF NOT EXISTS compliance_tier VARCHAR(1),
    ADD COLUMN IF NOT EXISTS commercial_use BOOLEAN;
ALTER TABLE dws.stock_agent_analysis_wide
    ADD CONSTRAINT ck_dws_wide_compliance_tier
    CHECK (compliance_tier IS NULL OR compliance_tier IN ('S', 'B', 'C', 'D', 'X'));
