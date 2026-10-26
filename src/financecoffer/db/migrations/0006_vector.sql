-- 0006 vector: announcement chunks, 1024-dim bge-m3 embeddings, HNSW + cosine.
-- Dimension is fixed by DEC-0005. Changing the model or dimension requires a
-- new table plus `fcoffer vector reindex`; never ALTER this column in place.

CREATE TABLE IF NOT EXISTS vector.announcement_chunk (
    id              BIGSERIAL PRIMARY KEY,
    symbol          VARCHAR(20)  NOT NULL,
    ann_date        DATE         NOT NULL,
    ann_type        VARCHAR(50),
    chunk_index     INTEGER      NOT NULL DEFAULT 0,
    chunk_text      TEXT         NOT NULL,
    token_count     INTEGER,
    embedding       vector(1024) NOT NULL,
    embedding_model VARCHAR(100) NOT NULL DEFAULT 'BAAI/bge-m3',
    embedded_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    dwd_ref_id      BIGINT REFERENCES dwd.stock_announcement (id) ON DELETE CASCADE,
    CONSTRAINT uq_ann_chunk_position UNIQUE (dwd_ref_id, chunk_index),
    CONSTRAINT ck_ann_chunk_index CHECK (chunk_index >= 0),
    CONSTRAINT ck_ann_chunk_tokens CHECK (token_count IS NULL OR token_count > 0)
);

-- Filter-first B-Tree: hybrid search narrows by symbol/date before distance.
CREATE INDEX IF NOT EXISTS idx_ann_chunk_filter
    ON vector.announcement_chunk (symbol, ann_date);
CREATE INDEX IF NOT EXISTS idx_ann_chunk_ref
    ON vector.announcement_chunk (dwd_ref_id);

CREATE INDEX IF NOT EXISTS idx_ann_chunk_embedding
    ON vector.announcement_chunk USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
