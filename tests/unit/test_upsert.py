"""Unit tests for src/alphalens/etl/upsert.py (S9).

Coverage: AC#1–14 from docs/specs/S9_upsert_pipeline.md, plus the mixed-model-version guard.

Offline + deterministic: asyncpg.Connection mocked via a minimal _FakeConn stand-in (no real DB).
asyncio_mode=auto: async tests run without @pytest.mark.asyncio.
One gated integration test (RUN_INTEGRATION=1) against real Neon validates pgvector codec,
JSONB round-trip, FK chain, ON CONFLICT behaviour, and CorpusModelConflict under live data.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID

import pytest
from alphalens.etl.chunker import Chunk
from alphalens.etl.embeddings import EmbeddingResult
from alphalens.etl.upsert import (
    CorpusModelConflict,
    EmbeddedChunk,
    assemble_embedded_chunks,
    upsert_embedded_chunks,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_DIM = 768
_JINA = "jina-v3"
_NOMIC = "nomic-embed-text-v1.5"
_FILING_ID = UUID("cafecafe-cafe-cafe-cafe-cafecafecafe")

# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_chunk(
    *,
    section: str = "Item 1",
    section_order: int = 0,
    chunk_index: int = 0,
    metadata: dict[str, Any] | None = None,
) -> Chunk:
    return Chunk(
        text=f"text for chunk {chunk_index}",
        token_count=10,
        section=section,
        section_order=section_order,
        chunk_index=chunk_index,
        metadata=metadata if metadata is not None else {"item_key": "item1"},
    )


def _make_embedding_result(n: int, *, model: str = _JINA) -> EmbeddingResult:
    return EmbeddingResult(
        vectors=[[0.1] * _DIM for _ in range(n)],
        model_version=model,  # type: ignore[arg-type]
        tokens_used=n * 5,
    )


class _FakeConn:
    """Minimal asyncpg.Connection stand-in. Records executemany calls for offline assertions."""

    def __init__(self, *, existing_model: str | None = None) -> None:
        self._existing_model = existing_model
        self.executemany_calls: list[tuple[str, list[Any]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if self._existing_model is None:
            return None
        return {"embedding_model_version": self._existing_model}

    async def executemany(self, sql: str, args: Any) -> None:
        self.executemany_calls.append((sql, list(args)))


# ── AC#1 — count mismatch raises ValueError ───────────────────────────────────


def test_ac1_count_mismatch_raises() -> None:
    """AC#1: assemble raises ValueError when len(chunks) != len(vectors)."""
    chunks = [_make_chunk()]
    embeddings = _make_embedding_result(2)  # 2 vectors, 1 chunk
    with pytest.raises(ValueError, match="mismatch"):
        assemble_embedded_chunks(_FILING_ID, chunks, embeddings)


# ── AC#2 — wrong vector dimension raises ValueError ───────────────────────────


def test_ac2_wrong_vector_dim_raises() -> None:
    """AC#2: assemble raises ValueError when any vector length != 768."""
    chunk = _make_chunk()
    bad = EmbeddingResult(vectors=[[0.1] * 100], model_version=_JINA, tokens_used=5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="768"):
        assemble_embedded_chunks(_FILING_ID, [chunk], bad)


# ── AC#3 — output order preserved; chunk_index == list position ───────────────


def test_ac3_order_and_global_index() -> None:
    """AC#3: records are in input order; chunk_index == list position (filing-global)."""
    chunks = [
        _make_chunk(section="Item 1", section_order=0, chunk_index=0),
        _make_chunk(section="Item 1", section_order=0, chunk_index=1),
        _make_chunk(section="Item 2", section_order=1, chunk_index=0),  # per-section reset
    ]
    records = assemble_embedded_chunks(_FILING_ID, chunks, _make_embedding_result(3))
    assert len(records) == 3
    for i, rec in enumerate(records):
        assert rec.chunk_index == i, f"records[{i}].chunk_index = {rec.chunk_index}, expected {i}"


# ── AC#4 — section_chunk_index stored; all chunker metadata retained ──────────


def test_ac4_metadata_section_chunk_index() -> None:
    """AC#4: original per-section chunk_index saved under metadata['section_chunk_index'];
    all other chunker metadata keys are retained unchanged."""
    chunk = _make_chunk(
        chunk_index=3,
        metadata={"item_key": "item1a", "part": "Part I"},
    )
    records = assemble_embedded_chunks(_FILING_ID, [chunk], _make_embedding_result(1))
    meta = records[0].metadata
    assert meta["section_chunk_index"] == 3
    assert meta["item_key"] == "item1a"
    assert meta["part"] == "Part I"


