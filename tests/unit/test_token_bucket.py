"""Unit tests for src/alphalens/etl/rate_limit.py (Spec 06a).

Coverage: AC#1–5, AC#8.
Offline + deterministic: time.monotonic and asyncio.sleep are monkeypatched so
tests run at full speed with no real sleeping.
asyncio_mode=auto: async tests run without @pytest.mark.asyncio.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from alphalens.etl.rate_limit import TokenBucket

# ---------------------------------------------------------------------------
# Shared fixture — fake monotonic clock + instant sleep
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace time.monotonic with a controllable counter and asyncio.sleep with
    an instant advance of that counter. Returns the mutable [current_time] list
    so tests can read and set it directly."""
    t: list[float] = [0.0]

    monkeypatch.setattr(time, "monotonic", lambda: t[0])

    async def _instant_sleep(seconds: float) -> None:
        t[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    return t


# ---------------------------------------------------------------------------
# AC#5 — from_tpm factory
# ---------------------------------------------------------------------------


def test_ac5_from_tpm() -> None:
    """AC#5: from_tpm(90000) yields capacity=90000 and refill_per_sec=1500.0."""
    bucket = TokenBucket.from_tpm(90_000)
    assert bucket._capacity == 90_000
    assert bucket._refill_per_sec == pytest.approx(1500.0)


# ---------------------------------------------------------------------------
# AC#4 — ValueError on tokens > capacity
# ---------------------------------------------------------------------------


async def test_ac4_value_error_exceeds_capacity(fake_clock: list[float]) -> None:
    """AC#4: acquire(n > capacity) raises ValueError immediately."""
    bucket = TokenBucket(capacity=100, refill_per_sec=10.0)
    with pytest.raises(ValueError, match="capacity"):
        await bucket.acquire(101)


def test_ac4_init_guards() -> None:
    """TokenBucket.__init__ rejects capacity=0 and refill_per_sec=0."""
    with pytest.raises(ValueError, match="capacity"):
        TokenBucket(capacity=0, refill_per_sec=1.0)
    with pytest.raises(ValueError, match="refill_per_sec"):
        TokenBucket(capacity=100, refill_per_sec=0.0)


# ---------------------------------------------------------------------------
# AC#1 — acquire within available balance: no sleep
# ---------------------------------------------------------------------------


async def test_ac1_acquire_within_balance_no_sleep(fake_clock: list[float]) -> None:
    """AC#1: acquire(n ≤ available) returns immediately; available decrements by n."""
    bucket = TokenBucket(capacity=1000, refill_per_sec=100.0)
    time_before = fake_clock[0]

    await bucket.acquire(400)

    assert fake_clock[0] == time_before, "asyncio.sleep must not be called when tokens available"
    # available reflects the deduction (no time has elapsed, so refill = 0)
    assert bucket.available == pytest.approx(600.0)


# ---------------------------------------------------------------------------
# AC#2 — acquire beyond available: sleep duration matches deficit / refill_rate
# ---------------------------------------------------------------------------


async def test_ac2_acquire_beyond_available_sleeps(fake_clock: list[float]) -> None:
    """AC#2: after full drain, acquire(n) sleeps ≈ n / refill_per_sec seconds."""
    bucket = TokenBucket(capacity=1000, refill_per_sec=500.0)

    # Drain the bucket completely first
    await bucket.acquire(1000)
    assert fake_clock[0] == pytest.approx(0.0), "full bucket should not sleep"

    # Now request 500 more tokens (deficit = 500, rate = 500/s → expect ~1.0 s sleep)
    await bucket.acquire(500)

    assert fake_clock[0] == pytest.approx(
        1.0, abs=0.01
    ), f"Expected ~1.0 s of mocked sleep, got {fake_clock[0]}"


# ---------------------------------------------------------------------------
# AC#3 — refill caps at capacity
# ---------------------------------------------------------------------------


async def test_ac3_refill_capped_at_capacity(fake_clock: list[float]) -> None:
    """AC#3: refill never exceeds capacity, even after a very long idle period."""
    bucket = TokenBucket(capacity=500, refill_per_sec=100.0)

    # Drain entirely
    await bucket.acquire(500)

    # Advance time far beyond what would be needed to overflow
    fake_clock[0] += 1000.0  # would add 100_000 tokens if uncapped

    # acquire 1 token to trigger refill logic
    await bucket.acquire(1)

    # After refill: capped at 500, then 1 deducted → 499
    assert bucket.available == pytest.approx(499.0, abs=1.0)


# ---------------------------------------------------------------------------
# AC#8 — concurrency: second acquire is paced when sum exceeds burst
# ---------------------------------------------------------------------------


async def test_ac8_concurrent_second_is_paced(fake_clock: list[float]) -> None:
    """AC#8: two concurrent acquires whose sum > burst → second is paced.

    Bucket: capacity=1000, refill_per_sec=500.
    - First acquire: 600 tokens → immediate (tokens available).
    - Second acquire: 600 tokens → deficit 200 after first deducts 600,
      expects sleep of 200/500 = 0.4 s.
    Because asyncio.Lock is held across the sleep, the second coroutine queues
    behind the first; total mocked elapsed ≥ 0.4 s.
    """
    bucket = TokenBucket(capacity=1000, refill_per_sec=500.0)
    elapsed_records: list[float] = []

    async def _timed_acquire(n: int) -> None:
        t_start = fake_clock[0]
        await bucket.acquire(n)
        elapsed_records.append(fake_clock[0] - t_start)

    await asyncio.gather(_timed_acquire(600), _timed_acquire(600))

    # One of the two coroutines must have slept (whichever queued behind the lock)
    assert (
        max(elapsed_records) >= 0.35
    ), f"Expected one acquire to sleep ≥ 0.4 s (±0.05), got elapsed={elapsed_records}"
    # Total mocked time advanced by the sleep
    assert fake_clock[0] >= 0.35
