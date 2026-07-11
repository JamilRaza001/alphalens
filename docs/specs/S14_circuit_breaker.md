# Spec S14 — Circuit Breaker (`circuit_breaker`)

> Spec **S14** (circuit_breaker) · v8 cross-ref: spec 11 · target: `src/alphalens/agent/circuit_breaker.py`
> Format: L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).
> Wraps the `stream_synthesis` seam built in S13. Also **piggybacks** on `nodes.py` (`AgentContext`
> gains a `breaker` dep; `synthesize_node` routes synthesis through it) and `config.py` (two `Settings`
> fields). The Groq **failure taxonomy** (which exception classes = a hard failure) is a
> **LIVE-VERIFY** point — confirm against the installed `groq` / `langchain-groq` in Claude Code.

**Decisions applied (locked this session):**
1. **D1 — only hard failures count.** `5xx`, timeout, and connection errors advance the breaker. **`429` is excluded** (transient throttle — normal on the free tier — a backoff concern, not an outage signal). **`4xx` is excluded** (our own bug; it must surface loudly, not hide behind a degraded response).
2. **D2 — consecutive counting, in-memory per-container.** A single `int` streak: `+1` on each hard failure, reset to `0` on any success. State lives **in-memory, per-container**, held as a dep on `AgentContext` (built once at cold-start, injected by reference at S16 — the same pattern as `llm` / `pool`). **No shared datastore.** Threshold = **3**, env-overridable via `BREAKER_FAILURE_THRESHOLD`.
3. **D3 — reset timeout = 30s**, env-overridable via `BREAKER_RESET_TIMEOUT_SECONDS`. Measured on a **monotonic** clock.
4. **D4 — single probe on HALF-OPEN.** After the reset timeout elapses, exactly **one** request becomes the probe (a real LLM call). One success → **CLOSED**. Probe hard-failure → **OPEN** with the reset timer **restarted from zero** (next probe is a full timeout later, measured from the probe failure — not the original trip).

---

### Goal

Guard the single most failure-prone step in the agent — the Groq synthesis call in `synthesize_node` — so that a Groq outage degrades the system *honestly and fast* instead of making every request wait out a full timeout before erroring.

The breaker wraps the S13 `stream_synthesis` seam. In **CLOSED** state the real LLM stream flows through untouched. Once Groq trips the breaker (**OPEN**), requests skip the LLM entirely and receive a **degraded response** — the top reranked SEC chunks streamed back verbatim, no synthesis. After a cool-off (**HALF-OPEN**) a single probe tests recovery.

This spec defines `circuit_breaker.py`: the `SynthesisCircuitBreaker` class (state machine + consecutive counter), the `BreakerState` enum, and the `is_hard_failure` classifier (D1). It also specifies the two piggyback edits (`AgentContext` dep + `synthesize_node` wiring in `nodes.py`; two `Settings` fields in `config.py`). The breaker is **domain-agnostic** — it only sees `Callable[[], AsyncGenerator[str, None]]`, never `AgentState` — so the concrete "real vs degraded" streams are built in `nodes.py` and handed in. **No retrieval or graph wiring here** — that is S15 / S16.

---

### Function Signatures

