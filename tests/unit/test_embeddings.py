"""Unit tests for src/alphalens/etl/embeddings.py (S8).

Coverage: AC#1–14 (all acceptance criteria from docs/specs/S8_embedding_client.md).

Offline + deterministic: Jina mocked via httpx.MockTransport (no respx);
nomic mocked via injected def (not lambda — ruff E731).
asyncio_mode=auto: async tests run without @pytest.mark.asyncio.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from alphalens.etl.embeddings import EmbeddingClient, EmbeddingResult
from alphalens.etl.rate_limit import TokenBucket

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIM = 768
_JINA_SENTINEL = 0.1  # value returned by the default fake Jina handler
_NOMIC_SENTINEL = 0.5  # value returned by the default fake nomic encoder

# ---------------------------------------------------------------------------
# Shared fakes / helpers
# ---------------------------------------------------------------------------


def _fake_jina_handler(request: httpx.Request) -> httpx.Response:
    """Default Jina mock: 200 with deterministic 768d vectors, index field included."""
    payload: dict[str, Any] = json.loads(request.content)
    n = len(payload["input"])
    return httpx.Response(
        200,
        json={
            "data": [{"index": i, "embedding": [_JINA_SENTINEL] * _DIM} for i in range(n)],
            "usage": {"total_tokens": n * 5},
        },
    )


def fake_nomic_encode(texts: list[str]) -> list[list[float]]:
    return [[_NOMIC_SENTINEL] * _DIM for _ in texts]


def _make_settings(batch_size: int = 128) -> MagicMock:
    s: MagicMock = MagicMock()
    s.jina_model = "jina-embeddings-v3"
    s.jina_dimensions = 768
    s.jina_free_tier_tokens = 10_000_000
    s.nomic_model = "nomic-ai/nomic-embed-text-v1.5"
    s.embedding_batch_size = batch_size
    s.jina_api_key.get_secret_value.return_value = "fake-key"
    s.jina_max_request_tokens = 6_000
    return s


def _make_client(
    *,
    jina_handler: Any = _fake_jina_handler,
    nomic_encode: Any = fake_nomic_encode,
    force_fallback: bool = False,
    batch_size: int = 128,
) -> EmbeddingClient:
    settings = _make_settings(batch_size=batch_size)
    transport = httpx.MockTransport(jina_handler)
    http_client = httpx.AsyncClient(transport=transport)
    # Inject a per-test bucket so existing tests never touch the module-level
    # singleton and remain isolated across event loops.
    test_bucket = TokenBucket(capacity=10_000_000, refill_per_sec=10_000_000)
    return EmbeddingClient(
        settings,
        http_client=http_client,
        nomic_encode=nomic_encode,
        force_fallback=force_fallback,
        bucket=test_bucket,
    )


# ---------------------------------------------------------------------------
# AC#1 — embed_documents shape
# ---------------------------------------------------------------------------


async def test_ac1_embed_documents_shape() -> None:
    """AC#1: len(vectors) == len(texts) and every vector is 768d."""
    client = _make_client()
    texts = ["alpha", "beta", "gamma"]
    result = await client.embed_documents(texts)
    assert isinstance(result, EmbeddingResult)
    assert len(result.vectors) == len(texts), f"expected {len(texts)}, got {len(result.vectors)}"
    for i, vec in enumerate(result.vectors):
        assert len(vec) == _DIM, f"vector[{i}] has dim {len(vec)}, expected {_DIM}"


# ---------------------------------------------------------------------------
# AC#2 — embed_query shape
# ---------------------------------------------------------------------------


async def test_ac2_embed_query_shape() -> None:
    """AC#2: embed_query returns exactly one 768d vector."""
    client = _make_client()
    result = await client.embed_query("what is revenue?")
    assert len(result.vectors) == 1, f"expected 1 vector, got {len(result.vectors)}"
    assert len(result.vectors[0]) == _DIM


# ---------------------------------------------------------------------------
# AC#3 — order preserved across batches
# ---------------------------------------------------------------------------