# ── AC#5 — filing_id and model_version stamped on every record ────────────────


def test_ac5_filing_id_and_model_stamped() -> None:
    """AC#5: every record carries the passed filing_id and embeddings.model_version."""
    from uuid import uuid4

    fid = uuid4()
    chunks = [_make_chunk(chunk_index=i) for i in range(2)]
    records = assemble_embedded_chunks(fid, chunks, _make_embedding_result(2, model=_NOMIC))
    for rec in records:
        assert rec.filing_id == fid
        assert rec.embedding_model_version == _NOMIC


# ── AC#6 — empty records → no DB write, rows_written == 0 ────────────────────


async def test_ac6_empty_records_no_write() -> None:
    """AC#6: upsert_embedded_chunks with records==[] writes nothing and returns rows_written=0."""
    conn = _FakeConn()
    result = await upsert_embedded_chunks(conn, [])  # type: ignore[arg-type]
    assert result.rows_written == 0
    assert result.filing_id is None
    assert result.model_version is None
    assert conn.executemany_calls == []


# ── AC#7 — N new records → rows_written == N ─────────────────────────────────


async def test_ac7_insert_n_records() -> None:
    """AC#7: inserting N brand-new records returns rows_written == N."""
    conn = _FakeConn(existing_model=None)
    chunks = [_make_chunk(chunk_index=i) for i in range(3)]
    records = assemble_embedded_chunks(_FILING_ID, chunks, _make_embedding_result(3))
    result = await upsert_embedded_chunks(conn, records)  # type: ignore[arg-type]
    assert result.rows_written == 3
    assert result.filing_id == _FILING_ID
    assert result.model_version == _JINA
    assert len(conn.executemany_calls) == 1  # 3 records < default batch_size=500


# ── AC#8 — idempotent re-run ──────────────────────────────────────────────────


async def test_ac8_idempotent_rerun() -> None:
    """AC#8: re-running the same records with the same model does not conflict."""
    conn_first = _FakeConn(existing_model=None)
    chunks = [_make_chunk(chunk_index=i) for i in range(2)]
    records = assemble_embedded_chunks(_FILING_ID, chunks, _make_embedding_result(2))
    result1 = await upsert_embedded_chunks(conn_first, records)  # type: ignore[arg-type]
    assert result1.rows_written == 2

    # Second run: corpus now has jina-v3 rows; same model → no conflict
    conn_second = _FakeConn(existing_model=_JINA)
    result2 = await upsert_embedded_chunks(conn_second, records)  # type: ignore[arg-type]
    assert result2.rows_written == 2


# ── AC#9 — ON CONFLICT DO UPDATE refreshes the 7 mutable columns ──────────────


async def test_ac9_upsert_sql_refreshed_columns() -> None:
    """AC#9: the SQL issued to the DB contains ON CONFLICT … DO UPDATE with all 7 refreshed columns."""
    conn = _FakeConn(existing_model=None)
    records = assemble_embedded_chunks(_FILING_ID, [_make_chunk()], _make_embedding_result(1))
    await upsert_embedded_chunks(conn, records)  # type: ignore[arg-type]

    assert conn.executemany_calls, "executemany must be called for non-empty records"
    sql, _ = conn.executemany_calls[0]
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql
    for col in (
        "text",
        "token_count",
        "section",
        "section_order",
        "embedding",
        "embedding_model_version",
        "metadata",
    ):
        assert col in sql, f"column {col!r} missing from DO UPDATE SET"


# ── AC#10 — embedding preserved through assembly (pgvector round-trip in integration) ──


def test_ac10_embedding_preserved_through_assembly() -> None:
    """AC#10: assemble_embedded_chunks preserves the vector exactly (768d, value-for-value)."""
    vector = [float(i % 100) / 100 for i in range(_DIM)]
    embeddings = EmbeddingResult(vectors=[vector], model_version=_JINA, tokens_used=5)  # type: ignore[arg-type]
    records = assemble_embedded_chunks(_FILING_ID, [_make_chunk()], embeddings)
    assert records[0].embedding == vector
    assert len(records[0].embedding) == _DIM


# ── AC#11 — embedding_model_version written per-row ───────────────────────────


