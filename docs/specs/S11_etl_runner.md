# Spec 11 — ETL Runner (`src/alphalens/etl/runner.py`)

**Status:** authored — not implemented.
**Format:** lightweight (L21).
**Locks:** D1 single file / two subcommands · D2 `r2_key` generated column · D3 sequential · D4 argparse · D5 `--limit`.

---

## Goal

The runner is the orchestrator that ties the already-built ETL modules into one
resumable pipeline. It exposes two subcommands:

- **`discover`** — reads the target companies from the `companies` table, lists
  their 10-K / 10-Q filings for 2022–2026 from EDGAR, and enqueues each into
  `filings` as `status='pending'`. Idempotent on `accession_number`. This fills
  the work queue (`filings` is otherwise empty).
- **`run`** — claims pending / retryable filings via the state machine and
  processes each one **sequentially**, end-to-end: download → R2 cache → section
  detection → chunking → embedding → upsert → mark `processed`. Resumable across
  crashes; retries and backoff are handled by the state machine.

No LLM/Groq is involved in ETL, so observability is the `ingestion_jobs` table
plus structured logs — **not Opik**. The runner owns orchestration only; all
domain logic lives in the existing modules it calls (`edgar`, `sections`,
`chunker`, `embeddings`, `upsert`, `state`).

---

## Prerequisite (run manually once, before first `discover`)

Per D2, `filings.r2_key` must become a **generated column** so discovery never
supplies it and the value can never drift. `filings` is currently empty, so this
is instant and lossless:

```sql
ALTER TABLE filings DROP COLUMN r2_key;
ALTER TABLE filings ADD COLUMN r2_key TEXT
  GENERATED ALWAYS AS ('filings/' || cik || '/' || accession_number || '.html') STORED;
```

Save this to `scripts/migrations/` for version control. Verify with `\d filings`.

---

## Function Signatures

```python
"""ETL runner: discover + run orchestration over the existing ETL modules."""
import argparse
from dataclasses import dataclass
from uuid import UUID

from asyncpg import Pool

from alphalens.config import Settings

# ---- result records (small; for logging + tests) ----------------------------

@dataclass(frozen=True, slots=True)
class DiscoverReport:
    """Outcome of one discover pass."""
    companies_scanned: int
    filings_found: int
    filings_enqueued: int   # newly inserted; ON CONFLICT DO NOTHING skips dupes

@dataclass(frozen=True, slots=True)
class RunReport:
    """Outcome of one run pass."""
    claimed: int
    processed: int
    failed: int

# ---- public orchestration ----------------------------------------------------

async def discover(settings: Settings, *, tickers: list[str] | None = None) -> DiscoverReport:
    """List 2022-2026 10-K/10-Q filings for target companies (from the companies
    table) and enqueue them as status='pending'. Idempotent on accession_number.
    Never writes r2_key. `tickers=None` means all seeded companies."""

async def run(settings: Settings, *, limit: int | None = None) -> RunReport:
    """Claim and sequentially process pending/retryable filings end-to-end.
    `limit` caps filings processed this invocation (None = all). Resumable."""

# ---- internal seams (underscore = private; shown for the contract) -----------

async def _process_one(filing_id: UUID, *, settings: Settings, pool: Pool) -> None:
    """Run one claimed filing through the full pipeline, recording each
    IngestionStep and routing failures through the state machine (backoff/retry)."""

async def _upload_html_to_r2(key: str, body: bytes, *, settings: Settings) -> None:
    """PUT filing HTML to R2 at `key` via aioboto3 (async S3-compatible client)."""

# ---- CLI (argparse, D4) ------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Parser with subcommands: `discover`; `run` (accepts `--limit N`, int)."""

def main(argv: list[str] | None = None) -> int:
    """Entrypoint: parse args, dispatch to discover/run via asyncio.run().
    Returns 0 on success, non-zero on failure.
    Wired as `python -m alphalens.etl.runner`."""
```

---

## Acceptance Criteria

1. `discover` reads CIKs from the `companies` table — does **not** hardcode the 10.
2. `discover` enqueues only `form_type in ('10-K', '10-Q')` with `filing_date`
   within 2022-01-01 … 2026-12-31.
3. `discover` is idempotent: a second pass inserts 0 new rows
   (`ON CONFLICT (accession_number) DO NOTHING`) and reports
   `filings_enqueued == 0`.
4. The `discover` INSERT does **not** reference `r2_key` (generated column).
5. `run` processes filings strictly one at a time (no concurrent tasks / no
   `asyncio.gather` over filings).
6. `run --limit 1` claims and fully processes exactly one filing, then stops.
7. A successfully processed filing ends at `status='processed'` and has ≥1 row in
   `chunks`, each with a non-null `embedding VECTOR(768)` and the correct
   `embedding_model_version` (`'jina-v3'` or `'nomic-embed-text-v1.5'`).
8. Each attempt writes an `ingestion_jobs` row with per-step status; failures
   route through the state machine — attempt count increments, backoff applies,
   and `status='failed'` is set only after `MAX_ATTEMPTS = 3`.
9. `run` is resumable: re-invoking after a crash claims only un-processed /
   retryable filings and never re-processes one already at `status='processed'`.
10. R2 upload targets the exact key in `filings.r2_key`; the object is retrievable
    at that key afterward.
11. CLI exits 0 on success, non-zero on any unhandled failure; `--help` lists both
    subcommands; unknown subcommand prints usage and exits non-zero.
12. `ruff` clean, `mypy --strict` clean, all unit + integration tests green.

---

## Gotchas

- **Never write `r2_key`.** It is a generated column — Postgres rejects any
  INSERT/UPDATE touching it. `run` *reads* the value from the claimed row to know
  the upload destination.
- **Single writer only (D3).** Do not run `discover`/`run` locally while a future
  GitHub Action runner is active. The state-machine claim is safe for one writer
  in v1, not for concurrent claimers.
- **Respect downstream caps.** Sequential keeps Neon pooled connections (max=5)
  and the Jina rate limit within budget. Do not parallelize without first
  revisiting pool size and embedding throttling.
- **Re-upload is safe.** A filing re-claimed after a partial failure re-uploads to
  the same deterministic R2 key, simply overwriting — no orphan objects.