async def test_ac3_order_preserved_across_batches() -> None:
    """AC#3: output vectors[i] corresponds to input texts[i] even across batch boundaries."""
    call_inputs: list[list[str]] = []

    def ordered_handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = json.loads(request.content)
        batch = payload["input"]
        call_inputs.append(batch)
        n = len(batch)
        # Encode position-within-batch as the sentinel float so we can verify ordering
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [float(i)] * _DIM} for i in range(n)],
                "usage": {"total_tokens": n},
            },
        )

    # 5 texts, token_counts=3000 each, jina_max_request_tokens=6000 → 3 batches: [0,1], [2,3], [4]
    client = _make_client(jina_handler=ordered_handler)
    texts = ["t0", "t1", "t2", "t3", "t4"]
    result = await client.embed_documents(texts, token_counts=[3000] * 5)

    assert len(result.vectors) == 5
    # Each batch resets index to 0 — so vectors[0]==0.0, [1]==1.0, [2]==0.0, [3]==1.0, [4]==0.0
    expected_first_floats = [0.0, 1.0, 0.0, 1.0, 0.0]
    for i, (vec, exp) in enumerate(zip(result.vectors, expected_first_floats, strict=True)):
        assert vec[0] == exp, f"vectors[{i}][0] = {vec[0]}, expected {exp}"


# ---------------------------------------------------------------------------
# AC#4 — batching splits and concatenates
# ---------------------------------------------------------------------------


async def test_ac4_batching_splits_and_concatenates() -> None:
    """AC#4: inputs longer than batch_size are split; results concatenated; single result returned."""
    call_count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _fake_jina_handler(request)

    client = _make_client(jina_handler=counting_handler)
    texts = [
        "a",
        "b",
        "c",
        "d",
        "e",
    ]  # 5 texts, token_counts=3000 each, max_request_tokens=6000 → 3 batches
    result = await client.embed_documents(texts, token_counts=[3000] * 5)

    assert call_count == 3, f"expected 3 Jina calls, got {call_count}"
    assert len(result.vectors) == 5
    assert isinstance(result, EmbeddingResult)


# ---------------------------------------------------------------------------
# AC#5 — Jina request body shape
# ---------------------------------------------------------------------------


async def test_ac5_jina_request_body_documents() -> None:
    """AC#5: embed_documents sends task='retrieval.passage' and dimensions=768."""
    captured: list[dict[str, Any]] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _fake_jina_handler(request)

    client = _make_client(jina_handler=capture_handler)
    await client.embed_documents(["chunk text"])

    assert len(captured) == 1
    body = captured[0]
    assert body["task"] == "retrieval.passage"
    assert body["dimensions"] == 768
    assert body["model"] == "jina-embeddings-v3"
    assert body["input"] == ["chunk text"]


async def test_ac5_jina_request_body_query() -> None:
    """AC#5: embed_query sends task='retrieval.query'."""
    captured: list[dict[str, Any]] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _fake_jina_handler(request)

    client = _make_client(jina_handler=capture_handler)
    await client.embed_query("what is net income?")

    assert captured[0]["task"] == "retrieval.query"


# ---------------------------------------------------------------------------
# AC#6 — nomic prefix applied correctly
# ---------------------------------------------------------------------------


async def test_ac6_nomic_prefix_documents() -> None:
    """AC#6: nomic path prefixes each text with 'search_document: ' for embed_documents."""
    received: list[list[str]] = []

    def capturing_encode(texts: list[str]) -> list[list[float]]:
        received.append(texts)
        return [[_NOMIC_SENTINEL] * _DIM for _ in texts]

    client = _make_client(nomic_encode=capturing_encode, force_fallback=True)
    await client.embed_documents(["revenue grew", "costs declined"])

    assert len(received) == 1
    for t in received[0]:
        assert t.startswith("search_document: "), f"missing prefix: {t!r}"
    assert not any(t.startswith("search_query:") for t in received[0])


async def test_ac6_nomic_prefix_query() -> None:
    """AC#6: nomic path prefixes text with 'search_query: ' for embed_query."""
    received: list[list[str]] = []

    def capturing_encode(texts: list[str]) -> list[list[float]]:
        received.append(texts)
        return [[_NOMIC_SENTINEL] * _DIM for _ in texts]

    client = _make_client(nomic_encode=capturing_encode, force_fallback=True)
    await client.embed_query("what is EPS?")

    assert received[0][0].startswith("search_query: "), f"missing prefix: {received[0][0]!r}"


