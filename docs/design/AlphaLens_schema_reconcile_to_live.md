# AlphaLens — Schema Reconcile to Live (Decisions #1–#8 Locked)

**Purpose:** Single source of truth for the pre-S10 schema/doc reconcile. Records the verified live schema, all eight locked decisions, the resulting target schema, and the execution order. This file supersedes the earlier partial draft (which predated locks #1b, #3, #4, #5, #6, #7, #8).

**Status:** All decisions DECIDED and LOCKED. Nothing applied yet — live DB, design doc, and specs are still untouched. Execution begins after this file is reviewed.

---

## 1. Reconcile Principle (how each mismatch was judged)

For each doc-vs-live mismatch, the better design *for the application* wins — not "live always wins." Live already has working, tested code (S2–S9), so the bar to *change live* is high; only genuinely-better changes clear it. Pure naming/type differences with no functional gain default to live (lowest churn, zero risk). The decision is always the user's; this file records the locked outcomes.

---

## 2. Verified LIVE Schema (current state, before migration)

Verified via `\d <table>` (S14). Connect with:
`psql "$(grep '^NEON_DIRECT_URL=' .env | cut -d= -f2-)" -c "\d <table>"` (never `source .env` — URLs contain `&`).

**companies** — PK = ticker; cik UNIQUE
`ticker TEXT PK | name TEXT NN | cik TEXT NN UNIQUE | sector TEXT | created_at TIMESTAMPTZ NN now()`

**filings** — PK = filing_id UUID; FK on ticker
`filing_id UUID PK gen_random_uuid() | ticker TEXT NN FK->companies(ticker) CASCADE | filing_type TEXT NN CHECK('10-K','10-Q') | filing_date DATE NN | period_end DATE NN | accession_number TEXT NN UNIQUE | r2_key TEXT NN | status TEXT NN DEFAULT 'pending' CHECK('pending','processing','processed','failed') | created_at TIMESTAMPTZ NN now()`
Index: `idx_filings_ticker_date(ticker, filing_date DESC)`

**chunks** — verified from S13
`chunk_id UUID PK | filing_id UUID FK->filings(filing_id) CASCADE | chunk_index INT | section TEXT | section_order INT NN DEFAULT 0 | text TEXT | token_count INT | embedding VECTOR(768) | embedding_model_version TEXT | metadata JSONB NN DEFAULT '{}' | tsv GENERATED | created_at`
UNIQUE(filing_id, chunk_index). HNSW + GIN(tsv) indexes.

**ingestion_jobs** — not in v8 doc; discovered S14
`job_id UUID PK gen_random_uuid() | filing_id UUID NN FK->filings(filing_id) CASCADE | status TEXT NN DEFAULT 'queued' CHECK('queued','running','done','failed') | error TEXT | started_at TIMESTAMPTZ | completed_at TIMESTAMPTZ | created_at TIMESTAMPTZ NN now()`

**financial_facts, entities** — DO NOT EXIST in live DB.

---

## 3. Locked Decisions (#1–#8)

| # | Decision | Locked Resolution | Why (one line) |
|---|---|---|---|
| 1 | Filing state model | **LIVE.** filings 4-state `status` + separate `ingestion_jobs`; doc follows | Separation of concerns (lifecycle vs per-attempt); clean retry/audit; less churn on durable entity |
| 1b | Step fail/stop tracking | **ENHANCE LIVE.** Add `step TEXT` + CHECK to `ingestion_jobs` | Per-stage diagnostics (`GROUP BY step`); belongs on jobs, not filings |
| 2 | ingestion_jobs table | **LIVE.** Keep it; doc adds it | Where attempts/errors/timing live (corollary of #1) |
| 3 | Retry tracking | **COUNT + app-level backoff.** No `retry_count`, no `next_attempt_at` columns | Rows = single source of truth; store facts, derive policy at runtime; single-owner batch ETL needs no shared DB state |
| 4 | companies PK | **FLIP to cik-PK.** cik = PK, ticker = UNIQUE; filings link by cik | cik is SEC's permanent identity; ticker is a mutable attribute (FB→META survives trivially) |
| 5 | sic_code + fiscal_year_end | **ADD now (Option A).** Add both to companies; keep `sector` | Cheap/free from EDGAR at seed; stable for megacaps; complements human-readable `sector` |
| 6 | primary_doc_url | **LIVE (Option B).** Drop; derive EDGAR index URL from cik + accession_number | Don't denormalize a derivable value — store the fact (accession), build the URL on demand |
| 7 | financial_facts / entities | **DEFER (Option B).** Do NOT create in v1; doc marks "planned v2" | v1 never reads them; expensive to populate (XBRL/KG); design finalizes in v2 — avoid speculative tables |
| 8 | Renames batch | **LIVE (Option B).** Doc follows live names/types | Mechanical, no functional gain; working tested code on live; live's `TEXT` + explicit `*_id` are the better choices |
| 9 | queries table | **KEPT in v1 (discovered live post-hoc).** Enhanced via additive ALTER with request_id, tickers, intent, confidence, status, error, opik_trace_id, metadata | Complements Opik, not duplicates. No FK — a query spans many filings/chunks. |

Note: Locked architecture in `AlphaLens_v8.md` §3 (L1–L21) is unchanged. This reconcile corrects the doc's *description* (§6 schema, §8.2 state machine) to match reality — it is not an architecture change.

---

## 4. TARGET Schema (after migrations)

**companies** — cik-PK (#4) + sic_code/fiscal_year_end (#5)
`cik TEXT PK | ticker TEXT NN UNIQUE | name TEXT NN | sector TEXT | sic_code TEXT | fiscal_year_end TEXT | created_at TIMESTAMPTZ NN now()`

**filings** — link by cik (#4); ticker KEPT as convenience column; primary_doc_url not added (#6)
`filing_id UUID PK gen_random_uuid() | cik TEXT NN FK->companies(cik) CASCADE | ticker TEXT NN | filing_type TEXT NN CHECK('10-K','10-Q') | filing_date DATE NN | period_end DATE NN | accession_number TEXT NN UNIQUE | r2_key TEXT NN | status TEXT NN DEFAULT 'pending' CHECK('pending','processing','processed','failed') | created_at TIMESTAMPTZ NN now()`
Indexes: keep `idx_filings_ticker_date(ticker, filing_date DESC)`; add an index on `cik` for the FK join.
> **Ticker (resolved — KEEP):** `cik` is the identity FK (#4); `ticker` stays as a denormalized convenience column (its old FK to companies is dropped, the `cik` FK replaces it). Tradeoff: if a ticker ever changes (rare for these megacaps), `filings.ticker` must be updated too or it goes stale — `cik` stays the source of truth regardless.

**chunks** — unchanged by these decisions
(as verified in §2; embedding VECTOR(768), HNSW + GIN, UNIQUE(filing_id, chunk_index))

**ingestion_jobs** — add step (#1b)
`job_id UUID PK gen_random_uuid() | filing_id UUID NN FK->filings(filing_id) CASCADE | status TEXT NN DEFAULT 'queued' CHECK('queued','running','done','failed') | step TEXT CHECK(step IN ('download','parse','chunk','embed','upsert')) | error TEXT | started_at TIMESTAMPTZ | completed_at TIMESTAMPTZ | created_at TIMESTAMPTZ NN now()`
> `step` is nullable (a `queued` job has not entered a stage yet; CHECK passes on NULL). Enum is **provisional** — confirm against S10's actual stage names; table is empty so adjusting the CHECK is trivial.

**financial_facts, entities** — NOT created (deferred to v2: XBRL → financial_facts; Apache AGE KG → entities).

---

## 5. State Model (two-level) and Retry (central to S10)

**Coarse lifecycle — `filings.status`:** `pending → processing → processed` / `failed`.
**Per-attempt — `ingestion_jobs.status`:** `queued → running → done` / `failed`.

- A filing in `processing` has 1+ ingestion_jobs rows. One failed job ≠ failed filing.
- filing → `failed` only after retries exhausted; → `processed` on success.

**Retry (#3) — derived, not stored:**
- Attempt count = `SELECT COUNT(*) FROM ingestion_jobs WHERE filing_id = $1`.
- Max-3 rule = `COUNT < 3`.
- Backoff computed app-level from the last failed job's `completed_at` + attempt number. No `next_attempt_at` column.
- Two retry layers: **inner** = tenacity on flaky API calls within a single attempt; **outer** = filing-level (new job row, ≤3, backoff) via COUNT.

---

## 6. What Changes Where

**Live DB migrations (only these 3 — additive, tables empty → safe):**
1. `ingestion_jobs.step` column + named CHECK constraint (#1b).
2. companies cik-PK flip; filings add `cik` FK + keep `ticker` as plain column (drop old ticker FK) (#4).
3. companies add `sic_code` + `fiscal_year_end` (#5).

**Doc-only (`AlphaLens_v8.md` — live already matches):**
- §6 → 5 live tables (companies, filings, chunks, ingestion_jobs, queries), cik-PK, step column, drop primary_doc_url (#6), mark financial_facts/entities "planned v2" (#7), live names/types (#8), queries table (#9).
- §8.2 → two-level state machine + retry-by-COUNT + app-level backoff (#1/#2/#3).
- §1 → history line `v8.1 patch`.
- grep-fix stale tokens: `state='indexed'` → `status='processed'`, `form_type` → `filing_type`, `period_of_report` → `period_end`, `r2_html_key` → `r2_key`, `id` → `filing_id`/`chunk_id`, VARCHAR → TEXT.

**Specs (`docs/specs/*`):** grep stale tokens + fix, especially `02_db_schema`.

---

## 7. Execution Order (one step, verify, next)

1. **This file** reviewed and committed (single source of truth).
2. **Live migrations** (the 3 above) — run each via `psql` in WSL, verify with `\d <table>` before the next.
3. **Doc reconcile** — `AlphaLens_v8.md` §6 + §8.2 + §1 + stale-token grep (Claude Code).
4. **Specs reconcile** — `docs/specs/*`, esp `02_db_schema` (Claude Code).
5. **Commit** — `docs:` / `chore:`, two-commit style, no chaining.
6. **Author + implement S10** on the reconciled foundation. S10's first acceptance criterion: confirm retry mechanism = job-row COUNT; finalize `step` enum to S10's actual stage names.
