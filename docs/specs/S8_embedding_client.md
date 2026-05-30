# Spec S8 — Embedding Client (Jina v3 primary + nomic fallback)

> Maps to v8 spec **06** (`06_embedding_client.md`) → `src/alphalens/etl/embeddings.py`.
> Format: lightweight (Goal + Function Signatures + Acceptance Criteria + Gotchas), per L21.
> Locks decisions D-S8.1..5 (below). Resolves O6, O13.

---

## Goal

Turn S7 `Chunk.text` (and, at query time, a raw user query) into a `VECTOR(768)` embedding so
pgvector HNSW can do semantic similarity search. Two interchangeable backends sit behind one
async interface: **Jina v3** (HTTP API, primary, `dimensions=768` MRL truncation) and
**nomic-embed-text-v1.5** (local model, fallback, native 768d). Both emit exactly 768 floats, so the
DB schema / HNSW index never change between them — only the `embedding_model_version` tag differs.
The client exposes the path it used so the upsert layer can enforce single-model corpus consistency.
DB writes are **out of scope** (next spec, upsert).

## Locked Decisions

- **D-S8.1 (resolves O13):** Jina free grant = **10,000,000 tokens** (confirmed on dashboard).
  `.env`/settings `JINA_FREE_TIER_TOKENS=10000000`.
- **D-S8.2 (resolves O6):** Migration/fallback trigger = **80% of grant = 8,000,000 tokens**, OR a
  sticky flag set when a live Jina call returns a quota/payment error (402). `jina_quota_exceeded()`
  is advisory (does not itself re-embed) — orchestration decides corpus policy.
- **D-S8.3:** **Async throughout.** Jina via injected `httpx.AsyncClient`. nomic is a sync CPU model,
  wrapped in `asyncio.to_thread(...)` so it never blocks the event loop.
- **D-S8.4:** Backend asymmetry is encapsulated. Documents vs query map to different backend hints:
  Jina `task` param (`retrieval.passage` | `retrieval.query`); nomic **text prefix**
  (`"search_document: "` | `"search_query: "`). Callers only choose `embed_documents` vs `embed_query`.
- **D-S8.5:** Counter + nomic encoder + http client are **dependency-injected** (lazy default
  factories) → unit tests run fully offline/deterministic; module decoupled from upsert (S9) and deploy.

## Function Signatures

```python
from collections.abc import Callable
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from alphalens.config import Settings  # S4

EmbeddingModelVersion = Literal["jina-v3", "nomic-embed-text-v1.5"]

# Sync encoder: list[str] -> list[768-float vectors]. Wrapped via asyncio.to_thread (D-S8.3).
NomicEncoder = Callable[[list[str]], list[list[float]]]


class EmbeddingResult(BaseModel):
    """Immutable embedding batch result. `vectors` order matches input `texts` order."""
    model_config = ConfigDict(frozen=True)

    vectors: list[list[float]]            # each exactly len 768
    model_version: EmbeddingModelVersion  # which backend produced these
    tokens_used: int                      # Jina usage.total_tokens for this call; 0 for nomic


def default_nomic_encoder(model_name: str) -> NomicEncoder:
    """Lazy-load nomic SentenceTransformer once (module-global cache); return a sync encode fn."""


class EmbeddingClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,   # injectable for tests; owned only if created here
        nomic_encode: NomicEncoder | None = None,        # injectable for tests
        force_fallback: bool = False,                    # skip Jina entirely (nomic only)
    ) -> None:
        """Build client. Defaults: real async httpx + lazy nomic encoder. force_fallback for nomic-only."""

    async def embed_documents(self, texts: list[str]) -> EmbeddingResult:
        """Embed ingestion chunks (Jina task=retrieval.passage / nomic prefix 'search_document: ')."""

    async def embed_query(self, text: str) -> EmbeddingResult:
        """Embed one search query (Jina task=retrieval.query / nomic prefix 'search_query: ')."""

    def jina_quota_exceeded(self) -> bool:
        """True if cumulative tokens >= 80% of grant (D-S8.2) OR a prior Jina call hit a quota error."""

    @property
    def total_tokens_used(self) -> int:
        """Cumulative Jina tokens consumed this process lifetime."""

    async def aclose(self) -> None:
        """Close the owned httpx client (no-op if http_client was injected). Idempotent."""

    # --- private ---
    # async def _embed(self, texts, *, task, prefix) -> EmbeddingResult   # router: Jina else nomic
    # async def _jina_embed(self, texts, *, task) -> tuple[list[list[float]], int]  # httpx + tenacity
    # async def _nomic_embed(self, texts, *, prefix) -> list[list[float]] # asyncio.to_thread(encode)
```