# ---------------------------------------------------------------------------
# AC#7 — model_version tag
# ---------------------------------------------------------------------------


async def test_ac7_model_version_jina() -> None:
    """AC#7: Jina path → model_version='jina-v3'."""
    result = await _make_client().embed_documents(["text"])
    assert result.model_version == "jina-v3"


async def test_ac7_model_version_nomic() -> None:
    """AC#7: nomic path → model_version='nomic-embed-text-v1.5'."""
    result = await _make_client(force_fallback=True).embed_documents(["text"])
    assert result.model_version == "nomic-embed-text-v1.5"


# ---------------------------------------------------------------------------
# AC#8 — quota threshold
# ---------------------------------------------------------------------------


async def test_ac8_quota_threshold() -> None:
    """AC#8: jina_quota_exceeded() flips True exactly at 8_000_000 tokens (80% of 10M)."""
    client = _make_client()
    # 7_999_999 tokens — just under threshold
    client._total_tokens = 7_999_999  # type: ignore[attr-defined]
    assert not client.jina_quota_exceeded(), "7_999_999 tokens should NOT exceed quota"

    client._total_tokens = 8_000_000  # type: ignore[attr-defined]
    assert client.jina_quota_exceeded(), "8_000_000 tokens should exceed quota"


# ---------------------------------------------------------------------------
# AC#9a — force_fallback never calls Jina
# ---------------------------------------------------------------------------


async def test_ac9_force_fallback_no_jina() -> None:
    """AC#9: force_fallback=True → Jina handler is never called."""
    handler_calls = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal handler_calls
        handler_calls += 1
        return _fake_jina_handler(request)

    client = _make_client(jina_handler=counting_handler, force_fallback=True)
    await client.embed_documents(["text1", "text2"])

    assert handler_calls == 0, f"Jina called {handler_calls} times with force_fallback=True"


# ---------------------------------------------------------------------------
# AC#9b — quota exceeded never calls Jina
# ---------------------------------------------------------------------------


async def test_ac9_quota_exceeded_no_jina() -> None:
    """AC#9: once quota exceeded (>= 8M tokens), Jina handler is never called."""
    handler_calls = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal handler_calls
        handler_calls += 1
        return _fake_jina_handler(request)

    client = _make_client(jina_handler=counting_handler)
    client._total_tokens = 8_000_000  # type: ignore[attr-defined]
    await client.embed_documents(["text"])

    assert handler_calls == 0, f"Jina called {handler_calls} times after quota exceeded"


# ---------------------------------------------------------------------------
# AC#10a — 429 is retried (real tenacity backoff ~3s)
# ---------------------------------------------------------------------------


async def test_ac10_retry_on_429() -> None:
    """AC#10: 429 is retried; succeeds on 3rd attempt. Real tenacity backoff (~3s, no mock)."""
    attempts = 0

    def flaky_429_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return _fake_jina_handler(request)

    client = _make_client(jina_handler=flaky_429_handler)
    result = await client.embed_documents(["text"])

    assert attempts == 3, f"expected 3 Jina calls (2 retries), got {attempts}"
    assert result.model_version == "jina-v3"
    assert len(result.vectors) == 1


# ---------------------------------------------------------------------------
# AC#10b — 402 flips to nomic, no retry, homogeneous result (FIX 1)
# ---------------------------------------------------------------------------