async def test_ac11_model_version_per_row() -> None:
    """AC#11: every SQL row tuple carries the correct embedding_model_version at position $7 (index 6)."""
    conn = _FakeConn(existing_model=None)
    chunks = [_make_chunk(chunk_index=i) for i in range(2)]
    records = assemble_embedded_chunks(_FILING_ID, chunks, _make_embedding_result(2, model=_NOMIC))
    for rec in records:
        assert rec.embedding_model_version == _NOMIC

    result = await upsert_embedded_chunks(conn, records)  # type: ignore[arg-type]
    assert result.model_version == _NOMIC

    _, batch_rows = conn.executemany_calls[0]
    for row in batch_rows:
        assert row[6] == _NOMIC, f"expected {_NOMIC!r} at row[6], got {row[6]!r}"


# ── AC#12 — batching respects batch_size ──────────────────────────────────────


async def test_ac12_batching() -> None:
    """AC#12: inserts are issued in batches of at most batch_size rows."""
    conn = _FakeConn(existing_model=None)
    n = 7
    chunks = [_make_chunk(chunk_index=i) for i in range(n)]
    records = assemble_embedded_chunks(_FILING_ID, chunks, _make_embedding_result(n))
    await upsert_embedded_chunks(conn, records, batch_size=3)  # type: ignore[arg-type]

    # 7 records, batch_size=3 → batches of sizes [3, 3, 1]
    assert len(conn.executemany_calls) == 3
    assert len(conn.executemany_calls[0][1]) == 3
    assert len(conn.executemany_calls[1][1]) == 3
    assert len(conn.executemany_calls[2][1]) == 1


# ── AC#13 — CorpusModelConflict raised, no write ──────────────────────────────


async def test_ac13_corpus_model_conflict() -> None:
    """AC#13: when detect_corpus_model returns a different model, CorpusModelConflict is raised and
    no executemany call is made."""
    conn = _FakeConn(existing_model=_JINA)  # corpus has jina-v3
    chunks = [_make_chunk()]
    records = assemble_embedded_chunks(_FILING_ID, chunks, _make_embedding_result(1, model=_NOMIC))
    with pytest.raises(CorpusModelConflict):
        await upsert_embedded_chunks(conn, records)  # type: ignore[arg-type]
    assert conn.executemany_calls == [], "no DB write must occur after CorpusModelConflict"


# ── AC#14 — section_order and metadata present on every row ───────────────────


async def test_ac14_section_order_and_metadata_written() -> None:
    """AC#14: section_order ($4, index 3) and metadata as JSON ($9, index 8) are in every SQL row."""
    conn = _FakeConn(existing_model=None)
    chunk = _make_chunk(section="MD&A", section_order=2, chunk_index=0)
    records = assemble_embedded_chunks(_FILING_ID, [chunk], _make_embedding_result(1))

    assert records[0].section_order == 2
    assert "section_chunk_index" in records[0].metadata

    await upsert_embedded_chunks(conn, records)  # type: ignore[arg-type]
    _, batch_rows = conn.executemany_calls[0]
    row = batch_rows[0]

    assert row[3] == 2, f"section_order at index 3 should be 2, got {row[3]!r}"
    parsed: dict[str, Any] = json.loads(row[8])
    assert parsed["item_key"] == "item1"
    assert "section_chunk_index" in parsed


# ── Mixed-model guard — ValueError before corpus check ────────────────────────


async def test_guard_mixed_model_versions_raises() -> None:
    """Guard: mixing embedding_model_version values in one call raises ValueError before any DB access."""
    from uuid import uuid4

    fid = uuid4()
    rec_jina = EmbeddedChunk(
        filing_id=fid,
        text="a",
        token_count=1,
        section="S",
        section_order=0,
        chunk_index=0,
        embedding=[0.1] * _DIM,
        embedding_model_version=_JINA,  # type: ignore[arg-type]
        metadata={},
    )
    rec_nomic = EmbeddedChunk(
        filing_id=fid,
        text="b",
        token_count=1,
        section="S",
        section_order=0,
        chunk_index=1,
        embedding=[0.5] * _DIM,
        embedding_model_version=_NOMIC,  # type: ignore[arg-type]
        metadata={},
    )
    conn = _FakeConn()
    with pytest.raises(ValueError, match="model version"):
        await upsert_embedded_chunks(conn, [rec_jina, rec_nomic])  # type: ignore[arg-type]
    assert conn.executemany_calls == [], "no DB write must occur on mixed-model ValueError"