## Acceptance Criteria

1. `embed_documents(texts)` returns an `EmbeddingResult` whose `vectors` has `len == len(texts)`, and **every** vector has `len == 768`.
2. `embed_query(text)` returns an `EmbeddingResult` with exactly **one** vector of `len == 768`.
3. **Order preserved:** output `vectors[i]` corresponds to input `texts[i]` even when the input is split across batches.
4. **Batching:** inputs longer than `settings.embedding_batch_size` are split into sequential batches; results concatenated in input order; a single `EmbeddingResult` returned.
5. **Jina request shape (when Jina path):** POST body includes `model=settings.jina_model`, `dimensions=768`, and `task` = `"retrieval.passage"` for documents / `"retrieval.query"` for query.
6. **nomic request shape (when fallback path):** each text is prefixed with `"search_document: "` (documents) or `"search_query: "` (query) **before** encoding; no `task` param.
7. `model_version` == `"jina-v3"` when Jina produced the batch, `"nomic-embed-text-v1.5"` when nomic did.
8. `jina_quota_exceeded()` returns `True` once `total_tokens_used >= 0.8 * settings.jina_free_tier_tokens` (8,000,000 of 10,000,000).
9. `force_fallback=True` **or** `jina_quota_exceeded()` truthy ⇒ Jina is **never** called (verify via injected http client never hit); nomic only.
10. **Retry vs flip:** transient Jina errors (HTTP 429 / 5xx / transport) are retried with capped exponential backoff (tenacity, ≤5 attempts); a Jina **quota** error (402) is **not** retried — the client sets the sticky fallback flag and re-embeds that batch via nomic, returning `model_version="nomic-embed-text-v1.5"`.
11. `total_tokens_used` increments by the Jina response `usage.total_tokens` after each successful Jina call; nomic calls add 0.
12. Empty input (`[]` to `embed_documents`) returns `vectors == []`, makes **no** Jina HTTP call and **no** nomic encode.
13. nomic encoding executes via `asyncio.to_thread` — the event loop is not blocked (testable: a concurrent coroutine makes progress during encode).
14. `aclose()` closes the client only if it was created internally; calling it twice does not raise.

## Gotchas

- **Doc/query model MUST match across a corpus.** Documents embedded with Jina and queries embedded with nomic = different vector spaces = meaningless cosine similarity. The fallback switch is safe only as a *corpus-wide* migration (backfill all docs → then switch query path, per v8 §8.4), **never** a per-call mismatch. The orchestration/upsert layer enforces this using `model_version`; this client only reports which path it used. Mid-ingestion auto-flip can silently create a mixed (broken) corpus — pre-flight the token budget before a bulk run.
- **nomic prefixes are asymmetric and mandatory.** Missing or swapped prefix throws **no error** — it silently degrades retrieval quality (L55-class silent fail). `search_document:` ≠ `search_query:`.
- **Convention mismatch between backends.** Jina v3 uses `task` + `dimensions` (NOT text prefixes). nomic uses text prefixes (NOT a `task` param). Do not cross the wires.
- **nomic weights are heavy** (~550 MB, ~1.5–2 GB RAM). Lazy-load once, cache module-global (S6/S7 pattern). The S7 integration run already pulled the nomic *tokenizer*; the *embedding weights* are a separate, larger download — first fallback call triggers it. Lambda memory ≥ 3 GB (setup-guide troubleshooting).
- **Assert 768 at the boundary.** Defensive `len(vec) == 768` check on both paths — a stray 1024d (forgot `dimensions`) would corrupt the HNSW index dimension contract.

## Out of Scope / Deferred

- DB upsert + `embedding_model_version` persistence → next spec (S9 upsert).
- Corpus migration/backfill orchestration (Jina→nomic) → operational, triggered at the ETL run when `jina_quota_exceeded()`; mechanism lives in upsert/ETL, not here.
- Reranker embeddings — separate concern (in-process cross-encoder, agent side).
