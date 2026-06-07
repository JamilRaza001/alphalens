\# Spec S2 — db\_schema\_and\_seed

\## Goal

Bootstrap Neon Postgres with pgvector schema (4 tables, HNSW + GIN indexes) per v8 §6.1, seed 10 companies, and scaffold Claude Code context files (CLAUDE.md + PROJECT\_STATUS.md) per L23.

\## Files to Create

1\. \`CLAUDE.md\` (repo root) — Claude Code context anchor

2\. \`docs/PROJECT\_STATUS.md\` — current phase/spec status (single source of truth)

3\. \`scripts/create\_schema.sql\` — schema DDL

4\. \`scripts/seed\_companies.py\` — idempotent company seed (psycopg v3)

5\. \`scripts/verify\_s2.sh\` — verification script

\---

\## Task 0 — Context Files (do this FIRST, no DB needed)

\### \`CLAUDE.md\` (repo root)

Must contain:

\- Project one-liner: "AlphaLens v8 — Agentic RAG over SEC 10-K/10-Q filings"

\- Stack summary (5 lines max): Lambda + Neon pgvector + R2 + Groq LLaMA 3.3 70B + Jina v3 embeddings

\- Pointer: "Authoritative design doc: \`docs/design/AlphaLens\_v8.md\`"

\- Pointer: "Current status: \`docs/PROJECT\_STATUS.md\`"

\- Spec workflow rule: "Claude Code reads spec from \`docs/specs/Sn\_\*.md\`, runs in Plan mode, waits for user approval before execute"

\- Locked decisions list (L11–L23) abbreviated, one line each

\- Style rules: "English only in code/specs/commits. No legacy syntax. uv (not pip). Python 3.12 pinned."

\### \`docs/PROJECT\_STATUS.md\`

Single table tracking phase progress. Columns: Spec | Title | Status | Commit | Notes. Row for S1 (DONE, \`11b6a38\`), S2 (IN\_PROGRESS), S3–S5 (PENDING). Update at end of every spec.

\---

\## Task 1 — Schema SQL (\`scripts/create\_schema.sql\`)

Four tables (per v8 §6.1 — reconciled to live schema, decisions #1–#8):

```sql
-- Required extensions
CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector for embedding column
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

-- 1. companies — master list of 10 tickers
CREATE TABLE IF NOT EXISTS companies (
    cik TEXT PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    sector TEXT,
    sic_code TEXT,
    fiscal_year_end TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. filings — 10-K / 10-Q metadata, points to R2 object
CREATE TABLE IF NOT EXISTS filings (
    filing_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cik TEXT NOT NULL REFERENCES companies(cik) ON DELETE CASCADE,
    ticker TEXT NOT NULL,                          -- convenience column; cik is the FK
    filing_type TEXT NOT NULL CHECK (filing_type IN ('10-K', '10-Q')),
    filing_date DATE NOT NULL,
    period_end DATE NOT NULL,
    accession_number TEXT NOT NULL UNIQUE,         -- SEC's unique ID per filing
    r2_key TEXT NOT NULL,                          -- path in R2 bucket
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'processed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_filings_cik_date ON filings(cik, filing_date DESC);
CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings(ticker, filing_date DESC);

-- 3. chunks — text chunks with embedding + tsvector (the heart of RAG)
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id UUID NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,                  -- filing-global, 0-based, contiguous
    section TEXT,                                  -- e.g. "Item 1A", "MD&A"
    section_order INTEGER NOT NULL DEFAULT 0,      -- position in filing for ordering
    text TEXT NOT NULL,
    tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding VECTOR(768),                         -- Jina v3 truncated / nomic native
    embedding_model_version TEXT,                  -- 'jina-v3' | 'nomic-embed-text-v1.5'
    token_count INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, chunk_index)
);
-- HNSW for dense vector search (cosine — Jina/nomic produce normalized vectors)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
-- GIN for lexical search via ts_rank_cd (hybrid retrieval)
CREATE INDEX IF NOT EXISTS idx_chunks_tsv_gin ON chunks USING gin (tsv);

-- 4. ingestion_jobs — per-attempt ETL audit trail (see §8.2 two-level state model)
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id UUID NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed')),
    step TEXT CHECK (step IN ('download', 'parse', 'chunk', 'embed', 'upsert')),
    error TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- financial_facts + entities: planned for v2 (XBRL → financial_facts; Apache AGE KG → entities); not created in v1.
