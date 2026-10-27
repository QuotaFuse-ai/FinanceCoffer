-- 0009 ods raw_text: keep the upstream bytes, not just the parsed JSON.
-- `raw_json` is JSONB, so PostgreSQL normalizes key order and whitespace; a
-- byte-for-byte `fcoffer ods show <id> --raw` needs the original text as sent.
-- The column is nullable because rows stored through `raw_file_path` never had
-- an inline payload.

ALTER TABLE ods.stock_raw_data
    ADD COLUMN IF NOT EXISTS raw_text TEXT;

COMMENT ON COLUMN ods.stock_raw_data.raw_text IS
    'Original upstream payload text; written once at landing, never rewritten.';
