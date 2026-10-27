-- 0011 ods: allow a null symbol on raw events.
-- Contract 3.7 northbound flow (and any future market-wide payload) carries
-- symbol=null. The raw layer stores exactly what upstream published, so it
-- must not invent a placeholder symbol just to satisfy a NOT NULL column.

ALTER TABLE ods.stock_raw_data ALTER COLUMN symbol DROP NOT NULL;
