"""AlphaLens v8 — Upsert Pipeline (S9).

Persists a filing's embedded chunks into the ``chunks`` table: batched, idempotent,
and corpus-consistent.  Depends on S7 (Chunk), S8 (EmbeddingResult), and the live
schema produced by S2 + the S9 one-time migration.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any, Literal, cast
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict

from alphalens.etl.chunker import Chunk
from alphalens.etl.embeddings import EmbeddingResult

_log = logging.getLogger(__name__)

# A pool-acquired connection is a PoolConnectionProxy; a raw connection (e.g. the
# create_pool `init=` callback) is a Connection. These helpers accept either.
type DbConn = asyncpg.Connection[asyncpg.Record] | asyncpg.pool.PoolConnectionProxy[asyncpg.Record]

_EMBEDDING_DIM: int = 768

ModelVersion = Literal["jina-v3", "nomic-embed-text-v1.5"]

_UPSERT_SQL = """
INSERT INTO chunks (
    filing_id, chunk_index, section, section_order, text,
    embedding, embedding_model_version, token_count, metadata
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
ON CONFLICT (filing_id, chunk_index)
DO UPDATE SET
    text                    = EXCLUDED.text,
    token_count             = EXCLUDED.token_count,
    section                 = EXCLUDED.section,
    section_order           = EXCLUDED.section_order,
    embedding               = EXCLUDED.embedding,
    embedding_model_version = EXCLUDED.embedding_model_version,
    metadata                = EXCLUDED.metadata
"""


class EmbeddedChunk(BaseModel):
    """A chunk paired with its embedding and filing identity — self-contained, correct by construction."""

    model_config = ConfigDict(frozen=True)

    filing_id: UUID
    text: str
    token_count: int
    section: str
    section_order: int
    chunk_index: int  # filing-global, 0-based, contiguous (re-indexed by assemble_embedded_chunks)
    embedding: list[float]  # length == 768
    embedding_model_version: ModelVersion
    metadata: dict[str, Any]  # retains chunker metadata + "section_chunk_index"


class UpsertResult(BaseModel):
    """Outcome summary of one upsert call."""

    model_config = ConfigDict(frozen=True)

    filing_id: UUID | None  # None when records == []
    rows_written: int  # inserted + updated; ON CONFLICT DO UPDATE counts every row
    model_version: ModelVersion | None  # None when records == []


class CorpusModelConflict(RuntimeError):
    """Raised when an upsert would mix two embedding models in one corpus."""


def assemble_embedded_chunks(
    filing_id: UUID,
    chunks: Sequence[Chunk],
    embeddings: EmbeddingResult,
) -> list[EmbeddedChunk]:
    """Zip chunks with vectors into validated, self-consistent records; re-index chunk_index globally."""
    if len(chunks) != len(embeddings.vectors):
        raise ValueError(
            f"chunk/vector count mismatch: {len(chunks)} chunks vs {len(embeddings.vectors)} vectors"
        )

    result: list[EmbeddedChunk] = []
    for i, (chunk, vector) in enumerate(zip(chunks, embeddings.vectors, strict=True)):
        if len(vector) != _EMBEDDING_DIM:
            raise ValueError(f"vector[{i}] has {len(vector)} dims, expected {_EMBEDDING_DIM}")
        meta: dict[str, Any] = {**chunk.metadata, "section_chunk_index": chunk.chunk_index}
        result.append(
            EmbeddedChunk(
                filing_id=filing_id,
                text=chunk.text,
                token_count=chunk.token_count,
                section=chunk.section,
                section_order=chunk.section_order,
                chunk_index=i,
                embedding=vector,
                embedding_model_version=embeddings.model_version,
                metadata=meta,
            )
        )
    return result


async def register_pgvector(conn: DbConn) -> None:
    """Register the pgvector codec on a connection so list[float] binds to vector(768). Call on pool init."""
    from pgvector.asyncpg import register_vector  # noqa: PLC0415

    await register_vector(conn)


async def detect_corpus_model(conn: DbConn) -> ModelVersion | None:
    """Return an embedding_model_version already present in chunks, or None if the table is empty."""
    row = await conn.fetchrow(
        "SELECT embedding_model_version FROM chunks "
        "WHERE embedding_model_version IS NOT NULL LIMIT 1"
    )
    if row is None:
        return None
    return cast(ModelVersion, row["embedding_model_version"])


async def upsert_embedded_chunks(
    conn: DbConn,
    records: Sequence[EmbeddedChunk],
    *,
    batch_size: int = 500,
) -> UpsertResult:
    """Batched, idempotent upsert into chunks via ON CONFLICT (filing_id, chunk_index). One model per corpus."""
    if not records:
        return UpsertResult(filing_id=None, rows_written=0, model_version=None)

    rec_list = list(records)

    # Caller-bug guard: every record in one call must share one embedding model
    model_versions = {rec.embedding_model_version for rec in rec_list}
    if len(model_versions) > 1:
        raise ValueError(
            f"all records in one upsert call must share one model version; got {model_versions!r}"
        )
    model_version: ModelVersion = next(iter(model_versions))
    filing_id: UUID = rec_list[0].filing_id

    # Corpus consistency guard: refuse to mix models table-wide
    existing = await detect_corpus_model(conn)
    if existing is not None and existing != model_version:
        raise CorpusModelConflict(
            f"corpus already uses {existing!r}; refusing to mix with {model_version!r}"
        )

    for start in range(0, len(rec_list), batch_size):
        batch = rec_list[start : start + batch_size]
        rows: list[tuple[Any, ...]] = [
            (
                rec.filing_id,
                rec.chunk_index,
                rec.section,
                rec.section_order,
                rec.text,
                rec.embedding,
                rec.embedding_model_version,
                rec.token_count,
                json.dumps(rec.metadata),
            )
            for rec in batch
        ]
        await conn.executemany(_UPSERT_SQL, rows)

    return UpsertResult(
        filing_id=filing_id,
        rows_written=len(rec_list),
        model_version=model_version,
    )
