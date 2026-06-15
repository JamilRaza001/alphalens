"""AlphaLens v8 — Filing State Machine (S10).

Two-level state model for filing ingestion and the outer retry policy.
filings.status tracks the coarse lifecycle; ingestion_jobs rows record each attempt.
Retry count is derived (COUNT(*) of job rows, never a stored column).
Backoff is computed at runtime from the last failed job's completed_at.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

_log = logging.getLogger(__name__)


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
    DOWNLOAD = "download"  # EDGAR fetch + R2 cache (S3)
    PARSE = "parse"  # section detection   (S4)
    CHUNK = "chunk"  # chunking            (S5)
    EMBED = "embed"  # embeddings          (S6)
    UPSERT = "upsert"  # Neon upsert         (S7)


MAX_ATTEMPTS: int = 3


class MaxAttemptsReached(Exception):
    """Raised when start_attempt is called on a filing that already has >= MAX_ATTEMPTS jobs."""


async def attempt_count(conn: asyncpg.Connection[Any], filing_id: UUID) -> int:
    """Return COUNT(*) of ingestion_jobs for the filing = attempts used so far."""
    result: int = await conn.fetchval(
        "SELECT COUNT(*) FROM ingestion_jobs WHERE filing_id = $1",
        filing_id,
    )
    return result


async def start_attempt(conn: asyncpg.Connection[Any], filing_id: UUID) -> UUID:
    """Open a new attempt: check count BEFORE insert, raise MaxAttemptsReached if >= MAX_ATTEMPTS.

    Gate: COUNT 0 → attempt 1 allowed, 1 → 2, 2 → 3; 3 → MaxAttemptsReached.
    Sets filings.status='processing', inserts ingestion_jobs (status='running',
    started_at=now(), step=NULL), returns job_id.
    Caller owns the transaction boundary (no nested transaction opened here).
    """
    count = await attempt_count(conn, filing_id)
    if count >= MAX_ATTEMPTS:
        raise MaxAttemptsReached(
            f"filing {filing_id} already has {count} attempts (MAX_ATTEMPTS={MAX_ATTEMPTS})"
        )
    await conn.execute(
        "UPDATE filings SET status = 'processing' WHERE filing_id = $1",
        filing_id,
    )
    job_id: UUID = await conn.fetchval(
        "INSERT INTO ingestion_jobs (filing_id, status, started_at)"
        " VALUES ($1, 'running', now())"
        " RETURNING job_id",
        filing_id,
    )
    return job_id


async def record_step(conn: asyncpg.Connection[Any], job_id: UUID, step: IngestionStep) -> None:
    """Set ingestion_jobs.step for the running job to the stage it has just entered."""
    await conn.execute(
        "UPDATE ingestion_jobs SET step = $1 WHERE job_id = $2",
        step,
        job_id,
    )


async def complete_attempt(conn: asyncpg.Connection[Any], job_id: UUID) -> None:
    """Close a successful attempt: job status='done', completed_at=now();
    parent filing status='processed'. filing_id is derived from the job row.
    """
    filing_id: UUID = await conn.fetchval(
        "UPDATE ingestion_jobs"
        " SET status = 'done', completed_at = now()"
        " WHERE job_id = $1"
        " RETURNING filing_id",
        job_id,
    )
    await conn.execute(
        "UPDATE filings SET status = 'processed' WHERE filing_id = $1",
        filing_id,
    )


async def fail_attempt(
    conn: asyncpg.Connection[Any],
    job_id: UUID,
    step: IngestionStep,
    error: str,
) -> FilingStatus:
    """Close a failed attempt: job status='failed', step=<stage>, error=<msg>, completed_at=now().

    Check attempt_count AFTER the failing row is written:
      count 1 or 2 → filing stays 'processing' (retryable), return PROCESSING
      count >= 3   → filing set to 'failed' (exhausted), return FAILED
    Caller owns the transaction boundary.
    """
    filing_id: UUID = await conn.fetchval(
        "UPDATE ingestion_jobs"
        " SET status = 'failed', step = $1, error = $2, completed_at = now()"
        " WHERE job_id = $3"
        " RETURNING filing_id",
        step,
        error,
        job_id,
    )
    count = await attempt_count(conn, filing_id)
    if count >= MAX_ATTEMPTS:
        await conn.execute(
            "UPDATE filings SET status = 'failed' WHERE filing_id = $1",
            filing_id,
        )
        return FilingStatus.FAILED
    return FilingStatus.PROCESSING


def next_backoff(
    attempt: int,
    base_seconds: float = 2.0,
    max_seconds: float = 300.0,
) -> float:
    """Pure: exponential backoff = min(base_seconds * 2 ** (attempt - 1), max_seconds).

    attempt is 1-based: next_backoff(1)=2.0, next_backoff(2)=4.0, next_backoff(3)=8.0.
    """
    return min(base_seconds * 2.0 ** (attempt - 1), max_seconds)


async def reap_stale_running(conn: asyncpg.Connection[Any]) -> int:
    """Mark leftover 'running' jobs from a prior crashed run as failed; return count reaped.

    Run ONCE at run() startup, before the first claim_retryable_filings. A process
    that crashes mid-pipeline leaves a 'running' job whose filing would otherwise be
    re-claimed immediately (last_failed_at IS NULL → no backoff) and never closed.

    Transition only (status -> 'failed', completed_at = now(), error marker); no rows
    deleted, so the per-attempt audit trail is preserved. completed_at drives the normal
    backoff path via claim_retryable_filings' last_failed_at. `step` is intentionally NOT
    rewritten: it keeps the crash-stage (useful diagnostic) and avoids any step CHECK.

    # SAFE ONLY UNDER SINGLE-RUNNER INVARIANT; revisit with v4 lease/SKIP LOCKED.
    """
    status: str = await conn.execute(
        "UPDATE ingestion_jobs"
        " SET status = 'failed',"
        "     error = 'reaped: stale running job from prior crashed run',"
        "     completed_at = now()"
        " WHERE status = 'running'",
    )
    # asyncpg execute() returns a command tag like "UPDATE 3"; parse the trailing count.
    return int(status.split()[-1]) if status else 0


async def claim_retryable_filings(
    conn: asyncpg.Connection[Any],
    limit: int = 10,
) -> list[UUID]:
    """Return up to `limit` filing_ids eligible to (re)process for the batch driver.

    Eligible:
      - status='pending' (never attempted), OR
      - status='processing' AND attempt_count < MAX_ATTEMPTS AND
        last_failed_at + next_backoff(attempt_count) <= now()

    Excludes 'processed' and 'failed'. Backoff is filtered app-side via next_backoff;
    no derived retry columns are read from the DB.
    """
    pending_rows = await conn.fetch(
        "SELECT filing_id FROM filings WHERE status = 'pending' LIMIT $1",
        limit,
    )
    pending_ids: list[UUID] = [row["filing_id"] for row in pending_rows]

    if len(pending_ids) >= limit:
        return pending_ids[:limit]

    remaining = limit - len(pending_ids)

    processing_rows = await conn.fetch(
        "SELECT f.filing_id,"
        "       COUNT(j.job_id) AS attempt_count,"
        "       MAX(CASE WHEN j.status = 'failed' THEN j.completed_at END) AS last_failed_at"
        " FROM filings f"
        " JOIN ingestion_jobs j ON j.filing_id = f.filing_id"
        " WHERE f.status = 'processing'"
        " GROUP BY f.filing_id"
        " HAVING COUNT(j.job_id) < $1"
        " LIMIT $2",
        MAX_ATTEMPTS,
        remaining,
    )

    now = datetime.now(UTC)
    retryable_ids: list[UUID] = []
    for row in processing_rows:
        count: int = row["attempt_count"]
        last_failed_at: datetime | None = row["last_failed_at"]
        if last_failed_at is None:
            retryable_ids.append(row["filing_id"])
            continue
        if last_failed_at.tzinfo is None:
            last_failed_at = last_failed_at.replace(tzinfo=UTC)
        if (last_failed_at.timestamp() + next_backoff(count)) <= now.timestamp():
            retryable_ids.append(row["filing_id"])

    return (pending_ids + retryable_ids)[:limit]
