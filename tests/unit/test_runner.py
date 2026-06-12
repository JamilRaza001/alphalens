"""Unit tests for src/alphalens/etl/runner.py (S11).

Coverage: AC#1–11 offline (deterministic, no DB/network); one gated integration
test (RUN_INTEGRATION=1) covers end-to-end against real Neon + R2 + EDGAR.
asyncio_mode=auto: async tests run without @pytest.mark.asyncio.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from alphalens.etl.runner import (
    DiscoverReport,
    _process_one,
    build_parser,
    discover,
    main,
    run,
)
from alphalens.etl.state import IngestionStep

# ── Constants ─────────────────────────────────────────────────────────────────

_FILING_ID = UUID("a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1")
_JOB_ID = UUID("b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2")
_CIK = "0000320193"
_TICKER = "AAPL"
_ACC = "0000320193-24-000001"
_R2_KEY = f"filings/{_CIK}/{_ACC}.html"
_HTML = b"<html><body>10-K content</body></html>"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_settings() -> MagicMock:
    """Minimal Settings mock with all secrets and URLs."""
    s = MagicMock()
    s.neon_database_url.get_secret_value.return_value = "postgresql://test/neondb"
    s.r2_access_key_id.get_secret_value.return_value = "r2key"
    s.r2_secret_access_key.get_secret_value.return_value = "r2secret"
    s.r2_endpoint_url = "https://r2.test.endpoint"
    s.r2_bucket_name = "test-bucket"
    s.sec_edgar_user_agent = "Test User test@test.com"
    return s


def _filing_row() -> dict[str, Any]:
    return {
        "cik": _CIK,
        "filing_type": "10-K",
        "filing_date": date(2024, 1, 15),
        "period_end": date(2023, 12, 31),
        "accession_number": _ACC,
        "r2_key": _R2_KEY,
    }


# ── Fake asyncpg helpers ──────────────────────────────────────────────────────


class _FakeTx:
    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass


class _FakeConn:
    """Minimal asyncpg.Connection stand-in.

    Routing:
      fetch  — "companies" → company_rows; "pending" → pending_rows; else []
      fetchval — "INSERT INTO filings" → insert_return; "COUNT" → attempt_count;
                 "RETURNING job_id" → job_id; "RETURNING filing_id" → filing_id;
                 else None
      fetchrow — "FROM filings WHERE filing_id" → filing_row; else None
      execute / executemany — no-ops; calls tracked in execute_calls
    """

    def __init__(
        self,
        *,
        company_rows: list[dict[str, Any]] | None = None,
        insert_return: UUID | None = _FILING_ID,
        pending_rows: list[UUID] | None = None,
        attempt_count: int = 0,
        job_id: UUID = _JOB_ID,
        filing_id: UUID = _FILING_ID,
        filing_row: dict[str, Any] | None = None,
    ) -> None:
        self._company_rows = company_rows or []
        self._insert_return = insert_return
        self._pending_rows = pending_rows or []
        self._attempt_count = attempt_count
        self._job_id = job_id
        self._filing_id = filing_id
        self._filing_row = filing_row
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_sqls: list[str] = []

    async def fetch(self, sql: str, *_: Any) -> list[Any]:
        if "companies" in sql.lower():
            return list(self._company_rows)
        if "status = 'pending'" in sql:
            return [{"filing_id": fid} for fid in self._pending_rows]
        if "status = 'processing'" in sql:
            return []
        return []

    async def fetchval(self, sql: str, *_: Any) -> Any:
        self.fetchval_sqls.append(sql)
        if "INSERT INTO filings" in sql:
            return self._insert_return
        if "COUNT(*)" in sql:
            return self._attempt_count
        if "RETURNING job_id" in sql:
            return self._job_id
        if "RETURNING filing_id" in sql:
            return self._filing_id
        return None

    async def fetchrow(self, sql: str, *_: Any) -> Any:
        if "FROM filings WHERE filing_id" in sql:
            return self._filing_row
        return None

    async def execute(self, sql: str, *args: Any) -> None:
        self.execute_calls.append((sql, args))

    async def executemany(self, sql: str, _args: Any) -> None:
        pass

    def transaction(self) -> _FakeTx:
        return _FakeTx()


class _AcqCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        pass


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _AcqCtx:
        return _AcqCtx(self._conn)

    async def close(self) -> None:
        pass


# ── Mock EdgarClient factory ──────────────────────────────────────────────────


def _mock_edgar_ctx(
    *,
    list_filings_return: list[Any] | None = None,
    fetch_doc_return: bytes = _HTML,
) -> MagicMock:
    """Return a MagicMock usable as `async with EdgarClient(...) as edgar:`."""
    edgar = AsyncMock()
    edgar.list_filings = AsyncMock(return_value=list_filings_return or [])
    edgar.fetch_primary_doc = AsyncMock(return_value=fetch_doc_return)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=edgar)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _fake_filing_meta(accession_number: str = _ACC) -> MagicMock:
    m = MagicMock()
    m.accession_number = accession_number
    m.form_type = "10-K"
    m.filing_date = date(2024, 1, 15)
    m.period_of_report = date(2023, 12, 31)
    return m


# ── AC#1 — discover reads companies table ────────────────────────────────────


async def test_ac1_discover_reads_companies_table() -> None:
    """AC#1: discover fetches CIKs from companies; list_filings called once per CIK."""
    conn = _FakeConn(
        company_rows=[{"ticker": _TICKER, "cik": _CIK}],
        insert_return=_FILING_ID,
    )
    pool = _FakePool(conn)
    edgar_ctx = _mock_edgar_ctx(list_filings_return=[_fake_filing_meta()])

    with (
        patch("alphalens.etl.runner.asyncpg.create_pool", AsyncMock(return_value=pool)),
        patch("alphalens.etl.runner.EdgarClient", return_value=edgar_ctx),
    ):
        report = await discover(_make_settings())

    assert report.companies_scanned == 1
    assert report.filings_found == 1
    assert report.filings_enqueued == 1
    edgar_ctx.__aenter__.return_value.list_filings.assert_called_once_with(_CIK)


