"""ETL runner: discover + run orchestration over the existing ETL modules."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import date
from typing import Literal, cast
from uuid import UUID

import aioboto3
import asyncpg
from aiobotocore.config import AioConfig
from pydantic import ValidationError

from alphalens.config import Settings, get_settings
from alphalens.etl.chunker import Chunker
from alphalens.etl.edgar import EdgarClient
from alphalens.etl.embeddings import EmbeddingClient
from alphalens.etl.sections import SectionDetector
from alphalens.etl.state import (
    IngestionStep,
    claim_retryable_filings,
    complete_attempt,
    fail_attempt,
    reap_stale_running,
    record_step,
    start_attempt,
)
from alphalens.etl.upsert import (
    assemble_embedded_chunks,
    register_pgvector,
    upsert_embedded_chunks,
)

_log = logging.getLogger(__name__)

# ---- result records (small; for logging + tests) ----------------------------


@dataclass(frozen=True, slots=True)
class DiscoverReport:
    """Outcome of one discover pass."""

    companies_scanned: int
    filings_found: int
    filings_enqueued: int  # newly inserted; ON CONFLICT DO NOTHING skips dupes


@dataclass(frozen=True, slots=True)
class RunReport:
    """Outcome of one run pass."""

    claimed: int
    processed: int
    failed: int


# ---- public orchestration ---------------------------------------------------


async def discover(settings: Settings, *, tickers: list[str] | None = None) -> DiscoverReport:
    """List 2022-2026 10-K/10-Q filings for target companies (from the companies
    table) and enqueue them as status='pending'. Idempotent on accession_number.
    Never writes r2_key. `tickers=None` means all seeded companies."""
    pool: asyncpg.Pool[asyncpg.Record] = await asyncpg.create_pool(
        settings.neon_database_url.get_secret_value(),
        min_size=1,
        max_size=5,
    )
    try:
        async with pool.acquire() as conn:
            if tickers is None:
                rows = await conn.fetch("SELECT ticker, cik FROM companies")
            else:
                rows = await conn.fetch(
                    "SELECT ticker, cik FROM companies WHERE ticker = ANY($1::text[])",
                    tickers,
                )
        companies: list[tuple[str, str]] = [(r["ticker"], r["cik"]) for r in rows]

        total_found = 0
        total_enqueued = 0

        async with EdgarClient(settings) as edgar:
            for ticker, cik in companies:
                filings = await edgar.list_filings(cik)
                total_found += len(filings)
                async with pool.acquire() as conn:
                    for f in filings:
                        result: UUID | None = await conn.fetchval(
                            "INSERT INTO filings"
                            " (ticker, cik, filing_type, filing_date,"
                            "  period_end, accession_number)"
                            " VALUES ($1, $2, $3, $4, $5, $6)"
                            " ON CONFLICT (accession_number) DO NOTHING"
                            " RETURNING filing_id",
                            ticker,
                            cik,
                            f.form_type,
                            f.filing_date,
                            f.period_of_report,
                            f.accession_number,
                        )
                        if result is not None:
                            total_enqueued += 1

        _log.info(
            "discover: scanned=%d found=%d enqueued=%d",
            len(companies),
            total_found,
            total_enqueued,
        )
        return DiscoverReport(
            companies_scanned=len(companies),
            filings_found=total_found,
            filings_enqueued=total_enqueued,
        )
    finally:
        await pool.close()


async def run(settings: Settings, *, limit: int | None = None) -> RunReport:
    """Claim and sequentially process pending/retryable filings end-to-end.
    `limit` caps filings processed this invocation (None = all). Resumable."""
    pool: asyncpg.Pool[asyncpg.Record] = await asyncpg.create_pool(
        settings.neon_database_url.get_secret_value(),
        min_size=1,
        max_size=5,
        init=register_pgvector,
    )
    try:
        # Startup-only: close orphaned 'running' jobs from a prior crashed run so they
        # enter the normal backoff path instead of being re-claimed in a tight loop.
        async with pool.acquire() as conn:
            reaped = await reap_stale_running(conn)
        if reaped:
            _log.info("reaped %d stale running job(s) from a prior crashed run", reaped)

        claimed = 0
        processed = 0
        failed = 0

        while True:
            remaining = None if limit is None else limit - claimed
            if remaining is not None and remaining <= 0:
                break
            batch = min(10, remaining) if remaining is not None else 10
            async with pool.acquire() as conn:
                filing_ids = await claim_retryable_filings(conn, limit=batch)
            if not filing_ids:
                break
            for filing_id in filing_ids:
                claimed += 1
                try:
                    await _process_one(filing_id, settings=settings, pool=pool)
                    processed += 1
                except Exception:
                    _log.exception("filing %s failed", filing_id)
                    failed += 1
                if limit is not None and claimed >= limit:
                    break
            if limit is not None and claimed >= limit:
                break

        _log.info("run: claimed=%d processed=%d failed=%d", claimed, processed, failed)
        return RunReport(claimed=claimed, processed=processed, failed=failed)
    finally:
        await pool.close()


# ---- internal seams ---------------------------------------------------------


async def _process_one(
    filing_id: UUID, *, settings: Settings, pool: asyncpg.Pool[asyncpg.Record]
) -> None:
    """Run one claimed filing through the full pipeline, recording each
    IngestionStep and routing failures through the state machine (backoff/retry)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cik, filing_type, filing_date, period_end,"
            " accession_number, r2_key"
            " FROM filings WHERE filing_id = $1",
            filing_id,
        )
    if row is None:
        raise RuntimeError(f"filing {filing_id} not found in DB")

    r2_key: str = row["r2_key"]
    cik: str = row["cik"]
    accession_number: str = row["accession_number"]
    filing_date: date = row["filing_date"]
    form_type = cast(Literal["10-K", "10-Q"], row["filing_type"])

    # MaxAttemptsReached propagates to run() — no job row was created, no fail_attempt needed
    async with pool.acquire() as conn, conn.transaction():
        job_id: UUID = await start_attempt(conn, filing_id)

    current_step = IngestionStep.DOWNLOAD
    try:
        async with pool.acquire() as conn:
            await record_step(conn, job_id, IngestionStep.DOWNLOAD)

        async with EdgarClient(settings) as edgar:
            metas = await edgar.list_filings(
                cik,
                form_types=(form_type,),
                date_from=filing_date,
                date_to=filing_date,
            )
            meta = next((m for m in metas if m.accession_number == accession_number), None)
            if meta is None:
                raise RuntimeError(
                    f"accession {accession_number!r} not in EDGAR listing for CIK {cik}"
                )
            html = await edgar.fetch_primary_doc(meta)

        await _upload_html_to_r2(r2_key, html, settings=settings)

        current_step = IngestionStep.PARSE
        async with pool.acquire() as conn:
            await record_step(conn, job_id, IngestionStep.PARSE)
        sections = SectionDetector(form_type=form_type).detect(html)

        current_step = IngestionStep.CHUNK
        async with pool.acquire() as conn:
            await record_step(conn, job_id, IngestionStep.CHUNK)
        chunks = Chunker(max_tokens=settings.chunk_max_tokens).chunk_sections(sections)
        if not chunks:
            raise RuntimeError("no chunks produced for filing")

        current_step = IngestionStep.EMBED
        async with pool.acquire() as conn:
            await record_step(conn, job_id, IngestionStep.EMBED)
        embedder = EmbeddingClient(settings)
        try:
            embeddings = await embedder.embed_documents(
                [c.text for c in chunks],
                token_counts=[c.token_count for c in chunks],
            )
        finally:
            await embedder.aclose()

        records = assemble_embedded_chunks(filing_id, chunks, embeddings)

        current_step = IngestionStep.UPSERT
        async with pool.acquire() as conn:
            await record_step(conn, job_id, IngestionStep.UPSERT)
            await upsert_embedded_chunks(conn, records)

        # Atomic: both UPDATEs (job -> done, filing -> processed) commit all-or-nothing.
        async with pool.acquire() as conn, conn.transaction():
            await complete_attempt(conn, job_id)

        _log.info("filing %s processed: %d chunks", filing_id, len(records))

    except Exception as exc:
        try:
            # Atomic: job-fail UPDATE + attempt-count SELECT + conditional filing UPDATE.
            async with pool.acquire() as conn, conn.transaction():
                await fail_attempt(conn, job_id, current_step, str(exc)[:2000])
        except Exception:
            _log.exception("fail_attempt raised for filing %s job %s", filing_id, job_id)
        raise