```python
from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Callable
from enum import Enum


# ── Failure taxonomy (D1) ───────────────────────────────────────────────────
# Only "hard" failures advance the breaker. Classified from the exception the
# protected synthesis call raises.
#
# LIVE-VERIFY: confirm the exact classes langchain-groq surfaces for each case
# against the installed `groq` SDK before finalizing. The status-code fallback
# below guards against SDK type drift, but verify the happy path.
#
#   timeout / connection error        -> True   (hard: Groq unreachable)
#   status 5xx  (500..599)            -> True   (hard: Groq server error)
#   status 429  (rate limit)          -> False  (excluded: transient throttle)
#   status 4xx  (400..499, not 429)   -> False  (excluded: our bug, surface it)
#   anything unrecognized             -> False  (fail safe: never trip on surprises)
def is_hard_failure(exc: Exception) -> bool:
    """True only for 5xx / timeout / connection errors (D1)."""
    # groq SDK types (preferred, precise). LIVE-VERIFY these names/imports exist.
    try:
        import groq
        if isinstance(exc, (groq.APITimeoutError, groq.APIConnectionError)):
            return True
        if isinstance(exc, groq.RateLimitError):        # 429 -> excluded
            return False
        if isinstance(exc, groq.InternalServerError):   # 5xx -> hard
            return True
    except ImportError:  # pragma: no cover  (groq is always in the Lambda image)
        pass

    # Builtin fallbacks (in case a bare timeout/conn error slips through).
    if isinstance(exc, (TimeoutError, ConnectionError)):
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


# ── State machine ───────────────────────────────────────────────────────────
class BreakerState(str, Enum):
    CLOSED = "closed"        # healthy: LLM synthesis flows
    OPEN = "open"            # tripped: serve degraded (reranked chunks, no LLM)
    HALF_OPEN = "half_open"  # reset elapsed: allow exactly ONE probe


class SynthesisCircuitBreaker:
    """Consecutive-failure breaker guarding the Groq synthesis stream.

    State is in-memory and PER-CONTAINER (D2): one instance is built at
    cold-start and injected via AgentContext, shared by every invocation on
    that warm container. No shared datastore. asyncio is single-threaded, so
    the integer counter needs no lock (see Gotchas).
    """

    def __init__(self, *, failure_threshold: int, reset_timeout_seconds: float) -> None:
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout_seconds
        self._streak = 0                       # consecutive hard failures
        self._opened_at: float | None = None   # monotonic ts when tripped; None => CLOSED

    @property
    def state(self) -> BreakerState:
        """Derived from the timer — no background task needed."""
        if self._opened_at is None:
            return BreakerState.CLOSED
        if time.monotonic() - self._opened_at >= self._reset_timeout:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    async def stream(
        self,
        protected: Callable[[], AsyncGenerator[str, None]],  # real LLM synthesis (deferred thunk)
        fallback: Callable[[], AsyncGenerator[str, None]],   # degraded: reranked chunks, no LLM
    ) -> AsyncGenerator[str, None]:
        """Gate, then drain. The state snapshot is taken lazily on the first
        __anext__ (drain time), so it reflects the freshest state at the moment
        streaming actually begins — not when synthesize_node returned."""
        if self.state is BreakerState.OPEN:
            async for token in fallback():          # OPEN: skip the LLM entirely
                yield token
            return

        # CLOSED or HALF_OPEN. If HALF_OPEN, this call IS the single probe (D4).
        yielded = False
        try:
            async for token in protected():
                yielded = True
                yield token
        except Exception as exc:                    # NOT BaseException — see Gotchas (CancelledError)
            if is_hard_failure(exc):
                self._record_hard_failure()         # streak++ / (re)open (D2, D4)
                if not yielded:
                    # Failed before the first token -> serve degraded for THIS
                    # request too (clean fallback). Common case: Groq down at connect.
                    async for token in fallback():
                        yield token
                    return
            raise  # non-hard (429/4xx) OR mid-stream hard failure -> propagate untouched
        else:
            self._record_success()                  # streak=0, close (D4)

    def _record_hard_failure(self) -> None:
        self._streak += 1
        if self._streak >= self._threshold:
            self._opened_at = time.monotonic()      # (re)start the reset timer from zero (D4)

    def _record_success(self) -> None:
        self._streak = 0
        self._opened_at = None                       # -> CLOSED
```

**Piggyback — `src/alphalens/agent/nodes.py`:**

```python
from alphalens.agent.circuit_breaker import SynthesisCircuitBreaker

@dataclass
class AgentContext:
    llm: ChatGroq
    reranker: CrossEncoder
    pool: Pool
    allowed_tickers: frozenset[str]
    breaker: SynthesisCircuitBreaker            # NEW (S14): built once at cold-start


async def synthesize_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    ctx = runtime.context

    # Real LLM stream (the S13 seam) — LIVE-VERIFY stream_synthesis's exact signature.
    def protected() -> AsyncGenerator[str, None]:
        return stream_synthesis(state, runtime)

    # Degraded stream: top reranked SEC chunks, deterministic, NO LLM (honesty rail).
    def fallback() -> AsyncGenerator[str, None]:
        return degraded_stream(state["reranked_chunks"])

    answer_stream = ctx.breaker.stream(protected, fallback)
    return {"answer_stream": answer_stream}         # + citations/status as already built in S13


async def degraded_stream(chunks: list[ScoredChunk]) -> AsyncGenerator[str, None]:
    """No-LLM fallback: stream the top reranked chunk texts with source tags.
    Lives HERE (not in circuit_breaker.py) so the breaker stays AgentState-agnostic."""
    ...
```

**Piggyback — `src/alphalens/config.py`:**

```python
class Settings(BaseSettings):
    ...
    breaker_failure_threshold: int = 3            # env: BREAKER_FAILURE_THRESHOLD (D2)
    breaker_reset_timeout_seconds: float = 30.0   # env: BREAKER_RESET_TIMEOUT_SECONDS (D3)
```

---

### Acceptance Criteria