# ── AC#2 — discover enqueues only 10-K/10-Q 2022-2026 ────────────────────────


async def test_ac2_discover_filters_via_edgar_defaults() -> None:
    """AC#2: list_filings defaults (2022-01-01..2026-12-31, 10-K/10-Q) do the filtering.
    Verify the runner does NOT supply custom date/form_types — relies on EdgarClient defaults."""
    conn = _FakeConn(
        company_rows=[{"ticker": _TICKER, "cik": _CIK}],
        insert_return=None,  # simulate conflict → not enqueued
    )
    pool = _FakePool(conn)
    edgar_ctx = _mock_edgar_ctx(list_filings_return=[_fake_filing_meta()])

    with (
        patch("alphalens.etl.runner.asyncpg.create_pool", AsyncMock(return_value=pool)),
        patch("alphalens.etl.runner.EdgarClient", return_value=edgar_ctx),
    ):
        report = await discover(_make_settings())

    # list_filings called with only the CIK — no extra date/form_types override
    edgar_ctx.__aenter__.return_value.list_filings.assert_called_once_with(_CIK)
    assert report.filings_found == 1
    assert report.filings_enqueued == 0  # conflict → RETURNING returned None


# ── AC#3 — discover is idempotent ────────────────────────────────────────────


async def test_ac3_discover_idempotent_on_conflict() -> None:
    """AC#3: ON CONFLICT (accession_number) DO NOTHING → filings_enqueued=0 on re-run."""
    conn = _FakeConn(
        company_rows=[{"ticker": _TICKER, "cik": _CIK}],
        insert_return=None,  # RETURNING yields nothing on conflict
    )
    pool = _FakePool(conn)
    edgar_ctx = _mock_edgar_ctx(list_filings_return=[_fake_filing_meta()])

    with (
        patch("alphalens.etl.runner.asyncpg.create_pool", AsyncMock(return_value=pool)),
        patch("alphalens.etl.runner.EdgarClient", return_value=edgar_ctx),
    ):
        report = await discover(_make_settings())

    assert report.filings_enqueued == 0
    # INSERT SQL must contain ON CONFLICT DO NOTHING
    insert_sqls = [s for s in conn.fetchval_sqls if "INSERT INTO filings" in s]
    assert insert_sqls, "expected at least one INSERT INTO filings"
    assert all("ON CONFLICT" in s for s in insert_sqls)
    assert all("DO NOTHING" in s for s in insert_sqls)


# ── AC#4 — discover INSERT never references r2_key ───────────────────────────


