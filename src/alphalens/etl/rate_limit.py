"""Async token-bucket rate limiter for Jina TPM pacing (Spec 06a)."""

from __future__ import annotations

import asyncio
import time

_JINA_BUCKET: TokenBucket | None = None


class TokenBucket:
    """Async token bucket pacing outbound tokens to a fixed per-minute rate."""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        """capacity = max burst (== TPM cap); refill_per_sec = capacity / 60."""
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        if refill_per_sec <= 0:
            raise ValueError(f"refill_per_sec must be > 0, got {refill_per_sec}")
        self._capacity = capacity
        self._tokens: float = float(capacity)
        self._refill_per_sec = refill_per_sec
        self._last_refill: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    def from_tpm(cls, tpm: int) -> TokenBucket:
        """Build a bucket whose burst and sustained rate both equal `tpm` per minute."""
        return cls(capacity=tpm, refill_per_sec=tpm / 60.0)

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
    """Return the process-wide lazy singleton built from settings.jina_tpm_limit."""
    global _JINA_BUCKET
    if _JINA_BUCKET is None:
        from alphalens.config import get_settings

        _JINA_BUCKET = TokenBucket.from_tpm(get_settings().jina_tpm_limit)
    return _JINA_BUCKET