```

\---

\## Task 2 — Seed Companies (\`scripts/seed\_companies.py\`)

\### Function Signature

\`\`\`python

def seed\_companies(database\_url: str) -> int:

"""UPSERT 10 S&P top companies. Returns rows affected."""

\`\`\`

\### Required behavior

\- Use \*\*psycopg v3\*\* (\`psycopg\`, NOT \`psycopg2\` — psycopg2 is legacy)

\- Load \`DATABASE\_URL\` from \`.env\` via \`python-dotenv\`

\- Use \`ON CONFLICT (cik) DO UPDATE\` so script is idempotent (re-runnable)

\- Use \`executemany\` with a parameterized query (no f-string SQL — injection risk)

\- Print row count and exit 0 on success, non-zero on failure

\### Seed Data (10 companies, verified CIKs)

| Ticker | Name | CIK | Sector |

|--------|------|-----|--------|

| AAPL | Apple Inc. | 0000320193 | Technology |

| MSFT | Microsoft Corporation | 0000789019 | Technology |

| GOOGL | Alphabet Inc. | 0001652044 | Communication Services |

| AMZN | Amazon.com Inc. | 0001018724 | Consumer Discretionary |

| NVDA | NVIDIA Corporation | 0001045810 | Technology |

| META | Meta Platforms Inc. | 0001326801 | Communication Services |

| TSLA | Tesla Inc. | 0001318605 | Consumer Discretionary |

| BRK-B | Berkshire Hathaway Inc. | 0001067983 | Financials |

| JPM | JPMorgan Chase & Co. | 0000019617 | Financials |

| V | Visa Inc. | 0001403161 | Financials |

\---

\## Acceptance Criteria (15)

1\. \`CLAUDE.md\` exists at repo root, references design doc + status doc + L11–L23

2\. \`docs/PROJECT\_STATUS.md\` exists with S1 row marked DONE (commit \`11b6a38\`)

3\. \`scripts/create\_schema.sql\` exists and is committed

4\. \`scripts/seed\_companies.py\` exists and is committed

5\. Running schema script on Neon does not error; re-running is idempotent (no duplicate-key failures)

6\. \`vector\` and \`pgcrypto\` extensions exist in DB

7\. All 4 tables exist: \`companies\`, \`filings\`, \`chunks\`, \`ingestion\_jobs\`

8\. \`chunks.embedding\` column type is \`vector(768)\`

9\. \`chunks.tsv\` is a \`STORED\` generated column

10\. HNSW index \`idx\_chunks\_embedding\_hnsw\` exists with \`vector\_cosine\_ops\`

11\. GIN index \`idx\_chunks\_tsv\_gin\` exists on \`chunks.tsv\`

12\. FK constraints present: \`filings.cik → companies(cik)\`, \`chunks.filing\_id → filings(filing\_id)\`, \`ingestion\_jobs.filing\_id → filings(filing\_id)\`

13\. Seed script populates exactly 10 rows in \`companies\`; re-running keeps it at 10

14\. Verification script \`scripts/verify\_s2.sh\` passes all checks

15\. \`PROJECT\_STATUS.md\` updated with S2 = DONE + commit hash after merge

\---

\## Gotchas

1\. \*\*psycopg v3, not v2.\*\* Modern API is \`psycopg.connect()\`, not \`psycopg2.connect()\`. Add \`psycopg\[binary\]>=3.2\` via \`uv add psycopg\[binary\]\`.

2\. \*\*pgvector version on Neon.\*\* HNSW requires pgvector ≥ 0.5.0. Neon ships ≥ 0.7 — safe. If \`CREATE INDEX ... USING hnsw\` errors, that's the version check failing.

3\. \*\*Cosine vs L2 ops class.\*\* Jina v3 and nomic both return \*\*normalized\*\* vectors → \`vector\_cosine\_ops\` is correct. Using \`vector\_l2\_ops\` here would silently work but give different rankings. Don't mix.

4\. \*\*Generated tsvector column.\*\* Modern pattern (Postgres 12+) — no trigger needed. Older guides still teach the trigger approach; ignore them.

5\. \*\*CIK as TEXT not INTEGER.\*\* SEC CIKs are 10-digit zero-padded strings (\`0000320193\`). Storing as int loses the leading zeros.

6\. \*\*Path with spaces.\*\* Run scripts as \`bash "scripts/verify\_s2.sh"\` — your WSL path \`/mnt/c/MJR Work Space/...\` needs quoting.

7\. \*\*HNSW build time.\*\* Empty table = instant. Don't worry now; index builds lazily as rows insert. For 22k rows later, expect ~10–30s build.

8\. \*\*\`.env\` DATABASE\_URL format for Neon.\*\* Must include \`?sslmode=require\` suffix. Without SSL, Neon rejects connections.

9\. \*\*Pre-commit duplicate hook (carry-over from S1).\*\* Dedupe \`check-added-large-files\` in \`.pre-commit-config.yaml\` while you're touching the repo.

\---

\## Verification Script (\`scripts/verify\_s2.sh\`)

\`\`\`bash

#!/usr/bin/env bash

set -euo pipefail

\# Load DATABASE\_URL from .env

set -a; source .env; set +a

PSQL="psql $DATABASE\_URL -v ON\_ERROR\_STOP=1 -tAc"

echo "1. Extensions..."

$PSQL "SELECT extname FROM pg\_extension WHERE extname IN ('vector','pgcrypto');" | wc -l | grep -q '^2$'

echo "2. Tables..."

for t in companies filings chunks ingestion\_jobs; do

$PSQL "SELECT to\_regclass('public.$t');" | grep -q "^$t$"

done

echo "3. embedding column type..."

$PSQL "SELECT format\_type(atttypid, atttypmod) FROM pg\_attribute

WHERE attrelid='chunks'::regclass AND attname='embedding';" | grep -q 'vector(768)'

echo "4. HNSW index..."

$PSQL "SELECT indexdef FROM pg\_indexes WHERE indexname='idx\_chunks\_embedding\_hnsw';" | grep -q 'hnsw'

echo "5. GIN index..."

$PSQL "SELECT indexdef FROM pg\_indexes WHERE indexname='idx\_chunks\_tsv\_gin';" | grep -q 'gin'

echo "6. Seed count = 10..."

$PSQL "SELECT count(\*) FROM companies;" | grep -q '^10$'

echo "✅ S2 verification passed."

\`\`\`

\---

\## Execution Order (when phone unblocked)

1\. Fill \`DATABASE\_URL\` in \`.env\`

2\. \`psql "$DATABASE\_URL" -f scripts/create\_schema.sql\`

3\. \`uv run python scripts/seed\_companies.py\`

4\. \`bash scripts/verify\_s2.sh\`

5\. Commit + push, update \`PROJECT\_STATUS.md\` with new commit hash