1. `BreakerState` is a `str`-`Enum` with `CLOSED` / `OPEN` / `HALF_OPEN`. `SynthesisCircuitBreaker(failure_threshold=..., reset_timeout_seconds=...)` and every method type-check under **mypy strict**.
2. **Fresh breaker:** `state == CLOSED`; `stream(protected, fallback)` drains `protected` (LLM path) and never calls `fallback`; a clean completion sets the streak to `0`.
3. **Consecutive counting (D2):** exactly `threshold` consecutive hard failures flip `state` to `OPEN`; a single success interleaved anywhere in the run resets the streak to `0`, so `threshold` *scattered* failures do **not** trip.
4. **Only hard failures count (D1):** a `429` or non-429 `4xx` raised by `protected` does **not** increment the streak and is **re-raised untouched** (not masked by `fallback`). A `5xx` / timeout / connection error **does** increment it.
5. **OPEN serves degraded only:** while `state == OPEN`, `stream` drains `fallback` and **never invokes `protected`** (zero LLM calls).
6. **HALF-OPEN single probe (D3 + D4):** once `reset_timeout_seconds` have elapsed since tripping, `state` reports `HALF_OPEN`, and the next `stream` call invokes `protected` **exactly once**.
7. **Probe success (D4):** a successful probe returns `state` to `CLOSED` with streak `== 0`.
8. **Probe failure (D4):** a hard-failing probe returns `state` to `OPEN` and **restarts the reset timer from zero** — timed from the probe failure, not the original trip (the next probe is a full `reset_timeout` later).
9. **Streaming failure semantics:** a hard failure **before the first yielded token** makes `stream` transparently serve `fallback` for that same request (and records the failure); a hard failure **after ≥1 token** propagates the exception (partial answer already delivered) and does **not** re-emit `fallback`.
10. **Wiring:** `AgentContext` carries `breaker: SynthesisCircuitBreaker`; `synthesize_node` routes the S13 `stream_synthesis` seam through `breaker.stream(...)`; `degraded_stream` lives in `nodes.py`; `Settings` exposes `breaker_failure_threshold` (default `3`) and `breaker_reset_timeout_seconds` (default `30.0`), both env-overridable.
11. **Unit-testable with no live deps:** every criterion above is provable with fake async generators (one that yields, one that raises a fake hard/soft error) and a fake clock — no Groq, DB, or graph. The timer is injectable/mockable (monotonic, see Gotchas).

---

### Gotchas

- **Monotonic clock, not wall-clock.** The reset timer uses `time.monotonic()`, never `time.time()`. Wall-clock is subject to NTP corrections and system clock changes, which would make the reset window jump. Monotonic is immune. Tests fake it by monkeypatching the module's `time.monotonic` reference.
- **Catch `Exception`, not `BaseException`.** `asyncio.CancelledError` subclasses `BaseException` (Py 3.8+). A client disconnect or an outer timeout cancels the drain via `CancelledError` — that is **not** a Groq failure and must propagate untouched. Catching `Exception` excludes it (and `KeyboardInterrupt` / `SystemExit`) automatically.
- **No lock — and that is deliberate (D4 gotcha).** asyncio is single-threaded; `self._streak += 1` and the `_opened_at` write have no `await` between read and write, so they are atomic. The only interleaving window is around `await protected()` — where, in theory, two concurrent HALF_OPEN requests could both act as "the probe." At low-traffic, sequential portfolio scale this race is practically impossible. **Do NOT add a lock/atomic-flag now**; revisit only if concurrency rises. (Right-size the solution — curated corpus, portfolio scale.)
- **The breaker is AgentState-agnostic by design.** It only sees `Callable[[], AsyncGenerator[str, None]]`. All `AgentState` / `ScoredChunk` coupling stays in `nodes.py` (`degraded_stream` lives there). This keeps `circuit_breaker.py` trivially unit-testable and lets the breaker be reused for any future guarded stream.
- **Lazy gate evaluation.** `stream` is an async generator, so its body — including the `self.state` snapshot — does not run until the first `__anext__` at drain time. This is correct: we want the freshest state when streaming *actually* begins, not when `synthesize_node` returned its dict. Don't "fix" this by checking state eagerly in the node.
- **Mid-stream failure is not cleanly recoverable.** Once tokens are emitted downstream, a hard failure can't be swapped for a degraded answer — the partial output is already gone to the client. The breaker still **records** it (protecting future requests) and re-raises; the honesty rail (Synthesize) should surface the truncation to the user. Buffering the whole stream to make it "atomic" is **rejected** — it defeats streaming, the entire point of the seam.
- **Failure taxonomy is a LIVE-VERIFY point.** Confirm the exact exception classes `langchain-groq` surfaces for 5xx / timeout / connection / 429 against the installed `groq` SDK before finalizing `is_hard_failure`. The status-code fallback is a safety net against SDK type drift, not a substitute for verifying the primary path.
- **Degraded ≠ error.** OPEN-state responses are *real answers* — the top reranked SEC chunks — just LLM-free. Mark them `status='degraded'` (not `'error'`) in the `queries` table; the breaker exposes `state` read-only for that tagging. Never fabricate synthesis in degraded mode; surface the chunks honestly (honesty rail).
- **`ScoredChunk` / `stream_synthesis` names.** Both come from S13; confirm the import paths and `stream_synthesis`'s exact signature in Claude Code before wiring (the seam was frozen in S13, but verify rather than assume).
