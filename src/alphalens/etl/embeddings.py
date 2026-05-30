"""AlphaLens v8 — Embedding client (S8).

Jina v3 primary (HTTP, dimensions=768 MRL) + nomic-embed-text-v1.5 fallback (local, native 768d).
Both paths emit exactly 768 floats — the pgvector HNSW index never changes between them.
DB writes are out of scope (S9 upsert).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from alphalens.config import Settings

_log = logging.getLogger(__name__)

_nomic_model: Any = None  # module-level lazy-load sentinel (SentenceTransformer)

# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

EmbeddingModelVersion = Literal["jina-v3", "nomic-embed-text-v1.5"]
NomicEncoder = Callable[[list[str]], list[list[float]]]

# ---------------------------------------------------------------------------
# Module-level retry predicate (mirrors edgar._is_retryable)
# ---------------------------------------------------------------------------


def _is_retryable_jina(exc: BaseException) -> bool:
    """True for 429 and 5xx — NOT for 402 (quota error, handled by caller)."""
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------


class EmbeddingResult(BaseModel):
    """Immutable embedding batch result. `vectors` order matches input `texts` order."""

    model_config = ConfigDict(frozen=True)

    vectors: list[list[float]]  # each exactly len 768
    model_version: EmbeddingModelVersion  # which backend produced these
    tokens_used: int  # Jina usage.total_tokens for this call; 0 for nomic


# ---------------------------------------------------------------------------
# Default factory
# ---------------------------------------------------------------------------


def default_nomic_encoder(model_name: str) -> NomicEncoder:
    """Lazy-load nomic SentenceTransformer once (module-global cache); return a sync encode fn."""
    global _nomic_model
    if _nomic_model is None:
        from sentence_transformers import SentenceTransformer

        _nomic_model = SentenceTransformer(model_name, trust_remote_code=True)
        _log.debug("nomic SentenceTransformer loaded: %s", model_name)
    model = _nomic_model

    def _encode(texts: list[str]) -> list[list[float]]:
        embeddings = model.encode(texts, convert_to_numpy=True)
        return [list(map(float, e)) for e in embeddings]

    return _encode


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class EmbeddingClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        nomic_encode: NomicEncoder | None = None,
        force_fallback: bool = False,
    ) -> None:
        self._settings = settings
        self._own_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._nomic_encode: NomicEncoder | None = nomic_encode  # None → lazy-load on first fallback
        self._force_fallback = force_fallback
        self._total_tokens: int = 0
        self._quota_exceeded: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def embed_documents(self, texts: list[str]) -> EmbeddingResult:
        """Embed ingestion chunks (Jina task=retrieval.passage / nomic prefix 'search_document: ')."""
        return await self._embed(texts, task="retrieval.passage", prefix="search_document: ")

    async def embed_query(self, text: str) -> EmbeddingResult:
        """Embed one search query (Jina task=retrieval.query / nomic prefix 'search_query: ')."""
        return await self._embed([text], task="retrieval.query", prefix="search_query: ")

    def jina_quota_exceeded(self) -> bool:
        """True if cumulative tokens >= 80% of grant (D-S8.2) OR a prior Jina call hit 402."""
        return self._quota_exceeded or self._total_tokens >= int(
            0.8 * self._settings.jina_free_tier_tokens
        )

    @property
    def total_tokens_used(self) -> int:
        """Cumulative Jina tokens consumed this process lifetime."""
        return self._total_tokens

    async def aclose(self) -> None:
        """Close the owned httpx client. No-op if http_client was injected. Idempotent."""
        if self._own_http:
            await self._http.aclose()
            self._own_http = False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _embed(self, texts: list[str], *, task: str, prefix: str) -> EmbeddingResult:
        """Route to Jina or nomic; batch; return a homogeneous EmbeddingResult."""
        if not texts:
            # AC#12: empty input — no HTTP call, no encode
            return EmbeddingResult(vectors=[], model_version="jina-v3", tokens_used=0)

        use_jina = not self._force_fallback and not self.jina_quota_exceeded()
        batch_size = self._settings.embedding_batch_size

        if use_jina:
            jina_vectors: list[list[float]] = []
            call_tokens = 0
            try:
                for i in range(0, len(texts), batch_size):
                    vecs, tok = await self._jina_embed(texts[i : i + batch_size], task=task)
                    jina_vectors.extend(vecs)
                    call_tokens += tok
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 402:
                    # FIX 1: discard all collected Jina vectors; re-embed entire input via nomic.
                    # Ensures homogeneous result — no mixed jina+nomic vectors under one tag.
                    self._quota_exceeded = True
                    # fall through to nomic path below
                else:
                    raise
            else:
                # Jina path fully succeeded — commit tokens and return
                self._total_tokens += call_tokens
                self._validate_dims(jina_vectors)
                return EmbeddingResult(
                    vectors=jina_vectors,
                    model_version="jina-v3",
                    tokens_used=call_tokens,
                )

        # Nomic path: force_fallback, quota exceeded before call, or post-402 flip.
        # Entire original `texts` re-embedded — homogeneous result guaranteed.
        nomic_vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            nomic_vectors.extend(await self._nomic_embed(texts[i : i + batch_size], prefix=prefix))
        self._validate_dims(nomic_vectors)
        return EmbeddingResult(
            vectors=nomic_vectors,
            model_version="nomic-embed-text-v1.5",
            tokens_used=0,
        )

    @retry(
        retry=retry_if_exception(_is_retryable_jina),
        wait=wait_exponential_jitter(initial=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _jina_embed(self, texts: list[str], *, task: str) -> tuple[list[list[float]], int]:
        """POST to Jina v3 API; tenacity retries 429/5xx up to 5x; 402 propagates immediately."""
        r = await self._http.post(
            "https://api.jina.ai/v1/embeddings",
            headers={"Authorization": f"Bearer {self._settings.jina_api_key.get_secret_value()}"},
            json={
                "model": self._settings.jina_model,
                "input": texts,
                "dimensions": self._settings.jina_dimensions,
                "task": task,
            },
        )
        r.raise_for_status()
        body: dict[str, Any] = r.json()
        # FIX 3: sort by per-item index — real Jina API does not guarantee response order
        items = sorted(body["data"], key=lambda d: d["index"])
        vectors: list[list[float]] = [item["embedding"] for item in items]
        tokens: int = body["usage"]["total_tokens"]
        return vectors, tokens

    def _get_nomic_encoder(self) -> NomicEncoder:
        """Lazy-load nomic encoder on first fallback call; return injected encoder immediately."""
        if self._nomic_encode is None:
            self._nomic_encode = default_nomic_encoder(self._settings.nomic_model)
        return self._nomic_encode

    async def _nomic_embed(self, texts: list[str], *, prefix: str) -> list[list[float]]:
        """Prefix texts and encode via asyncio.to_thread — never blocks the event loop (D-S8.3)."""
        prefixed = [f"{prefix}{t}" for t in texts]
        encode = self._get_nomic_encoder()
        result: list[list[float]] = await asyncio.to_thread(encode, prefixed)
        return result

    def _validate_dims(self, vectors: list[list[float]]) -> None:
        """AC#1, AC#2: every vector must be exactly 768d (both Jina and nomic paths)."""
        for vec in vectors:
            if len(vec) != 768:
                raise ValueError(f"Expected 768-dim vector, got {len(vec)}")
