-- 0012 dwd: keep the financial statement name and version per statement.
--
-- One report reaches us as several statements: balance sheet, income statement
-- and cash flow statement share a symbol, a report date and a report type but
-- are different facts, and only the income statement carries revenue and net
-- profit. Without a discriminator all three landed on the same business key, so
-- the second and third statement looked like corrections of the first
-- (version 2, 3) and a reader that takes the latest version read a row with no
-- amounts at all.
--
-- The column is NOT NULL with an empty default because rows ingested before this
-- migration were written without it: their values stay byte-identical and only
-- new rows carry the statement name. Empty means "the publisher did not say",
-- which is stored as-is rather than guessed.

ALTER TABLE dwd.stock_financial_snapshot
    ADD COLUMN IF NOT EXISTS statement VARCHAR(20) NOT NULL DEFAULT '';

ALTER TABLE dwd.stock_financial_snapshot
    DROP CONSTRAINT IF EXISTS stock_financial_snapshot_pkey;

ALTER TABLE dwd.stock_financial_snapshot
    ADD CONSTRAINT stock_financial_snapshot_pkey
    PRIMARY KEY (symbol, report_date, report_type, statement, version);

COMMENT ON COLUMN dwd.stock_financial_snapshot.statement IS
    'Upstream statement name (balance / income / cashflow); empty when none was published.';

CREATE INDEX IF NOT EXISTS idx_fin_snapshot_symbol_statement
    ON dwd.stock_financial_snapshot (symbol, statement, report_date DESC);