async def test_ac10_402_flips_to_nomic() -> None:
    """AC#10 + FIX 1: 402 on batch 2 → discard all Jina vectors, re-embed entire input via nomic.

    Setup: 3 texts, token_counts=3000 each, jina_max_request_tokens=6000
    → batch 1=[t0,t1] succeeds (200), batch 2=[t2] returns 402.
    Assert:
      - Final result has model_version='nomic-embed-text-v1.5'
      - ALL 3 vectors equal the nomic sentinel (0.5), none equal the Jina sentinel (0.1)
      - Jina handler called exactly 2× (batch 1 + batch 2 that returned 402; no retry on 402)
    """
    jina_call_count = 0

    def quota_on_second_batch(request: httpx.Request) -> httpx.Response:
        nonlocal jina_call_count
        jina_call_count += 1
        if jina_call_count == 1:
            return _fake_jina_handler(request)  # batch 1: success
        return httpx.Response(402, json={"error": "quota exceeded"})  # batch 2: quota

    client = _make_client(
        jina_handler=quota_on_second_batch,
        nomic_encode=fake_nomic_encode,
    )
    result = await client.embed_documents(["t0", "t1", "t2"], token_counts=[3000, 3000, 3000])

    assert (
        result.model_version == "nomic-embed-text-v1.5"
    ), f"expected nomic-embed-text-v1.5, got {result.model_version}"
    assert len(result.vectors) == 3, f"expected 3 vectors, got {len(result.vectors)}"
    for i, vec in enumerate(result.vectors):
        assert (
            vec == [_NOMIC_SENTINEL] * _DIM
        ), f"vectors[{i}] should be nomic sentinel ({_NOMIC_SENTINEL}), got {vec[0]}"
    assert (
        jina_call_count == 2
    ), f"Jina handler should be called exactly 2× (batch1+batch2 no retry), got {jina_call_count}"
    assert client.jina_quota_exceeded()


# ---------------------------------------------------------------------------
# AC#11 — token accumulation
# ---------------------------------------------------------------------------


async def test_ac11_tokens_accumulate() -> None:
    """AC#11: total_tokens_used accumulates across Jina calls; nomic adds 0."""
    client = _make_client()

    # embed_documents(["a","b"]) → 2 texts × 5 tokens = 10 tokens
    result1 = await client.embed_documents(["a", "b"])
    assert result1.tokens_used == 10
    assert client.total_tokens_used == 10

    # embed_documents(["c"]) → 1 text × 5 tokens = 5 tokens
    result2 = await client.embed_documents(["c"])
    assert result2.tokens_used == 5
    assert client.total_tokens_used == 15

    # nomic call adds 0
    client2 = _make_client(force_fallback=True)
    result3 = await client2.embed_documents(["x"])
    assert result3.tokens_used == 0
    assert client2.total_tokens_used == 0


# ---------------------------------------------------------------------------
# AC#12 — empty input
# ---------------------------------------------------------------------------


async def test_ac12_empty_input() -> None:
    """AC#12: embed_documents([]) returns empty vectors, makes no Jina or nomic calls."""
    jina_calls = 0
    nomic_calls = 0

    def counting_jina(request: httpx.Request) -> httpx.Response:
        nonlocal jina_calls
        jina_calls += 1
        return _fake_jina_handler(request)

    def counting_nomic(texts: list[str]) -> list[list[float]]:
        nonlocal nomic_calls
        nomic_calls += 1
        return []

    client = _make_client(jina_handler=counting_jina, nomic_encode=counting_nomic)
    result = await client.embed_documents([])

    assert result.vectors == []
    assert result.tokens_used == 0
    assert jina_calls == 0
    assert nomic_calls == 0


# ---------------------------------------------------------------------------
# AC#13 — nomic via asyncio.to_thread (FIX 2: ordering assertion)
# ---------------------------------------------------------------------------


async def test_ac13_nomic_via_to_thread() -> None:
    """AC#13: nomic encode runs in thread pool — event loop stays free during blocking encode.

    FIX 2: ordering list discriminates: with asyncio.to_thread the loop is free while
    encode blocks the thread, so concurrent() fires between encode_start and encode_end.
    Without to_thread (blocking), order would be [start, end, concurrent_ran] → test fails.
    """
    order: list[str] = []

    def slow_nomic(texts: list[str]) -> list[list[float]]:
        order.append("encode_start")
        time.sleep(0.1)  # blocks the thread — NOT the event loop
        order.append("encode_end")
        return [[_NOMIC_SENTINEL] * _DIM for _ in texts]

    async def concurrent() -> None:
        await asyncio.sleep(0.02)  # yields; fires mid-encode if loop is free
        order.append("concurrent_ran")

    client = _make_client(nomic_encode=slow_nomic, force_fallback=True)
    await asyncio.gather(client.embed_documents(["x"]), concurrent())

    assert order == ["encode_start", "concurrent_ran", "encode_end"], (
        f"Expected to_thread ordering, got: {order}. "
        "If 'encode_end' appears before 'concurrent_ran', asyncio.to_thread is missing."
    )


