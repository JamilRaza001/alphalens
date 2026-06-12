## Spec 06a — Token-Bucket Rate Limiter (Jina TPM)

### Goal

The Jina free tier enforces **100K tokens-per-minute (TPM), 100 RPM, 2 concurrent** — whichever trips first. The current embed path is **reactive only** (tenacity retries 429s), which cannot clear a filing whose token volume exceeds the per-minute cap *within a single filing* (the 3 JPM iXBRL 10-Qs at ~116K tokens each): every retry lands in the same over-quota minute and exhausts the attempt budget. This spec adds a **proactive async token bucket** in `embeddings.py` that paces outbound Jina embedding requests to stay under a configurable cap (default **90K TPM**, a ~10% safety margin below the 100K hard limit). It converts an intra-filing burst into a smooth, sustainable token stream so a large filing simply takes longer rather than failing. **Out of scope:** the iXBRL parser fix (deferred to v2) and the nomic fallback path (402 quota flip, unchanged — this bucket wraps the Jina path only).

### Function Signatures

```python
from collections.abc import Sequence


class TokenBucket:
    """Async token bucket pacing outbound tokens to a fixed per-minute rate."""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        """capacity = max burst (== TPM cap); refill_per_sec = capacity / 60."""

    @classmethod
    def from_tpm(cls, tpm: int) -> "TokenBucket":
        """Build a bucket whose burst and sustained rate both equal `tpm` per minute."""

    async def acquire(self, tokens: int) -> None:
        """Lazily refill, then block via asyncio.sleep until `tokens` are available
        and deduct them. Raises ValueError if tokens > capacity."""

    @property
    def available(self) -> float:
        """Current token count after applying any pending refill. For tests/observability."""


def get_jina_bucket() -> TokenBucket:
    """Process-wide lazy singleton built from settings.jina_tpm_limit."""


async def jina_embed(
    texts: list[str],
    token_counts: Sequence[int] | None = None,
    *,
    task: str = "retrieval.passage",
    bucket: TokenBucket | None = None,
) -> list[list[float]]:
    """Embed via Jina v3 (truncate_dim=768), pacing the request through the token
    bucket before the HTTP call. token_counts defaults to the project's existing
    token-count utility; bucket defaults to get_jina_bucket().
    task: Jina task string — "retrieval.passage" for document chunks,
    "retrieval.query" for search queries."""
```

### Acceptance Criteria

1. `acquire(n)` with `n <= available` returns **without sleeping** (no `asyncio.sleep`) and decrements `available` by `n`.
2. After the bucket is drained, `acquire(n)` sleeps ≈ `n / refill_per_sec` seconds (±1 refill tick) before returning — verified with **mocked time**, not wall-clock waiting.
3. Refill is **time-based and lazy** (recomputed from elapsed monotonic time on each `acquire`), capped at `capacity`. No background task or timer thread.
4. `acquire(n)` with `n > capacity` raises `ValueError` — a single request must fit under the cap; batching is the caller's responsibility.
5. `from_tpm(90000)` yields `capacity == 90000` and `refill_per_sec == 1500.0`.
6. `jina_embed` calls `await bucket.acquire(sum(token_counts))` **exactly once**, immediately before issuing the Jina HTTP request.
7. When `token_counts is None`, `jina_embed` derives counts from the project's existing token-count utility (the same one the chunker uses) so client-side pacing matches the stored `chunks.token_count`.
8. **Concurrency-safe:** `acquire` is guarded by an `asyncio.Lock`. A test issuing two concurrent `acquire` calls whose sum exceeds the current burst observes the second one being paced (total mocked-elapsed ≥ the expected refill wait), never both passing instantly.
9. `settings.jina_tpm_limit: int` exists (default `90000`) and is the **single source of truth**; `get_jina_bucket()` reads it.
10. Gates green: `ruff` (format + lint), `mypy --strict`, `pytest`. New module `tests/unit/test_token_bucket.py` covers criteria 1–5 and 8 using mocked time (no real sleeping).
11. The nomic fallback path and the existing 402→fallback flip are **unchanged**.
12. `jina_embed` forwards `task` verbatim to the Jina request body. Default is `"retrieval.passage"`. The query embedding path calls with `task="retrieval.query"`.

### Gotchas (optional)

- **Hold the lock across the sleep.** Releasing the `asyncio.Lock` before `await asyncio.sleep(...)` lets a second coroutine refill-and-deduct against stale state → cap overshoot. The critical section is refill + (optional sleep) + deduct, all under the lock. Throughput serializes — which is exactly what a shared rate limit wants.
- **Monotonic clock only.** Use `asyncio.get_running_loop().time()` (or `time.monotonic()`), never `time.time()`. Wall-clock jumps (NTP, host suspend) corrupt refill math.
- **Token counts are an estimate.** Jina's server-side tokenizer may differ slightly from the project counter; the 90K-vs-100K margin absorbs the drift. Don't chase exact parity.
- **A 116K-token filing still won't fit in one minute — and shouldn't.** The bucket paces it to ~77s of embedding instead of failing. That is the intended behavior, not a regression.
