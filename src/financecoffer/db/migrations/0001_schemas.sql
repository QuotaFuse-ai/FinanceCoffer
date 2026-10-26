-- 0001 schemas: one schema per warehouse layer plus extensions.
-- The `vector` extension hosts the 1024-dim embedding column in 0006
-- (DEC-0005: local BAAI/bge-m3, cosine/HNSW).

CREATE SCHEMA IF NOT EXISTS ods;
CREATE SCHEMA IF NOT EXISTS dwd;
CREATE SCHEMA IF NOT EXISTS dws;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS vector;

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