# ---------------------------------------------------------------------------
# AC#14 — aclose idempotent; owned vs injected
# ---------------------------------------------------------------------------


async def test_ac14_aclose_owned_idempotent() -> None:
    """AC#14: aclose() on an owned client closes it; calling twice does not raise."""
    client = _make_client()
    await client.aclose()
    await client.aclose()  # second call must be a no-op


async def test_ac14_aclose_injected_not_closed() -> None:
    """AC#14: aclose() does NOT close an injected http_client."""
    settings = _make_settings()
    transport = httpx.MockTransport(_fake_jina_handler)
    injected = httpx.AsyncClient(transport=transport)

    client = EmbeddingClient(settings, http_client=injected, nomic_encode=fake_nomic_encode)
    await client.aclose()

    # Injected client should still be usable (not closed)
    assert not injected.is_closed, "aclose() must not close an injected http_client"
    await injected.aclose()  # cleanup


# ---------------------------------------------------------------------------
# Spec 06a AC#13 — token-aware batching caps each Jina request
# ---------------------------------------------------------------------------


async def test_spec06a_ac13_token_aware_batching_limits_request_size() -> None:
    """Spec 06a AC#13: no Jina request body has summed token_count > jina_max_request_tokens."""
    captured_inputs: list[list[str]] = []

    def capture_handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = json.loads(request.content)
        captured_inputs.append(payload["input"])
        return _fake_jina_handler(request)

    settings = _make_settings()
    settings.jina_max_request_tokens = 5_000
    transport = httpx.MockTransport(capture_handler)
    http_client = httpx.AsyncClient(transport=transport)
    test_bucket = TokenBucket(capacity=10_000_000, refill_per_sec=10_000_000)
    client = EmbeddingClient(settings, http_client=http_client, bucket=test_bucket)

    texts = ["text_a", "text_b", "text_c", "text_d"]
    token_counts = [2000, 2000, 2000, 2000]  # naive single-batch sum=8000 > 5000
    tc_map = dict(zip(texts, token_counts, strict=True))

    result = await client.embed_documents(texts, token_counts=token_counts)

    assert len(result.vectors) == 4
    assert len(captured_inputs) >= 2, f"expected multiple batches, got {len(captured_inputs)}"
    for batch_inputs in captured_inputs:
        batch_sum = sum(tc_map[t] for t in batch_inputs)
        assert batch_sum <= 5_000, f"request exceeded cap: sum={batch_sum}, inputs={batch_inputs}"


# ---------------------------------------------------------------------------
# Integration — real Jina + real nomic (skip unless RUN_INTEGRATION=1)
# ---------------------------------------------------------------------------

_skip_integration = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="set RUN_INTEGRATION=1 to run live integration tests",
)


@_skip_integration
@pytest.mark.integration
async def test_integration_real_jina_and_nomic() -> None:
    """Integration: real Jina call (768d, tokens>0) + real nomic fallback (force_fallback=True).

    Note: real nomic weights (~550 MB) download on first run.
    """
    from alphalens.config import get_settings

    settings = get_settings()

    # Real Jina
    jina_client = EmbeddingClient(settings)
    try:
        result = await jina_client.embed_documents(["AlphaLens integration test chunk."])
        assert len(result.vectors) == 1
        assert len(result.vectors[0]) == 768, f"Jina dim = {len(result.vectors[0])}"
        assert result.tokens_used > 0, "Jina should report >0 tokens"
        assert result.model_version == "jina-v3"
    finally:
        await jina_client.aclose()

    # Real nomic (force_fallback=True — skips Jina entirely)
    nomic_client = EmbeddingClient(settings, force_fallback=True)
    try:
        result2 = await nomic_client.embed_documents(["AlphaLens nomic fallback test."])
        assert len(result2.vectors) == 1
        assert len(result2.vectors[0]) == 768, f"nomic dim = {len(result2.vectors[0])}"
        assert result2.tokens_used == 0
        assert result2.model_version == "nomic-embed-text-v1.5"
    finally:
        await nomic_client.aclose()
