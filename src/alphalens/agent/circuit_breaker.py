"""AlphaLens v8 -- Synthesis circuit breaker (S14).

Guards the single most failure-prone step in the agent -- the Groq synthesis stream in
``synthesize_node`` -- so a Groq outage degrades the system honestly and fast instead of
making every request wait out a full timeout before erroring.

The breaker wraps the S13 ``stream_synthesis`` seam. In CLOSED state the real LLM stream
flows through untouched. Once Groq trips it (OPEN), requests skip the LLM entirely and
receive a degraded response -- the top reranked SEC chunks, no synthesis (built in
``nodes.degraded_stream``, handed in as ``fallback``). After a cool-off (HALF_OPEN) a
single probe tests recovery.

Decisions (locked this session):
  D1 -- only hard failures count: 5xx / timeout / connection advance the breaker; 429
        (transient throttle) and non-429 4xx (our own bug) are excluded.
  D2 -- consecutive counting, in-memory PER-CONTAINER: a single int streak, +1 per hard
        failure, reset to 0 on any success. Threshold = 3 (env BREAKER_FAILURE_THRESHOLD).
  D3 -- reset timeout = 30s (env BREAKER_RESET_TIMEOUT_SECONDS), on a MONOTONIC clock.
  D4 -- single probe on HALF_OPEN: one success -> CLOSED; probe hard-failure -> OPEN with
        the reset timer restarted from zero (next probe a full timeout after the failure).

The breaker is domain-agnostic: it only ever sees ``Callable[[], AsyncGenerator[str, None]]``,
never ``AgentState``. All state/chunk coupling stays in ``nodes.py``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Callable
from enum import StrEnum


# ── Failure taxonomy (D1) ────────────────────────────────────────────────────
# Only "hard" failures advance the breaker. Classified from the exception the
# protected synthesis call raises.
#
#   timeout / connection error        -> True   (hard: Groq unreachable)
#   status 5xx  (500..599)            -> True   (hard: Groq server error)
#   status 429  (rate limit)          -> False  (excluded: transient throttle)
#   status 4xx  (400..499, not 429)   -> False  (excluded: our bug, surface it)
#   anything unrecognized             -> False  (fail safe: never trip on surprises)
#
# LIVE-VERIFIED (S14): langchain-groq's ``_astream`` iterates the native ``groq`` async
# client with no try/except, so native ``groq.*`` exceptions propagate untouched. groq maps
# every >=500 to InternalServerError and 429 to RateLimitError, both subclasses of
# APIStatusError (which carries ``.status_code`` / ``.response``); APITimeoutError subclasses
# APIConnectionError. The status-code fallback below guards against SDK type drift.
def is_hard_failure(exc: Exception) -> bool:
    """True only for 5xx / timeout / connection errors (D1)."""
    # groq SDK types (preferred, precise).
    try:
        import groq

        if isinstance(exc, groq.APITimeoutError | groq.APIConnectionError):
            return True
        if isinstance(exc, groq.RateLimitError):  # 429 -> excluded
            return False
        if isinstance(exc, groq.InternalServerError):  # 5xx -> hard
            return True
    except ImportError:  # pragma: no cover  (groq is always in the Lambda image)
        pass

    # Builtin fallbacks (in case a bare timeout/conn error slips through).
    if isinstance(exc, TimeoutError | ConnectionError):
        return True

    # Status-code fallback: read a status off the exc or its .response.
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if isinstance(status, int):
        if status == 429:
            return False
        if 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False

    return False  # unrecognized -> do not count


# ── State machine ────────────────────────────────────────────────────────────
class BreakerState(StrEnum):
    CLOSED = "closed"  # healthy: LLM synthesis flows
    OPEN = "open"  # tripped: serve degraded (reranked chunks, no LLM)
    HALF_OPEN = "half_open"  # reset elapsed: allow exactly ONE probe


class SynthesisCircuitBreaker:
    """Consecutive-failure breaker guarding the Groq synthesis stream.

    State is in-memory and PER-CONTAINER (D2): one instance is built at cold-start and
    injected via AgentContext, shared by every invocation on that warm container. No shared
    datastore. asyncio is single-threaded, so the integer counter needs no lock: the only
    interleaving window is around ``await protected()``, and at portfolio scale two
    concurrent HALF_OPEN probes is practically impossible -- do NOT add a lock now.
    """

    def __init__(self, *, failure_threshold: int, reset_timeout_seconds: float) -> None:
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout_seconds
        self._streak = 0  # consecutive hard failures
        self._opened_at: float | None = None  # monotonic ts when tripped; None => CLOSED

    @property
    def state(self) -> BreakerState:
        """Derived from the timer -- no background task needed."""
        if self._opened_at is None:
            return BreakerState.CLOSED
        if time.monotonic() - self._opened_at >= self._reset_timeout:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    async def stream(
        self,
        protected: Callable[[], AsyncGenerator[str, None]],  # real LLM synthesis (deferred thunk)
        fallback: Callable[[], AsyncGenerator[str, None]],  # degraded: reranked chunks, no LLM
    ) -> AsyncGenerator[str, None]:
        """Gate, then drain.

        The state snapshot is taken lazily on the first ``__anext__`` (drain time), so it
        reflects the freshest state at the moment streaming actually begins -- not when
        ``synthesize_node`` returned. Don't "fix" this by checking state eagerly in the node.
        """
        if self.state is BreakerState.OPEN:
            async for token in fallback():  # OPEN: skip the LLM entirely
                yield token
            return

        # CLOSED or HALF_OPEN. If HALF_OPEN, this call IS the single probe (D4).
        yielded = False
        try:
            async for token in protected():
                yielded = True
                yield token
        except Exception as exc:  # NOT BaseException -- CancelledError must propagate untouched
            if is_hard_failure(exc):
                self._record_hard_failure()  # streak++ / (re)open (D2, D4)
                if not yielded:
                    # Failed before the first token -> serve degraded for THIS request too
                    # (clean fallback). Common case: Groq down at connect.
                    async for token in fallback():
                        yield token
                    return
            raise  # non-hard (429/4xx) OR mid-stream hard failure -> propagate untouched
        else:
            self._record_success()  # streak=0, close (D4)

    def _record_hard_failure(self) -> None:
        self._streak += 1
        if self._streak >= self._threshold:
            self._opened_at = time.monotonic()  # (re)start the reset timer from zero (D4)

    def _record_success(self) -> None:
        self._streak = 0
        self._opened_at = None  # -> CLOSED