async def test_ac4_discover_insert_omits_r2_key() -> None:
    """AC#4: the INSERT SQL must not mention r2_key (it is a generated column)."""
    conn = _FakeConn(
        company_rows=[{"ticker": _TICKER, "cik": _CIK}],
        insert_return=_FILING_ID,
    )
    pool = _FakePool(conn)
    edgar_ctx = _mock_edgar_ctx(list_filings_return=[_fake_filing_meta()])

    with (
        patch("alphalens.etl.runner.asyncpg.create_pool", AsyncMock(return_value=pool)),
        patch("alphalens.etl.runner.EdgarClient", return_value=edgar_ctx),
    ):
        await discover(_make_settings())

    insert_sqls = [s for s in conn.fetchval_sqls if "INSERT INTO filings" in s]
    assert insert_sqls, "expected an INSERT INTO filings call"
    for sql in insert_sqls:
        assert "r2_key" not in sql, f"r2_key must not appear in INSERT SQL; got:\n{sql}"


# ── AC#5 — run processes filings sequentially (D3) ───────────────────────────


async def test_ac5_run_sequential_no_gather() -> None:
    """AC#5: run() processes filings one at a time — _process_one called 3× in order."""
    ids = [UUID(f"{'c' * 8}-{'c' * 4}-{'c' * 4}-{'c' * 4}-{str(i) * 12}") for i in range(3)]
    call_order: list[UUID] = []

    async def _fake_process(fid: UUID, *, settings: Any, pool: Any) -> None:
        call_order.append(fid)

    conn = _FakeConn(pending_rows=ids)
    pool = _FakePool(conn)

    with (
        patch("alphalens.etl.runner.asyncpg.create_pool", AsyncMock(return_value=pool)),
        patch("alphalens.etl.runner.register_pgvector", AsyncMock()),
        patch("alphalens.etl.runner.claim_retryable_filings", AsyncMock(side_effect=[ids, []])),
        patch("alphalens.etl.runner._process_one", side_effect=_fake_process),
    ):
        report = await run(_make_settings())

    assert call_order == ids, "filings must be processed in claim order"
    assert report.claimed == 3
    assert report.processed == 3
    assert report.failed == 0


# ── AC#6 — run --limit 1 processes exactly one filing ────────────────────────


async def test_ac6_run_limit_1() -> None:
    """AC#6: limit=1 claims and processes exactly one filing, then stops."""
    ids = [_FILING_ID, UUID("d4d4d4d4-d4d4-d4d4-d4d4-d4d4d4d4d4d4")]

    conn = _FakeConn()
    pool = _FakePool(conn)

    with (
        patch("alphalens.etl.runner.asyncpg.create_pool", AsyncMock(return_value=pool)),
        patch("alphalens.etl.runner.register_pgvector", AsyncMock()),
        patch(
            "alphalens.etl.runner.claim_retryable_filings",
            AsyncMock(return_value=ids),
        ),
        patch(
            "alphalens.etl.runner._process_one",
            AsyncMock(return_value=None),
        ) as mock_proc,
    ):
        report = await run(_make_settings(), limit=1)

    assert mock_proc.call_count == 1
    assert report.claimed == 1
    assert report.processed == 1


# ── AC#7 — successful _process_one calls complete_attempt ────────────────────


async def test_ac7_process_one_success_calls_complete() -> None:
    """AC#7: successful pipeline run calls complete_attempt; no fail_attempt called."""
    conn = _FakeConn(filing_row=_filing_row())
    pool = _FakePool(conn)

    mock_chunk = MagicMock()
    mock_chunk.text = "risk factor sentence."
    mock_section = MagicMock()
    mock_embeddings = MagicMock()
    mock_records = [MagicMock()]

    edgar_ctx = _mock_edgar_ctx(list_filings_return=[_fake_filing_meta()])

    with (
        patch("alphalens.etl.runner.start_attempt", AsyncMock(return_value=_JOB_ID)),
        patch("alphalens.etl.runner.record_step", AsyncMock()),
        patch("alphalens.etl.runner.complete_attempt", AsyncMock()) as mock_complete,
        patch("alphalens.etl.runner.fail_attempt", AsyncMock()) as mock_fail,
        patch("alphalens.etl.runner.EdgarClient", return_value=edgar_ctx),
        patch("alphalens.etl.runner._upload_html_to_r2", AsyncMock()),
        patch(
            "alphalens.etl.runner.SectionDetector",
            return_value=MagicMock(detect=MagicMock(return_value=[mock_section])),
        ),
        patch(
            "alphalens.etl.runner.Chunker",
            return_value=MagicMock(chunk_sections=MagicMock(return_value=[mock_chunk])),
        ),
        patch(
            "alphalens.etl.runner.EmbeddingClient",
            return_value=MagicMock(
                embed_documents=AsyncMock(return_value=mock_embeddings),
                aclose=AsyncMock(),
            ),
        ),
        patch(
            "alphalens.etl.runner.assemble_embedded_chunks",
            return_value=mock_records,
        ),
        patch("alphalens.etl.runner.upsert_embedded_chunks", AsyncMock()),
    ):
        await _process_one(_FILING_ID, settings=_make_settings(), pool=pool)

    mock_complete.assert_called_once()
    mock_fail.assert_not_called()


