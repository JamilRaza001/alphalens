"""Unit tests for src/alphalens/etl/state.py (S10).

Coverage: AC#2–9 offline (deterministic, no DB); one gated integration test
(RUN_INTEGRATION=1) covers ACs 1, 3, 4, 5, 8, 10 against real Neon.
asyncio_mode=auto: async tests run without @pytest.mark.asyncio.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest
from alphalens.etl.state import (
    FilingStatus,
    IngestionStep,
    MaxAttemptsReached,
    attempt_count,
    claim_retryable_filings,
    complete_attempt,
    fail_attempt,
    next_backoff,
    reap_stale_running,
    record_step,
    start_attempt,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_FILING_ID = UUID("d1d1d1d1-d1d1-d1d1-d1d1-d1d1d1d1d1d1")
_JOB_ID = UUID("e2e2e2e2-e2e2-e2e2-e2e2-e2e2e2e2e2e2")


# ── _FakeConn ─────────────────────────────────────────────────────────────────


class _FakeConn:
    """Minimal asyncpg.Connection stand-in for offline state machine tests.

    fetchval dispatch:
      - SQL contains "COUNT(*)"        → return self._attempt_count
      - SQL contains "RETURNING job_id"    → return self._job_id
      - SQL contains "RETURNING filing_id" → return self._filing_id
    execute calls are appended to execute_calls for assertion.
    fetch always returns [].
    """

    def __init__(
        self,
        *,
        attempt_count: int = 0,
        filing_id: UUID = _FILING_ID,
        job_id: UUID = _JOB_ID,
    ) -> None:
        self._attempt_count = attempt_count
        self._filing_id = filing_id
        self._job_id = job_id
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "COUNT(*)" in sql:
            return self._attempt_count
        if "RETURNING job_id" in sql:
            return self._job_id
        if "RETURNING filing_id" in sql:
            return self._filing_id
        raise AssertionError(f"Unexpected fetchval SQL: {sql[:80]!r}")

    async def execute(self, sql: str, *args: Any) -> None:
        self.execute_calls.append((sql, args))

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return []


# ── AC#2 — MaxAttemptsReached gate ────────────────────────────────────────────


async def test_ac2_max_attempts_reached() -> None:
    """AC#2: start_attempt raises MaxAttemptsReached when attempt_count >= 3."""
    conn = _FakeConn(attempt_count=3)
    with pytest.raises(MaxAttemptsReached):
        await start_attempt(conn, _FILING_ID)


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_ac2_start_allowed_at_0_1_2(count: int) -> None:
    """AC#2: start_attempt does NOT raise for counts 0, 1, 2; returns job_id."""
    conn = _FakeConn(attempt_count=count)
    job_id = await start_attempt(conn, _FILING_ID)
    assert job_id == _JOB_ID


# ── AC#5 — fail_attempt off-by-one ───────────────────────────────────────────


async def test_ac5_fail_returns_failed_at_count_3() -> None:
    """AC#5: fail_attempt returns FAILED and sets filing to 'failed' when count reaches 3."""
    conn = _FakeConn(attempt_count=3)
    result = await fail_attempt(conn, _JOB_ID, IngestionStep.EMBED, "quota exceeded")
    assert result == FilingStatus.FAILED
    sql_texts = [call[0] for call in conn.execute_calls]
    assert any(
        "'failed'" in sql for sql in sql_texts
    ), "expected UPDATE filings SET status = 'failed' when count >= MAX_ATTEMPTS"


async def test_ac5_fail_returns_processing_at_count_1() -> None:
    """AC#5: fail_attempt returns PROCESSING when count=1 (2 attempts remain); filing not set to 'failed'."""
    conn = _FakeConn(attempt_count=1)
    result = await fail_attempt(conn, _JOB_ID, IngestionStep.PARSE, "parse error")
    assert result == FilingStatus.PROCESSING
    sql_texts = [call[0] for call in conn.execute_calls]
    assert not any(
        "status = 'failed'" in sql for sql in sql_texts
    ), "filing must NOT be set to 'failed' when count < MAX_ATTEMPTS"


async def test_ac5_fail_returns_processing_at_count_2() -> None:
    """AC#5: fail_attempt returns PROCESSING when count=2 (1 attempt remains)."""
    conn = _FakeConn(attempt_count=2)
    result = await fail_attempt(conn, _JOB_ID, IngestionStep.CHUNK, "chunk failed")
    assert result == FilingStatus.PROCESSING


# ── AC#6 — attempt_count ─────────────────────────────────────────────────────


