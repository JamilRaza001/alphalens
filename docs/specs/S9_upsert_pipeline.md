# Spec S9 -- Upsert Pipeline

**Maps to:** v8 spec 07 (`07_upsert_pipeline.md`) -> `src/alphalens/etl/upsert.py`
**Depends on:** S2 (live `chunks` schema), S7 (`Chunk`), S8 (`EmbeddingResult`), one-time migration (below)
**Status:** authored, not implemented

## Goal

Persist a filing's chunks and their embeddings into the `chunks` table: batched, idempotent, and corpus-consistent. The flow has two seams. First, `assemble_embedded_chunks` zips the chunker's `Chunk` objects with the matching vectors from S8's `EmbeddingResult` into self-contained `EmbeddedChunk` records -- validating count and dimension, stamping `filing_id` and `embedding_model_version`, and re-indexing the per-section `chunk_index` into a filing-global contiguous sequence (the live `UNIQUE (filing_id, chunk_index)` constraint requires global uniqueness). Second, `upsert_embedded_chunks` writes those records in batches via `INSERT ... ON CONFLICT (filing_id, chunk_index) DO UPDATE`, so re-running is safe. A single corpus must use one embedding model; the upsert refuses to mix `jina-v3` and `nomic-embed-text-v1.5` vectors (cosine search across models is invalid). Adding the missing `section_order` and `metadata` columns is a one-time additive migration, kept separate from the runtime path.

## Function Signatures

```python
from typing import Any, Literal, Sequence
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict

from alphalens.etl.chunker import Chunk
from alphalens.etl.embeddings import EmbeddingResult

ModelVersion = Literal["jina-v3", "nomic-embed-text-v1.5"]


class EmbeddedChunk(BaseModel):
    """A chunk paired with its embedding and filing identity -- self-contained, correct by construction."""
    model_config = ConfigDict(frozen=True)
    filing_id: UUID
    text: str
    token_count: int
    section: str
    section_order: int
    chunk_index: int                       # filing-global, 0-based, contiguous (re-indexed)
    embedding: list[float]                 # length == 768
    embedding_model_version: ModelVersion
    metadata: dict[str, Any]               # retains chunker metadata + "section_chunk_index"


class UpsertResult(BaseModel):
    """Outcome summary of one upsert call."""
    model_config = ConfigDict(frozen=True)
    filing_id: UUID
    rows_written: int                      # inserted + updated
    model_version: ModelVersion


class CorpusModelConflict(RuntimeError):
    """Raised when an upsert would mix two embedding models in one corpus."""


def assemble_embedded_chunks(
    filing_id: UUID,
    chunks: Sequence[Chunk],
    embeddings: EmbeddingResult,
) -> list[EmbeddedChunk]:
    """Zip chunks with vectors into validated, self-consistent records; re-index chunk_index globally."""


async def register_pgvector(conn: asyncpg.Connection) -> None:
    """Register the pgvector codec on a connection so list[float] binds to vector(768). Call on pool init."""


async def detect_corpus_model(conn: asyncpg.Connection) -> ModelVersion | None:
    """Return an embedding_model_version already present in chunks, or None if the table is empty."""


async def upsert_embedded_chunks(
    conn: asyncpg.Connection,
    records: Sequence[EmbeddedChunk],
    *,
    batch_size: int = 500,
) -> UpsertResult:
    """Batched, idempotent upsert into chunks via ON CONFLICT (filing_id, chunk_index). One model per corpus."""
```

## Acceptance Criteria

1. `assemble_embedded_chunks` raises `ValueError` when `len(chunks) != len(embeddings.vectors)`.
2. `assemble_embedded_chunks` raises `ValueError` when any vector length != 768.
3. Output records preserve input order; `chunk_index` equals list position (filing-global, 0-based, contiguous).
4. Each record's original per-section index is stored under `metadata["section_chunk_index"]`; all other chunker metadata keys are retained unchanged.
5. Every record carries the passed `filing_id` and `embeddings.model_version`.
6. With `records == []`, `upsert_embedded_chunks` performs no DB write and returns `rows_written == 0`.
7. Inserting N brand-new records writes N rows and returns `rows_written == N`.
8. Re-running the exact same records leaves the table row count unchanged (idempotent) and updates the conflicting rows in place via `ON CONFLICT (filing_id, chunk_index) DO UPDATE`.
9. The `ON CONFLICT` update refreshes `text, token_count, section, section_order, embedding, embedding_model_version, metadata`.
10. A round-trip read of `embedding` returns exactly 768 floats (pgvector codec registered on the connection).
11. Each stored row's `embedding_model_version` matches its source record.
12. Inserts are issued in batches of at most `batch_size` rows.
13. If `detect_corpus_model(conn)` returns a model different from the records' model, `upsert_embedded_chunks` raises `CorpusModelConflict` and writes nothing.
14. After the one-time migration, `section_order` and `metadata` columns exist and assembly populates both on every row.

## Gotchas

- **pgvector codec:** must be registered on the asyncpg connection (`from pgvector.asyncpg import register_vector; await register_vector(conn)`), ideally in the pool `init` callback -- not per row. Without it, `list[float]` will not bind to `vector(768)`.
- **ON CONFLICT target:** must match the live unique constraint `chunks_filing_id_chunk_index_key` on `(filing_id, chunk_index)` -- verified against the live DB (`\d chunks`), NOT the stale `scripts/create_schema.sql`.
- **Document order required:** the chunker emits `chunk_index` per-section (resets each section). Assembly re-indexes globally, which is correct ONLY if the input `chunks` list is in document order (the chunker's natural emission order across sections). Maintain/assert this ordering upstream.
- **JSONB encoding:** `metadata` is JSONB; encode the dict explicitly (e.g. `json.dumps(...)` bound with a `$n::jsonb` cast, or register a JSONB codec). asyncpg will not auto-encode a Python dict to JSONB.
- **One model per corpus:** mixing `jina-v3` and `nomic-embed-text-v1.5` vectors breaks cosine search (the spaces are not comparable). The quota-driven jina->nomic switch is an operational re-embed/backfill of the whole corpus, never a silent mid-corpus mix.

## One-Time Migration (run once, separate from the runtime module)

```sql
-- Adds the two columns the live schema lacks. Idempotent. Run via psql on NEON_DIRECT_URL.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS section_order INT NOT NULL DEFAULT 0;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
-- Table is empty pre-ETL, so the DEFAULTs are a safety convenience; assembly always writes real values.
```

Then reconcile the docs (resolves cleanup #14 / #15):
- Update `scripts/create_schema.sql` so a fresh provision includes `section_order` + `metadata` and matches the live schema.
- Fix the S7 module docstring label: "S8 migration adds it" -> "S9 migration adds it".

## Test Notes

- Offline unit tests: fake/mock `asyncpg.Connection` (record executed SQL + args) for assembly + batching + idempotency + corpus-guard logic; no real DB.
- One gated integration test (`RUN_INTEGRATION=1`) against real Neon: register pgvector, upsert a small batch, read back, assert 768d round-trip + idempotent re-run + `CorpusModelConflict` on a second model.