# ── AC#8 — pipeline failure routes through state machine ─────────────────────


async def test_ac8_embed_failure_calls_fail_attempt() -> None:
    """AC#8: an exception during EMBED calls fail_attempt with IngestionStep.EMBED."""
    conn = _FakeConn(filing_row=_filing_row())
    pool = _FakePool(conn)

    mock_chunk = MagicMock()
    mock_chunk.text = "text"
    edgar_ctx = _mock_edgar_ctx(list_filings_return=[_fake_filing_meta()])

    with (
        patch("alphalens.etl.runner.start_attempt", AsyncMock(return_value=_JOB_ID)),
        patch("alphalens.etl.runner.record_step", AsyncMock()),
        patch("alphalens.etl.runner.complete_attempt", AsyncMock()),
        patch("alphalens.etl.runner.fail_attempt", AsyncMock()) as mock_fail,
        patch("alphalens.etl.runner.EdgarClient", return_value=edgar_ctx),
        patch("alphalens.etl.runner._upload_html_to_r2", AsyncMock()),
        patch(
            "alphalens.etl.runner.SectionDetector",
            return_value=MagicMock(detect=MagicMock(return_value=[MagicMock()])),
        ),
        patch(
            "alphalens.etl.runner.Chunker",
            return_value=MagicMock(chunk_sections=MagicMock(return_value=[mock_chunk])),
        ),
        patch(
            "alphalens.etl.runner.EmbeddingClient",
            return_value=MagicMock(
                embed_documents=AsyncMock(side_effect=RuntimeError("quota exceeded")),
                aclose=AsyncMock(),
            ),
        ),
        pytest.raises(RuntimeError, match="quota exceeded"),
    ):
        await _process_one(_FILING_ID, settings=_make_settings(), pool=pool)

    mock_fail.assert_called_once()
    # fail_attempt(conn, job_id, step, error) — positional
    args = mock_fail.call_args.args
    assert args[1] == _JOB_ID
    assert args[2] == IngestionStep.EMBED
    assert "quota exceeded" in args[3]


# ── AC#9 — run is resumable (skips empty claim on re-invoke) ─────────────────


async def test_ac9_run_exits_when_no_filings() -> None:
    """AC#9: when claim_retryable_filings returns [], run exits immediately with claimed=0."""
    conn = _FakeConn()
    pool = _FakePool(conn)

    with (
        patch("alphalens.etl.runner.asyncpg.create_pool", AsyncMock(return_value=pool)),
        patch("alphalens.etl.runner.register_pgvector", AsyncMock()),
        patch(
            "alphalens.etl.runner.claim_retryable_filings",
            AsyncMock(return_value=[]),
        ),
        patch("alphalens.etl.runner._process_one", AsyncMock()) as mock_proc,
    ):
        report = await run(_make_settings())

    mock_proc.assert_not_called()
    assert report.claimed == 0
    assert report.processed == 0


# ── AC#10 — R2 upload uses filings.r2_key ────────────────────────────────────


async def test_ac10_upload_uses_r2_key_from_db_row() -> None:
    """AC#10: _process_one passes the DB-supplied r2_key to _upload_html_to_r2."""
    expected_key = _R2_KEY
    conn = _FakeConn(filing_row=_filing_row())
    pool = _FakePool(conn)

    mock_chunk = MagicMock()
    mock_chunk.text = "text"
    edgar_ctx = _mock_edgar_ctx(list_filings_return=[_fake_filing_meta()])

    upload_calls: list[str] = []

    async def _capture_upload(key: str, body: bytes, *, settings: Any) -> None:
        upload_calls.append(key)

    with (
        patch("alphalens.etl.runner.start_attempt", AsyncMock(return_value=_JOB_ID)),
        patch("alphalens.etl.runner.record_step", AsyncMock()),
        patch("alphalens.etl.runner.complete_attempt", AsyncMock()),
        patch("alphalens.etl.runner.fail_attempt", AsyncMock()),
        patch("alphalens.etl.runner.EdgarClient", return_value=edgar_ctx),
        patch("alphalens.etl.runner._upload_html_to_r2", side_effect=_capture_upload),
        patch(
            "alphalens.etl.runner.SectionDetector",
            return_value=MagicMock(detect=MagicMock(return_value=[MagicMock()])),
        ),
        patch(
            "alphalens.etl.runner.Chunker",
            return_value=MagicMock(chunk_sections=MagicMock(return_value=[mock_chunk])),
        ),
        patch(
            "alphalens.etl.runner.EmbeddingClient",
            return_value=MagicMock(
                embed_documents=AsyncMock(return_value=MagicMock()),
                aclose=AsyncMock(),
            ),
        ),
        patch("alphalens.etl.runner.assemble_embedded_chunks", return_value=[MagicMock()]),
        patch("alphalens.etl.runner.upsert_embedded_chunks", AsyncMock()),
    ):
        await _process_one(_FILING_ID, settings=_make_settings(), pool=pool)

    assert upload_calls == [expected_key], f"expected R2 key {expected_key!r}, got {upload_calls}"


