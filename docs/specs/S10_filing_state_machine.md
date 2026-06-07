# S10 — Filing State Machine

**File:** `docs/specs/S10_filing_state_machine.md` · **Implements:** `src/alphalens/etl/state.py`
**Depends on:** S2 (schema: `filings`, `ingestion_jobs`), S3–S7 (ETL stages it tracks)
**Foundation:** reconcile decisions #1 (two-level state), #1b (`step` column), #3 (COUNT-based retry + app-level backoff).

---

## Goal

Own the **two-level state model** for filing ingestion and the **outer (filing-level) retry policy**. A filing has a coarse lifecycle (`filings.status`); each processing attempt is a row in `ingestion_jobs` with its own per-attempt status and the `step` it reached. This module exposes the functions the ETL driver calls to: open an attempt, record which stage is running, close an attempt as success or failure, and decide whether a failed filing is retryable. Retry count is **derived** (`COUNT(*)` of job rows, never a stored column) and backoff is **computed at runtime** from the last failed job's `completed_at` — the DB stores facts, this module derives policy.

This module does **not** run the stages themselves (download/parse/chunk/embed/upsert live in S3–S7) and does **not** own the inner per-call retries (that is tenacity inside S3/S6). It is the outer state + retry layer the driver wires around those stages.

---

## Function Signatures

```python
from enum import StrEnum
from uuid import UUID
import asyncpg


class FilingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class IngestionStep(StrEnum):
    DOWNLOAD = "download"   # EDGAR fetch + R2 cache (S3)
    PARSE = "parse"         # section detection   (S4)
    CHUNK = "chunk"         # chunking            (S5)
    EMBED = "embed"         # embeddings          (S6)
    UPSERT = "upsert"       # Neon upsert         (S7)


MAX_ATTEMPTS: int = 3


class MaxAttemptsReached(Exception):
    """Raised when start_attempt is called on a filing that already has >= MAX_ATTEMPTS jobs."""


async def attempt_count(conn: asyncpg.Connection, filing_id: UUID) -> int:
    """Return COUNT(*) of ingestion_jobs for the filing = attempts used so far."""


async def start_attempt(conn: asyncpg.Connection, filing_id: UUID) -> UUID:
    """Open a new attempt atomically: set filings.status='processing', insert an
    ingestion_jobs row (status='running', started_at=now(), step=NULL), return its job_id.
    Raise MaxAttemptsReached if attempt_count(filing_id) >= MAX_ATTEMPTS."""


async def record_step(conn: asyncpg.Connection, job_id: UUID, step: IngestionStep) -> None:
    """Set ingestion_jobs.step for the running job to the stage it has just entered."""


async def complete_attempt(conn: asyncpg.Connection, job_id: UUID) -> None:
    """Close a successful attempt atomically: job status='done', completed_at=now();
    parent filing status='processed'. (Parent filing_id is read from the job row.)"""


async def fail_attempt(
    conn: asyncpg.Connection, job_id: UUID, step: IngestionStep, error: str
) -> FilingStatus:
    """Close a failed attempt atomically: job status='failed', step=<failed stage>,
    error=<message>, completed_at=now(). Then decide the parent filing's fate:
    if attempt_count(filing_id) >= MAX_ATTEMPTS -> filings.status='failed';
    else leave filings.status='processing' (retryable). Return the resulting FilingStatus."""


def next_backoff(attempt: int, base_seconds: float = 2.0, max_seconds: float = 300.0) -> float:
    """Pure: exponential backoff before the next attempt =
    min(base_seconds * 2 ** (attempt - 1), max_seconds). attempt is 1-based."""


async def claim_retryable_filings(conn: asyncpg.Connection, limit: int = 10) -> list[UUID]:
    """Return up to `limit` filing_ids eligible to (re)process, for the batch driver:
      - status='pending' (never attempted), OR
      - status='processing' AND attempt_count < MAX_ATTEMPTS AND the last failed job's
        completed_at + next_backoff(attempt_count) <= now().
    Excludes 'processed' and 'failed'. Backoff eligibility is filtered app-side using
    next_backoff (no next_attempt_at column is read)."""
```

---

## Acceptance Criteria

1. `start_attempt` sets `filings.status='processing'` and inserts exactly one `ingestion_jobs` row with `status='running'`, `started_at=now()`, `step IS NULL`; returns its `job_id`. Both writes occur in one transaction.
2. `start_attempt` raises `MaxAttemptsReached` when `attempt_count(filing_id) >= 3` (no 4th attempt is ever opened).
3. `record_step` sets `ingestion_jobs.step` to the given value; invalid values are rejected by the DB CHECK (`ingestion_jobs_step_check`).
4. `complete_attempt` sets the job `status='done'`, `completed_at=now()`, and the parent filing `status='processed'`, atomically; `filing_id` is derived from the job row (not passed in).
5. `fail_attempt` sets job `status='failed'`, `step=<failed stage>`, `error=<message>`, `completed_at=now()`; then sets `filings.status='failed'` iff `attempt_count >= 3` after this row, else leaves `'processing'`; returns the resulting `FilingStatus`. Atomic.
6. `attempt_count` returns `COUNT(*)` of `ingestion_jobs WHERE filing_id = $1` — the single source of truth for retries used.
7. `next_backoff` is pure and deterministic: `next_backoff(1)=2.0`, `next_backoff(2)=4.0`, `next_backoff(3)=8.0`, capped at `max_seconds`.
8. `claim_retryable_filings` returns `pending` filings plus `processing` filings whose `attempt_count < 3` and whose last failed `completed_at + next_backoff(attempt_count) <= now()`; it never returns `processed` or `failed` filings.
9. **No `retry_count` and no `next_attempt_at` column is read or written anywhere** — retries come from `COUNT(*)`, backoff is computed at runtime (#3).
10. Every function that performs more than one write runs inside a single transaction (caller may pass a connection already in a transaction; functions must not open a nested/competing one — see Gotchas).

---

## Gotchas

- **Off-by-one on attempt count.** A job row is inserted at `start_attempt`, so during attempt *N* the count is *N*. The gate in `start_attempt` checks `COUNT < 3` **before** inserting (COUNT 0→attempt1, 1→2, 2→3 allowed, 3→reject). In `fail_attempt` the check is **after** the failing row exists, so `COUNT >= 3` means exhausted. Write the gate exactly as specified or retries will be off by one.
- **Backoff is policy, not data.** Never persist `next_attempt_at`/`retry_count`. Store only `completed_at` (a fact) and derive the wait via `next_backoff` at claim time. This keeps the count un-driftable and timing logic in one place (#3).
- **`step` is nullable and per-attempt.** A `queued`/just-`running` job has `step=NULL`; CHECK passes on NULL. `step` records the stage in flight (or the stage that failed) — it is diagnostics (`GROUP BY step` → "18 failed at embed = Jina quota"), not control flow.
- **Two retry layers, don't conflate.** Inner = tenacity on flaky API/network calls *within* a stage (lives in S3/S6, not here). Outer = this module: a new `ingestion_jobs` row per filing-level retry, ≤3, with backoff. Do not add inner retries here.
- **Transaction ownership.** The driver typically wraps a whole attempt; these functions take an `asyncpg.Connection` and assume the caller controls the transaction boundary. Use `async with conn.transaction():` only if the spec's atomicity for a single function can't be met by the caller's outer transaction — document which functions self-wrap.