async def test_ac6_attempt_count() -> None:
    """AC#6: attempt_count returns the COUNT(*) result from ingestion_jobs."""
    conn = _FakeConn(attempt_count=7)
    result = await attempt_count(conn, _FILING_ID)
    assert result == 7


# ── AC#7 — next_backoff pure ─────────────────────────────────────────────────


def test_ac7_next_backoff_values() -> None:
    """AC#7: next_backoff(1)=2.0, next_backoff(2)=4.0, next_backoff(3)=8.0."""
    assert next_backoff(1) == 2.0
    assert next_backoff(2) == 4.0
    assert next_backoff(3) == 8.0


def test_ac7_next_backoff_cap() -> None:
    """AC#7: next_backoff caps at max_seconds."""
    assert next_backoff(100, max_seconds=10.0) == 10.0
    assert next_backoff(1, base_seconds=2.0, max_seconds=1.5) == 1.5


# ── AC#8 — claim_retryable_filings offline (empty DB) ────────────────────────


async def test_ac8_claim_retryable_empty() -> None:
    """AC#8: claim_retryable_filings returns [] when the DB returns no rows."""
    conn = _FakeConn()
    result = await claim_retryable_filings(conn)
    assert result == []


# ── reap_stale_running (#4) ──────────────────────────────────────────────────


class _ReapConn:
    """asyncpg.Connection stand-in whose execute() returns a command tag (e.g. 'UPDATE 3')."""

    def __init__(self, command_tag: str = "UPDATE 0") -> None:
        self._command_tag = command_tag
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return self._command_tag


async def test_reap_returns_parsed_count() -> None:
    """reap_stale_running parses asyncpg's command tag and returns the row count."""
    conn = _ReapConn("UPDATE 3")
    assert await reap_stale_running(conn) == 3


async def test_reap_none_running() -> None:
    """reap_stale_running returns 0 when no 'running' jobs remain."""
    conn = _ReapConn("UPDATE 0")
    assert await reap_stale_running(conn) == 0


async def test_reap_transition_only_preserves_step_and_audit() -> None:
    """Reaper flips status->failed + completed_at + error, targets only 'running',
    deletes nothing, and does NOT rewrite step (crash-stage kept; avoids step CHECK)."""
    conn = _ReapConn("UPDATE 1")
    await reap_stale_running(conn)
    assert len(conn.execute_calls) == 1
    sql = conn.execute_calls[0][0]
    assert "status = 'failed'" in sql
    assert "completed_at = now()" in sql
    assert "WHERE status = 'running'" in sql
    assert "DELETE" not in sql.upper()
    assert "step =" not in sql


# ── AC#9 — no forbidden columns ──────────────────────────────────────────────


def test_ac9_no_forbidden_columns() -> None:
    """AC#9: state.py must not reference retry_count or next_attempt_at."""
    import pathlib

    state_py = (
        pathlib.Path(__file__).parent.parent.parent / "src" / "alphalens" / "etl" / "state.py"
    )
    text = state_py.read_text()
    assert "retry_count" not in text, "retry_count must not appear in state.py"
    assert "next_attempt_at" not in text, "next_attempt_at must not appear in state.py"


# ── Integration — real Neon DB (skip unless RUN_INTEGRATION=1) ────────────────

_skip_integration = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="set RUN_INTEGRATION=1 to run live integration tests",
)


