"""baseline: live schema snapshot (S2 + S9 generated r2_key, S10 step CHECK).

Revision ID: m00
Revises:
Create Date: 2026-06-15

Squashed baseline reproducing the live Neon schema as of the S22 hardening batch.
Transcribed from ``pg_dump --schema-only`` of the production DB (PostgreSQL 17),
so it is the single source of truth — it absorbs the former
``scripts/migrations/m01_r2_key_generated.sql`` (generated ``r2_key``) and replaces
the stale ``scripts/create_schema.sql``.

The live DB is **stamped** at this revision (never upgraded into) — see the WSL
commands in docs/specs/S_review_S22_etl_hardening.md / the plan. Fresh databases
(e.g. the future CI throwaway Postgres) build their schema by running this upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "m00"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Extensions ────────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;")

    # ── companies (cik PK, ticker UNIQUE) ─────────────────────────────────────
    op.execute(
        """
        CREATE TABLE companies (
            ticker          text NOT NULL,
            name            text NOT NULL,
            cik             text NOT NULL,
            sector          text,
            created_at      timestamp with time zone DEFAULT now() NOT NULL,
            sic_code        text,
            fiscal_year_end text,
            CONSTRAINT companies_pkey PRIMARY KEY (cik),
            CONSTRAINT companies_ticker_key UNIQUE (ticker)
        );
        """
    )

    # ── filings (cik FK → companies; generated r2_key) ────────────────────────
    op.execute(
        """
        CREATE TABLE filings (
            filing_id        uuid DEFAULT gen_random_uuid() NOT NULL,
            ticker           text NOT NULL,
            filing_type      text NOT NULL,
            filing_date      date NOT NULL,
            period_end       date NOT NULL,
            accession_number text NOT NULL,
            status           text DEFAULT 'pending'::text NOT NULL,
            created_at       timestamp with time zone DEFAULT now() NOT NULL,
            cik              text NOT NULL,
            r2_key           text GENERATED ALWAYS AS (
                'filings/' || cik || '/' || accession_number || '.html'
            ) STORED,
            CONSTRAINT filings_pkey PRIMARY KEY (filing_id),
            CONSTRAINT filings_accession_number_key UNIQUE (accession_number),
            CONSTRAINT filings_filing_type_check
                CHECK (filing_type = ANY (ARRAY['10-K'::text, '10-Q'::text])),
            CONSTRAINT filings_status_check
                CHECK (status = ANY (ARRAY['pending'::text, 'processing'::text,
                                           'processed'::text, 'failed'::text])),
            CONSTRAINT filings_cik_fkey FOREIGN KEY (cik)
                REFERENCES companies(cik) ON DELETE CASCADE
        );
        """
    )
    op.execute("CREATE INDEX idx_filings_cik_date ON filings USING btree (cik, filing_date DESC);")
    op.execute(
        "CREATE INDEX idx_filings_ticker_date ON filings USING btree (ticker, filing_date DESC);"
    )

    # ── chunks (VECTOR(768) + tsv; HNSW + GIN) ────────────────────────────────
    op.execute(
        """
        CREATE TABLE chunks (
            chunk_id                uuid DEFAULT gen_random_uuid() NOT NULL,
            filing_id               uuid NOT NULL,
            chunk_index             integer NOT NULL,
            section                 text,
            text                    text NOT NULL,
            tsv                     tsvector GENERATED ALWAYS AS
                                        (to_tsvector('english'::regconfig, text)) STORED,
            embedding               vector(768),
            embedding_model_version text,
            token_count             integer,
            created_at              timestamp with time zone DEFAULT now() NOT NULL,
            section_order           integer DEFAULT 0 NOT NULL,
            metadata                jsonb DEFAULT '{}'::jsonb NOT NULL,
            CONSTRAINT chunks_pkey PRIMARY KEY (chunk_id),
            CONSTRAINT chunks_filing_id_chunk_index_key UNIQUE (filing_id, chunk_index),
            CONSTRAINT chunks_filing_id_fkey FOREIGN KEY (filing_id)
                REFERENCES filings(filing_id) ON DELETE CASCADE
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);"
    )
    op.execute("CREATE INDEX idx_chunks_tsv_gin ON chunks USING gin (tsv);")

    # ── ingestion_jobs (status + step CHECKs) ─────────────────────────────────
    op.execute(
        """
        CREATE TABLE ingestion_jobs (
            job_id       uuid DEFAULT gen_random_uuid() NOT NULL,
            filing_id    uuid NOT NULL,
            status       text DEFAULT 'queued'::text NOT NULL,
            error        text,
            started_at   timestamp with time zone,
            completed_at timestamp with time zone,
            created_at   timestamp with time zone DEFAULT now() NOT NULL,
            step         text,
            CONSTRAINT ingestion_jobs_pkey PRIMARY KEY (job_id),
            CONSTRAINT ingestion_jobs_status_check
                CHECK (status = ANY (ARRAY['queued'::text, 'running'::text,
                                           'done'::text, 'failed'::text])),
            CONSTRAINT ingestion_jobs_step_check
                CHECK (step = ANY (ARRAY['download'::text, 'parse'::text, 'chunk'::text,
                                         'embed'::text, 'upsert'::text])),
            CONSTRAINT ingestion_jobs_filing_id_fkey FOREIGN KEY (filing_id)
                REFERENCES filings(filing_id) ON DELETE CASCADE
        );
        """
    )

    # ── queries (observability / Opik feed) ───────────────────────────────────
    op.execute(
        """
        CREATE TABLE queries (
            query_id            uuid DEFAULT gen_random_uuid() NOT NULL,
            user_id             text,
            question            text NOT NULL,
            answer              text,
            retrieved_chunk_ids uuid[],
            latency_ms          integer,
            tokens_used         integer,
            created_at          timestamp with time zone DEFAULT now() NOT NULL,
            request_id          text,
            tickers             text[],
            intent              text,
            confidence          text,
            status              text DEFAULT 'ok'::text NOT NULL,
            error               text,
            opik_trace_id       text,
            metadata            jsonb DEFAULT '{}'::jsonb NOT NULL,
            CONSTRAINT queries_pkey PRIMARY KEY (query_id),
            CONSTRAINT queries_confidence_check
                CHECK (confidence = ANY (ARRAY['low'::text, 'high'::text])),
            CONSTRAINT queries_intent_check
                CHECK (intent = ANY (ARRAY['comparative'::text, 'temporal'::text,
                                           'factual'::text, 'qualitative'::text])),
            CONSTRAINT queries_status_check
                CHECK (status = ANY (ARRAY['ok'::text, 'degraded'::text, 'error'::text]))
        );
        """
    )
    op.execute("CREATE INDEX idx_queries_created ON queries USING btree (created_at DESC);")
    op.execute(
        "CREATE INDEX idx_queries_low_conf ON queries "
        "USING btree (created_at DESC) WHERE (confidence = 'low'::text);"
    )
    op.execute(
        "CREATE INDEX idx_queries_not_ok ON queries "
        "USING btree (created_at DESC) WHERE (status <> 'ok'::text);"
    )


def downgrade() -> None:
    # Reverse dependency order; extensions left in place (shared, low-risk).
    op.execute("DROP TABLE IF EXISTS queries;")
    op.execute("DROP TABLE IF EXISTS ingestion_jobs;")
    op.execute("DROP TABLE IF EXISTS chunks;")
    op.execute("DROP TABLE IF EXISTS filings;")
    op.execute("DROP TABLE IF EXISTS companies;")