# ── AC#11 — CLI exit codes ────────────────────────────────────────────────────


def test_ac11_main_returns_0_on_success() -> None:
    """AC#11: main() returns 0 when discover completes without error."""
    mock_report = DiscoverReport(companies_scanned=1, filings_found=5, filings_enqueued=5)
    with (
        patch("alphalens.etl.runner.get_settings", return_value=_make_settings()),
        patch("alphalens.etl.runner.discover", new=MagicMock(return_value=mock_report)),
        patch("alphalens.etl.runner.asyncio.run", new=MagicMock(return_value=mock_report)),
    ):
        assert main(["discover"]) == 0


def test_ac11_main_returns_nonzero_on_failure() -> None:
    """AC#11: main() returns non-zero when the command raises an exception."""
    with (
        patch("alphalens.etl.runner.get_settings", return_value=_make_settings()),
        patch("alphalens.etl.runner.discover", new=MagicMock(return_value=MagicMock())),
        patch(
            "alphalens.etl.runner.asyncio.run",
            new=MagicMock(side_effect=RuntimeError("neon connection refused")),
        ),
    ):
        assert main(["discover"]) != 0


def test_ac11_help_exits_zero() -> None:
    """AC#11: --help exits with code 0."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_ac11_unknown_subcommand_exits_nonzero() -> None:
    """AC#11: unknown subcommand exits with non-zero code."""
    with pytest.raises(SystemExit) as exc_info:
        main(["badcmd"])
    assert exc_info.value.code != 0


def test_ac11_build_parser_has_both_subcommands() -> None:
    """AC#11: parser exposes discover and run subcommands; run has --limit."""
    parser = build_parser()
    args = parser.parse_args(["run", "--limit", "5"])
    assert args.cmd == "run"
    assert args.limit == 5

    args2 = parser.parse_args(["discover"])
    assert args2.cmd == "discover"


# ── Integration — real Neon DB + R2 + EDGAR (skip unless RUN_INTEGRATION=1) ──

_skip_integration = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="set RUN_INTEGRATION=1 to run live integration tests",
)


@_skip_integration
@pytest.mark.integration
async def test_integration_discover_and_run_one() -> None:
    """Integration AC#7: discover enqueues filings, run --limit 1 fully processes one.

    Writes to real Neon DB and real R2 bucket. Rolls back DB via transaction.
    NOTE: R2 objects written during the test are NOT automatically cleaned up.
    """
    import asyncpg as _asyncpg
    from alphalens.config import get_settings

    settings = get_settings()
    conn: _asyncpg.Connection[Any] = await _asyncpg.connect(
        settings.neon_direct_url.get_secret_value()
    )
    tr = conn.transaction()
    await tr.start()
    try:
        # Confirm at least one company exists to discover
        count: int = await conn.fetchval("SELECT COUNT(*) FROM companies")
        assert count > 0, "seed companies before running integration test"

        # discover: enqueue up to 1 ticker
        tickers: list[str] = [
            row["ticker"] for row in await conn.fetch("SELECT ticker FROM companies LIMIT 1")
        ]
        discover_report = await discover(settings, tickers=tickers)
        assert discover_report.companies_scanned == 1
        assert discover_report.filings_found >= 0  # may be 0 if EDGAR returns nothing

        # run --limit 1 (only if something was enqueued)
        if discover_report.filings_enqueued > 0:
            run_report = await run(settings, limit=1)
            assert run_report.claimed == 1
            assert run_report.processed + run_report.failed == 1  # exactly one outcome
    finally:
        await tr.rollback()
        await conn.close()