async def _upload_html_to_r2(key: str, body: bytes, *, settings: Settings) -> None:
    """PUT filing HTML to R2 at `key` via aioboto3 (async S3-compatible client).

    `key` is the canonical, DB-linked ``filings.r2_key`` (``filings/{cik}/{accession}.html``),
    distinct from EdgarClient's upstream write-through cache key. See
    ``FilingMetadata.r2_cache_key`` for why both keys are retained by design (#12).
    """
    session = aioboto3.Session(
        aws_access_key_id=settings.r2_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),
    )
    async with session.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url,
        region_name="auto",
        config=AioConfig(signature_version="s3v4"),
    ) as r2:
        await r2.put_object(
            Bucket=settings.r2_bucket_name, Key=key, Body=body, ContentType="text/html"
        )


# ---- CLI (argparse, D4) -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Parser with subcommands: `discover`; `run` (accepts `--limit N`, int)."""
    parser = argparse.ArgumentParser(
        prog="python -m alphalens.etl.runner",
        description="AlphaLens ETL runner",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    subparsers.add_parser("discover", help="Enqueue 10-K/10-Q filings from EDGAR")
    run_sub = subparsers.add_parser("run", help="Process pending filings end-to-end")
    run_sub.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap filings processed this invocation (default: all)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entrypoint: parse args, dispatch to discover/run via asyncio.run().

    Exit-code contract (for schedulers / CI):
      0 — success
      2 — config/validation error (NOT retryable; fix the config)
      1 — unexpected runtime error (may be transient; safe to retry next run)
    Wired as `python -m alphalens.etl.runner`."""
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
        logging.basicConfig(
            level=settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        if args.cmd == "discover":
            _log.info("discover complete: %s", asyncio.run(discover(settings)))
        else:
            _log.info("run complete: %s", asyncio.run(run(settings, limit=args.limit)))
        return 0
    except ValidationError as exc:
        # Config/validation failure (missing/invalid env). Not retryable.
        _log.error("Config error — not retryable: %s", exc)
        return 2
    except Exception:
        # Unexpected runtime error (incl. transient ValueError) — retryable.
        _log.exception("Unexpected runtime error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
