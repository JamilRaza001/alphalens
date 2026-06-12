## Spec 06a — Token-Bucket Rate Limiter + Token-Aware Batching (Jina TPM)

### Goal

The Jina free tier enforces **100K tokens-per-minute (TPM)** as a rolling 60-second
**sum** (plus 100 RPM, 2 concurrent — not binding here). A purely reactive path
(tenacity retries on 429) cannot clear a filing whose token volume exceeds the
per-minute cap within a single filing (the 3 JPM iXBRL 10-Qs at ~116K tokens each):
every retry lands in the same over-quota minute and exhausts the attempt budget.

Two independent controls are required, because Jina enforces a **windowed sum**, not
an average:

1. **Proactive pacing** — an async token bucket that limits the sustained outbound
   rate to `jina_tpm_limit / 60` tokens/sec. The Jina bucket starts **empty** so a
   fresh process is never granted an initial burst.
2. **Token-aware request sizing** — each Jina request is bounded to
   `jina_max_request_tokens` summed tokens, so individual requests are small "lumps".
   Pacing alone is insufficient: with fixed *count* batching (128 chunks ~ ~50K
   tokens/request), two paced requests can still land inside one rolling minute and
   exceed 100K.

**Safety invariant:** `jina_tpm_limit + jina_max_request_tokens <= 100_000`. The worst
case in any 60-second window is `(rate x 60) + one_request = jina_tpm_limit +
jina_max_request_tokens`. With defaults `90_000 + 6_000 = 96_000 <= 100_000`.

**Out of scope:** the iXBRL parser fix (deferred to v2 — its blob-then-recursive-split
is the reason these filings are token-heavy); the nomic fallback path (402 quota flip,
unchanged — this spec wraps the Jina path only).

### Function Signatures

```python
from collections.abc import Iterator, Sequence


class TokenBucket:
    """Async token bucket pacing outbound tokens to a fixed per-minute rate."""

    def __init__(
        self,
        capacity: int,
        refill_per_sec: float,
        *,
        initial_tokens: float | None = None,
    ) -> None:
        """capacity = max burst; refill_per_sec = sustained rate (tokens/sec).
        initial_tokens: starting fill. None -> capacity (full, backward-compatible).
        Pass 0 for a rate-limiter that grants no initial burst."""

    @classmethod
    def from_tpm(cls, tpm: int, *, initial_tokens: float | None = None) -> "TokenBucket":
        """Bucket whose burst and sustained rate both equal `tpm` per minute."""

    async def acquire(self, tokens: int) -> None:
        """Lazily refill, then block via asyncio.sleep until `tokens` are available
        and deduct them. Raises ValueError if tokens > capacity."""

    @property
    def available(self) -> float:
        """Current token count after applying any pending refill. For tests/obs."""


def get_jina_bucket() -> TokenBucket:
    """Process-wide lazy singleton: from_tpm(settings.jina_tpm_limit, initial_tokens=0).
    Starts EMPTY — no initial burst."""


def token_aware_batches(
    texts: list[str],
    token_counts: Sequence[int],
    max_tokens: int,
) -> Iterator[tuple[list[str], list[int]]]:
    """Yield (batch_texts, batch_counts) where each batch's summed token_count
    <= max_tokens. A single item whose count exceeds max_tokens is emitted alone
    (never split here, never infinite-loops)."""


async def jina_embed(
    texts: list[str],
    token_counts: Sequence[int] | None = None,
    *,
    task: str = "retrieval.passage",
    bucket: TokenBucket | None = None,
) -> list[list[float]]:
    """Embed via Jina v3 (truncate_dim=768), pacing the request through the token
    bucket before the HTTP call. task: "retrieval.passage" for documents,
    "retrieval.query" for queries. token_counts defaults to the project's existing
    token counter; bucket defaults to get_jina_bucket()."""
```

`EmbeddingClient._embed` groups chunks via `token_aware_batches(...,
settings.jina_max_request_tokens)` instead of fixed-count slicing, and calls the
paced Jina path once per token-bounded batch.

### Acceptance Criteria

1. `acquire(n)` with `n <= available` returns without sleeping and decrements `available` by `n`.
2. After the bucket is drained, `acquire(n)` sleeps ~ `n / refill_per_sec` seconds (+/-1 refill tick) — verified with **mocked time**.
3. Refill is time-based, lazy, monotonic-clock, capped at `capacity`. No background task.
4. `acquire(n)` with `n > capacity` raises `ValueError`.
5. `from_tpm(90000)` -> `capacity == 90000`, `refill_per_sec == 1500.0`.
6. The paced Jina path calls `await bucket.acquire(sum(batch_token_counts))` **exactly once** per request, before the HTTP call.
7. When `token_counts is None`, counts derive from the project's existing token-count utility (same one the chunker uses).
8. **Concurrency-safe:** `acquire` is guarded by an `asyncio.Lock` held across the sleep; two overlapping acquires whose sum exceeds the burst observe the second one paced.
9. `settings.jina_tpm_limit: int` exists (default `90_000`), single source of truth; `get_jina_bucket()` reads it.
10. Gates green: `ruff`, `mypy --strict`, `pytest`. `tests/unit/test_token_bucket.py` covers AC 1-5, 8 with mocked time.
11. The nomic fallback path and the 402->fallback flip are unchanged.
12. `jina_embed` forwards `task` verbatim to the Jina request body; default `"retrieval.passage"`; the query path calls with `"retrieval.query"`.
13. **Token-aware batching:** `settings.jina_max_request_tokens: int` exists (default `6_000`). `_embed` emits Jina requests whose summed `token_count` <= `jina_max_request_tokens`. A test asserts no emitted Jina request body exceeds the cap (using inputs that previously produced ~50K-token requests).
14. **Empty-start Jina bucket:** `get_jina_bucket()` returns a bucket with `available == 0` immediately after creation; its first `acquire(n>0)` paces (sleeps). The class default (`initial_tokens=None`) still starts full — existing AC#1 test fills the bucket explicitly.
15. **Safety invariant guard:** a test (or startup assertion) verifies `jina_tpm_limit + jina_max_request_tokens <= 100_000`.

### Gotchas (optional)

- **Jina counts a rolling 60-second SUM, not an average.** A token bucket only bounds the average rate; a large single request still lands as one lump at request time. That is why both controls are mandatory — small per-request token cap (bounds the lump) **and** empty-start bucket (no initial burst). `capacity == tpm` means a *full* bucket could dump a whole minute's budget at once, so the Jina singleton must start empty.
- **Hold the lock across the sleep.** Critical section = refill + (optional sleep) + deduct, all under the `asyncio.Lock`. Releasing before sleep lets a second coroutine refill-and-deduct against stale state -> cap overshoot.
- **Monotonic clock only** (`time.monotonic()` / loop time). Wall-clock jumps corrupt refill math.
- **Token counts are estimates;** Jina's server tokenizer differs slightly. The invariant's headroom (96K vs 100K) absorbs the drift — do not chase exact parity.
- **A 116K-token filing still cannot fit in one minute, and shouldn't** — it paces to ~77s and succeeds, instead of failing. Intended behavior.
- **`token_aware_batches` must emit an over-cap lone item as its own batch** (defensive — never split, never loop forever). In practice chunks are <=512 tokens so this never triggers, but the batcher must not assume it.
