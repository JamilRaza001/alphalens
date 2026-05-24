-- AlphaLens v8 — Database Schema
-- Idempotent: safe to re-run (all DDL uses IF NOT EXISTS)
-- Requires: Neon Postgres with pgvector >= 0.5.0

-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector for embedding column
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

-- 1. companies — master list of 10 tickers
CREATE TABLE IF NOT EXISTS companies (
    ticker      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    cik         TEXT NOT NULL UNIQUE,  -- SEC Central Index Key, 10-digit zero-padded
    sector      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. filings — 10-K / 10-Q metadata, points to R2 object
CREATE TABLE IF NOT EXISTS filings (
    filing_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker           TEXT NOT NULL REFERENCES companies(ticker) ON DELETE CASCADE,
    filing_type      TEXT NOT NULL CHECK (filing_type IN ('10-K', '10-Q')),
    filing_date      DATE NOT NULL,
    period_end       DATE NOT NULL,
    accession_number TEXT NOT NULL UNIQUE,  -- SEC's unique ID per filing
    r2_key           TEXT NOT NULL,          -- path in R2 bucket
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'processing', 'processed', 'failed')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings(ticker, filing_date DESC);

-- 3. chunks — text chunks with embedding + tsvector (the heart of RAG)
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id              UUID NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    chunk_index            INTEGER NOT NULL,  -- order within filing
    section                TEXT,              -- e.g. "Item 1A", "MD&A"
    text                   TEXT NOT NULL,
    tsv                    TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding              VECTOR(768),       -- Jina v3 truncated / nomic native (L17)
    embedding_model_version TEXT,             -- 'jina-v3' | 'nomic-embed-text-v1.5' (L9)
    token_count            INTEGER,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, chunk_index)
);

-- HNSW for dense vector search (cosine — Jina/nomic produce normalized vectors) (L17)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- GIN for lexical keyword search via ts_rank_cd (hybrid retrieval)
CREATE INDEX IF NOT EXISTS idx_chunks_tsv_gin
    ON chunks USING gin (tsv);

-- 4. queries — observability / Opik feed
CREATE TABLE IF NOT EXISTS queries (
    query_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              TEXT,  -- nullable until auth wired
    question             TEXT NOT NULL,
    answer               TEXT,
    retrieved_chunk_ids  UUID[],
    latency_ms           INTEGER,
    tokens_used          INTEGER,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. ingestion_jobs — ETL state machine
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id    UUID NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'failed')),
    error        TEXT,
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
