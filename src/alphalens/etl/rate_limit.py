"""Async token-bucket rate limiter for Jina TPM pacing (Spec 06a)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator, Sequence

_JINA_BUCKET: TokenBucket | None = None


class TokenBucket:
    """Async token bucket pacing outbound tokens to a fixed per-minute rate."""

    def __init__(
        self,
        capacity: int,
        refill_per_sec: float,
        *,
        initial_tokens: float | None = None,
    ) -> None:
        """capacity = max burst (== TPM cap); refill_per_sec = capacity / 60.
        initial_tokens: starting fill. None → capacity (full, backward-compatible).
        Pass 0 for a rate-limiter that grants no initial burst."""
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        if refill_per_sec <= 0:
            raise ValueError(f"refill_per_sec must be > 0, got {refill_per_sec}")
        self._capacity = capacity
        self._tokens: float = float(capacity) if initial_tokens is None else initial_tokens
        self._refill_per_sec = refill_per_sec
        self._last_refill: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    def from_tpm(cls, tpm: int, *, initial_tokens: float | None = None) -> TokenBucket:
        """Build a bucket whose burst and sustained rate both equal `tpm` per minute."""
        return cls(capacity=tpm, refill_per_sec=tpm / 60.0, initial_tokens=initial_tokens)

    @property
    def available(self) -> float:
        """Current token count after applying any pending refill. For tests/observability."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        return min(float(self._capacity), self._tokens + elapsed * self._refill_per_sec)

    def _refill(self) -> None:
        """Lazily refill from elapsed monotonic time. Must be called under lock."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self._capacity), self._tokens + elapsed * self._refill_per_sec)
        self._last_refill = now

    async def acquire(self, tokens: int) -> None:
        """Lazily refill then block until `tokens` are available and deduct them.

        Raises ValueError if tokens > capacity — a single request must fit under
        the cap; batching is the caller's responsibility.
        The asyncio.Lock is held across any sleep so the critical section
        (refill + optional sleep + deduct) is never split between coroutines.
        """
        if tokens > self._capacity:
            raise ValueError(
                f"Requested {tokens} tokens exceeds bucket capacity {self._capacity}; "
                "reduce batch size"
            )
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                await asyncio.sleep(deficit / self._refill_per_sec)


def get_jina_bucket() -> TokenBucket:
    """Return the process-wide lazy singleton built from settings.jina_tpm_limit.
    Starts EMPTY (initial_tokens=0) — no initial burst granted."""
    global _JINA_BUCKET
    if _JINA_BUCKET is None:
        from alphalens.config import get_settings

        _JINA_BUCKET = TokenBucket.from_tpm(get_settings().jina_tpm_limit, initial_tokens=0)
    return _JINA_BUCKET


def token_aware_batches(
    texts: list[str],
    token_counts: Sequence[int],
    max_tokens: int,
) -> Iterator[tuple[list[str], list[int]]]:
    """Yield (batch_texts, batch_counts) where each batch's summed token_count
    <= max_tokens. A single item whose count exceeds max_tokens is emitted alone
    (never split, never infinite-loops)."""
    batch_texts: list[str] = []
    batch_counts: list[int] = []
    batch_sum = 0
    for text, count in zip(texts, token_counts, strict=True):
        if batch_texts and batch_sum + count > max_tokens:
            yield batch_texts, batch_counts
            batch_texts, batch_counts, batch_sum = [], [], 0
        batch_texts.append(text)
        batch_counts.append(count)
        batch_sum += count
    if batch_texts:
        yield batch_texts, batch_counts