@_skip_integration
@pytest.mark.integration
async def test_integration_state_machine() -> None:
    """Integration: full two-level state transitions against real Neon DB.

    Covers ACs 1, 3, 4, 5, 8, 10.
    FK chain: companies → filings → ingestion_jobs.
    All writes rolled back in finally — no persistent state left behind.

    NOTE: filings.cik is NOT NULL and FK → companies.cik (live schema requires it).
    S9's integration test omits cik (pre-existing bug). This test includes it correctly.
    """
    from datetime import date

    import asyncpg as _asyncpg
    from alphalens.config import get_settings

    settings = get_settings()
    conn: _asyncpg.Connection[Any] = await _asyncpg.connect(
        settings.neon_direct_url.get_secret_value()
    )
    tr = conn.transaction()
    await tr.start()
    try:
        # FK chain: company first (cik is NOT NULL FK in filings)
        await conn.execute(
            "INSERT INTO companies (ticker, name, cik) VALUES ($1, $2, $3)",
            "__S10TEST__",
            "S10 Integration Test",
            "0000000000",
        )
        filing_id: UUID = await conn.fetchval(
            "INSERT INTO filings"
            " (ticker, filing_type, filing_date, period_end,"
            "  accession_number, r2_key, cik)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)"
            " RETURNING filing_id",
            "__S10TEST__",
            "10-K",
            date(2024, 1, 1),
            date(2023, 12, 31),
            "0000000000-24-000001",
            "test/s10/integration",
            "0000000000",
        )
        assert filing_id is not None

        # ── AC#1: start_attempt sets filing='processing', inserts job (step IS NULL) ──
        job_id = await start_attempt(conn, filing_id)
        assert job_id is not None

        status: str = await conn.fetchval(
            "SELECT status FROM filings WHERE filing_id = $1", filing_id
        )
        assert status == "processing", f"expected 'processing', got {status!r}"

        job_row = await conn.fetchrow(
            "SELECT status, step, started_at FROM ingestion_jobs WHERE job_id = $1", job_id
        )
        assert job_row is not None
        assert job_row["status"] == "running"
        assert job_row["step"] is None
        assert job_row["started_at"] is not None

        # ── AC#3: record_step sets step column; DB CHECK enforces valid values ──
        await record_step(conn, job_id, IngestionStep.DOWNLOAD)
        step: str = await conn.fetchval("SELECT step FROM ingestion_jobs WHERE job_id = $1", job_id)
        assert step == "download", f"expected 'download', got {step!r}"

        # ── AC#4: complete_attempt → job='done', filing='processed' ──────────────
        await complete_attempt(conn, job_id)

        filing_status: str = await conn.fetchval(
            "SELECT status FROM filings WHERE filing_id = $1", filing_id
        )
        assert filing_status == "processed", f"expected 'processed', got {filing_status!r}"

        job_status: str = await conn.fetchval(
            "SELECT status FROM ingestion_jobs WHERE job_id = $1", job_id
        )
        assert job_status == "done", f"expected 'done', got {job_status!r}"

        # ── AC#8: claim_retryable_filings finds pending, excludes processed ────
        filing_id2: UUID = await conn.fetchval(
            "INSERT INTO filings"
            " (ticker, filing_type, filing_date, period_end,"
            "  accession_number, r2_key, cik)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)"
            " RETURNING filing_id",
            "__S10TEST__",
            "10-Q",
            date(2024, 4, 1),
            date(2024, 3, 31),
            "0000000000-24-000002",
            "test/s10/integration2",
            "0000000000",
        )
        retryable = await claim_retryable_filings(conn)
        assert filing_id2 in retryable, "pending filing must appear in claim_retryable_filings"
        assert filing_id not in retryable, "processed filing must NOT appear"

        # ── AC#5: retry exhaustion — 3 failures → FAILED; 4th attempt raises ──

        # Attempt 1 → fail → count=1 → PROCESSING
        job2a = await start_attempt(conn, filing_id2)
        result1 = await fail_attempt(conn, job2a, IngestionStep.EMBED, "attempt 1 error")
        assert result1 == FilingStatus.PROCESSING, f"got {result1!r}, expected PROCESSING"

        status2_after_1: str = await conn.fetchval(
            "SELECT status FROM filings WHERE filing_id = $1", filing_id2
        )
        assert status2_after_1 == "processing"

        # Attempt 2 → fail → count=2 → PROCESSING
        job2b = await start_attempt(conn, filing_id2)
        result2 = await fail_attempt(conn, job2b, IngestionStep.EMBED, "attempt 2 error")
        assert result2 == FilingStatus.PROCESSING, f"got {result2!r}, expected PROCESSING"

        # Attempt 3 → fail → count=3 → FAILED
        job2c = await start_attempt(conn, filing_id2)
        result3 = await fail_attempt(conn, job2c, IngestionStep.EMBED, "attempt 3 error")
        assert result3 == FilingStatus.FAILED, f"got {result3!r}, expected FAILED"

        final_status: str = await conn.fetchval(
            "SELECT status FROM filings WHERE filing_id = $1", filing_id2
        )
        assert final_status == "failed", f"expected 'failed', got {final_status!r}"

        # ── AC#10: 4th attempt raises MaxAttemptsReached ──────────────────────
        with pytest.raises(MaxAttemptsReached):
            await start_attempt(conn, filing_id2)

        # ── AC#8 part 2: claim_retryable_filings excludes 'failed' ───────────
        retryable2 = await claim_retryable_filings(conn)
        assert (
            filing_id2 not in retryable2
        ), "failed filing must NOT appear in claim_retryable_filings"

    finally:
        await tr.rollback()
        await conn.close()
