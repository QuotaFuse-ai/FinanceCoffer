-- 0013 valuation basis columns: carry the inputs both valuation bases need.
-- amount_net_profit_deducted comes from the income statement like the other
-- amounts (year-to-date); pb_mrq is the vendor ratio, passed through as-is.
-- Both are nullable, so existing rows and history stay byte-identical.

ALTER TABLE dwd.stock_financial_snapshot
    ADD COLUMN IF NOT EXISTS amount_net_profit_deducted NUMERIC(20, 4);

ALTER TABLE dwd.stock_quote_snapshot
    ADD COLUMN IF NOT EXISTS pb_mrq NUMERIC(20, 4);

COMMENT ON COLUMN dwd.stock_financial_snapshot.amount_net_profit_deducted IS
    'Deducted net profit, year-to-date. Never substituted by amount_net_profit.';
COMMENT ON COLUMN dwd.stock_quote_snapshot.pb_mrq IS
    'Vendor PB (MRQ) passed through for the reference basis; never derived.';