# ── Integration — real Neon DB (skip unless RUN_INTEGRATION=1) ────────────────

_skip_integration = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="set RUN_INTEGRATION=1 to run live integration tests",
)


@_skip_integration
@pytest.mark.integration
async def test_integration_upsert_round_trip() -> None:
    """Integration: pgvector codec, JSONB, FK chain, ON CONFLICT, and CorpusModelConflict against real Neon.

    Connects via neon_direct_url (non-PgBouncer) for explicit transaction support (L12).
    Wraps all writes in a rolled-back transaction — no persistent state left behind.
    """
    from datetime import date

    import asyncpg as _asyncpg
    from alphalens.config import get_settings
    from alphalens.etl.upsert import detect_corpus_model, register_pgvector

    settings = get_settings()
    conn: _asyncpg.Connection[Any] = await _asyncpg.connect(  # type: ignore[type-arg]
        settings.neon_direct_url.get_secret_value()
    )
    await register_pgvector(conn)

    tr = conn.transaction()
    await tr.start()
    try:
        # 1. FK chain: insert temp company + filing (all NOT NULL columns populated)
        await conn.execute(
            "INSERT INTO companies (ticker, name, cik) VALUES ($1, $2, $3)",
            "__S9TEST__",
            "S9 Integration Test",
            "9999999999",
        )
        filing_id: UUID = await conn.fetchval(
            "INSERT INTO filings"
            " (ticker, filing_type, filing_date, period_end, accession_number, r2_key)"
            " VALUES ($1, $2, $3, $4, $5, $6) RETURNING filing_id",
            "__S9TEST__",
            "10-K",
            date(2024, 1, 1),
            date(2023, 12, 31),
            "9999999999-24-000001",
            "test/s9/integration",
        )
        assert filing_id is not None

        # 2. Assemble 3 jina-v3 embedded chunks
        chunks = [
            Chunk(
                text=f"sentence {i}",
                token_count=5,
                section="Item 1",
                section_order=0,
                chunk_index=i,
                metadata={"item_key": "item1"},
            )
            for i in range(3)
        ]
        jina_embeddings = EmbeddingResult(
            vectors=[[float(i + 1) / 1000] * _DIM for i in range(3)],
            model_version="jina-v3",
            tokens_used=15,
        )
        records = assemble_embedded_chunks(filing_id, chunks, jina_embeddings)

        # 3. First upsert: 3 new rows
        result1 = await upsert_embedded_chunks(conn, records)  # type: ignore[arg-type]
        assert result1.rows_written == 3
        assert result1.filing_id == filing_id
        assert result1.model_version == "jina-v3"

        # 4. AC#10 pgvector round-trip: embeddings read back as 768-dim vectors
        db_rows = await conn.fetch(
            "SELECT embedding FROM chunks WHERE filing_id = $1 ORDER BY chunk_index",
            filing_id,
        )
        assert len(db_rows) == 3
        for db_row in db_rows:
            vec = db_row["embedding"]
            assert len(vec) == 768, f"expected 768d vector, got {len(vec)}d"

        # 5. AC#8 idempotent re-run: row count unchanged
        result2 = await upsert_embedded_chunks(conn, records)  # type: ignore[arg-type]
        assert result2.rows_written == 3
        count: int = await conn.fetchval(
            "SELECT COUNT(*) FROM chunks WHERE filing_id = $1", filing_id
        )
        assert count == 3

        # 6. AC#13 CorpusModelConflict: nomic records refused while corpus has jina-v3
        nomic_embeddings = EmbeddingResult(
            vectors=[[0.5] * _DIM for _ in range(3)],
            model_version="nomic-embed-text-v1.5",
            tokens_used=0,
        )
        nomic_records = assemble_embedded_chunks(filing_id, chunks, nomic_embeddings)
        with pytest.raises(CorpusModelConflict):
            await upsert_embedded_chunks(conn, nomic_records)  # type: ignore[arg-type]

        # 7. Row count still 3 after refused conflict
        count2: int = await conn.fetchval(
            "SELECT COUNT(*) FROM chunks WHERE filing_id = $1", filing_id
        )
        assert count2 == 3

        # Spot-check detect_corpus_model returns jina-v3 (table-wide)
        detected = await detect_corpus_model(conn)  # type: ignore[arg-type]
        assert detected == "jina-v3"

    finally:
        await tr.rollback()
        await conn.close()
