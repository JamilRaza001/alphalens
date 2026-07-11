"""Unit tests for src/alphalens/agent/circuit_breaker.py (S14).

Fakes ONLY -- no live Groq / DB / graph. The protected + fallback streams are fake async
generators (one that yields, one that raises a fake hard error, one that raises a soft
429/4xx error), and ``time.monotonic`` is monkeypatched to a deterministic fake clock.

Each of the 11 spec acceptance criteria maps to a named test below.
asyncio_mode=auto: async tests run without @pytest.mark.asyncio.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from asyncpg import Pool
from langchain_groq import ChatGroq
from langgraph.runtime import Runtime
from sentence_transformers import CrossEncoder

from alphalens.agent import circuit_breaker as cb
from alphalens.agent.circuit_breaker import (
    BreakerState,
    SynthesisCircuitBreaker,
    is_hard_failure,
)
from alphalens.agent.nodes import AgentContext, degraded_stream, synthesize_node
from alphalens.agent.state import AgentState, RetrievedChunk, ScoredChunk

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _Clock:
    """Deterministic monotonic-clock stand-in; advance with ``.tick(seconds)``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class _HTTPError(Exception):
    """Stand-in for a groq APIStatusError: carries a status_code (429/4xx/5xx)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _Factory:
    """A ``Callable[[], AsyncGenerator[str, None]]`` that counts how often it is invoked.

    Yields ``tokens`` in order; if ``raises`` is set, raises it after ``raise_after`` tokens
    (``raise_after=0`` -> before the first token).
    """

    def __init__(
        self,
        tokens: tuple[str, ...] = (),
        *,
        raises: BaseException | None = None,
        raise_after: int = 0,
    ) -> None:
        self._tokens = tokens
        self._raises = raises
        self._raise_after = raise_after
        self.calls = 0

    def __call__(self) -> AsyncGenerator[str, None]:
        self.calls += 1
        return self._gen()

    async def _gen(self) -> AsyncGenerator[str, None]:
        for i, tok in enumerate(self._tokens):
            if self._raises is not None and i == self._raise_after:
                raise self._raises
            yield tok
        if self._raises is not None and self._raise_after >= len(self._tokens):
            raise self._raises


async def _drain(agen: AsyncGenerator[str, None]) -> list[str]:
    return [tok async for tok in agen]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Freeze the breaker's monotonic clock so timing is deterministic."""
    fake = _Clock()
    monkeypatch.setattr(cb.time, "monotonic", fake)
    return fake


def _breaker(threshold: int = 3, reset: float = 30.0) -> SynthesisCircuitBreaker:
    return SynthesisCircuitBreaker(failure_threshold=threshold, reset_timeout_seconds=reset)


# Convenience factories.
def _hard() -> _Factory:
    """Pre-first-token hard failure (timeout) -> breaker serves fallback for this request."""
    return _Factory(raises=TimeoutError())


def _ok(*tokens: str) -> _Factory:
    return _Factory(tokens or ("ok",))


def _fb() -> _Factory:
    return _Factory(("FALLBACK",))


# ── AC#1: types / enum ─────────────────────────────────────────────────────────


def test_breaker_state_is_str_enum() -> None:
    assert issubclass(BreakerState, str)
    assert {s.value for s in BreakerState} == {"closed", "open", "half_open"}
    assert (BreakerState.CLOSED, BreakerState.OPEN, BreakerState.HALF_OPEN) == (
        BreakerState.CLOSED,
        BreakerState.OPEN,
        BreakerState.HALF_OPEN,
    )


# ── AC#2: fresh breaker drains protected (LLM path), never fallback ────────────


async def test_fresh_breaker_drains_protected_only(clock: _Clock) -> None:
    b = _breaker()
    assert b.state is BreakerState.CLOSED

    protected = _ok("Hello", " world")
    fallback = _fb()
    out = await _drain(b.stream(protected, fallback))

    assert out == ["Hello", " world"]
    assert protected.calls == 1
    assert fallback.calls == 0  # LLM path only
    assert b.state is BreakerState.CLOSED  # clean completion -> streak stays 0


# ── AC#3: consecutive counting vs scattered failures ───────────────────────────


async def test_consecutive_hard_failures_trip(clock: _Clock) -> None:
    b = _breaker(threshold=3)
    for _ in range(2):
        assert await _drain(b.stream(_hard(), _fb())) == ["FALLBACK"]  # served degraded
        assert b.state is BreakerState.CLOSED  # not tripped yet
    # 3rd consecutive hard failure trips it.
    assert await _drain(b.stream(_hard(), _fb())) == ["FALLBACK"]
    assert b.state is BreakerState.OPEN


async def test_scattered_failures_do_not_trip(clock: _Clock) -> None:
    b = _breaker(threshold=3)
    # A success interleaved anywhere resets the streak, so 2+2 scattered never reaches 3.
    await _drain(b.stream(_hard(), _fb()))
    await _drain(b.stream(_hard(), _fb()))
    await _drain(b.stream(_ok(), _fb()))  # success -> streak reset to 0
    await _drain(b.stream(_hard(), _fb()))
    await _drain(b.stream(_hard(), _fb()))
    assert b.state is BreakerState.CLOSED


# ── AC#4: only hard failures count; soft (429/4xx) re-raised untouched ─────────


def test_is_hard_failure_taxonomy() -> None:
    # timeout / connection -> hard
    assert is_hard_failure(TimeoutError()) is True
    assert is_hard_failure(ConnectionError()) is True
    # status-code fallback
    assert is_hard_failure(_HTTPError(500)) is True
    assert is_hard_failure(_HTTPError(503)) is True
    assert is_hard_failure(_HTTPError(429)) is False  # transient throttle, excluded
    assert is_hard_failure(_HTTPError(400)) is False  # our bug, surface it
    assert is_hard_failure(_HTTPError(404)) is False  # non-429 4xx, excluded
    # unrecognized -> fail safe (never trip on surprises)
    assert is_hard_failure(Exception("mystery")) is False

    # status read off a nested .response as well.
    class _Nested(Exception):
        def __init__(self) -> None:
            super().__init__("nested")
            self.response = _HTTPError(502)

    assert is_hard_failure(_Nested()) is True


async def test_soft_failure_reraised_and_not_counted(clock: _Clock) -> None:
    b = _breaker(threshold=1)  # a single hard failure would trip -> proves soft isn't counted
    protected = _Factory(raises=_HTTPError(429))
    fallback = _fb()
    with pytest.raises(_HTTPError):
        await _drain(b.stream(protected, fallback))
    assert fallback.calls == 0  # NOT masked by the degraded fallback
    assert b.state is BreakerState.CLOSED  # streak untouched

    # A non-429 4xx behaves the same.
    protected2 = _Factory(raises=_HTTPError(400))
    with pytest.raises(_HTTPError):
        await _drain(b.stream(protected2, _fb()))
    assert b.state is BreakerState.CLOSED


async def test_hard_5xx_increments(clock: _Clock) -> None:
    b = _breaker(threshold=1)
    assert await _drain(b.stream(_Factory(raises=_HTTPError(500)), _fb())) == ["FALLBACK"]
    assert b.state is BreakerState.OPEN  # 5xx counted -> tripped at threshold 1


# ── AC#5: OPEN serves degraded only, never invokes protected ───────────────────


async def test_open_serves_fallback_never_protected(clock: _Clock) -> None:
    b = _breaker(threshold=1)
    await _drain(b.stream(_hard(), _fb()))  # trip
    assert b.state is BreakerState.OPEN

    protected = _ok("SHOULD-NOT-RUN")
    fallback = _fb()
    out = await _drain(b.stream(protected, fallback))
    assert out == ["FALLBACK"]
    assert protected.calls == 0  # zero LLM calls while OPEN
    assert fallback.calls == 1


# ── AC#6 + #7: HALF_OPEN single probe; probe success -> CLOSED ─────────────────


async def test_half_open_single_probe_then_success_closes(clock: _Clock) -> None:
    b = _breaker(threshold=1, reset=30.0)
    await _drain(b.stream(_hard(), _fb()))  # trip at t0
    assert b.state is BreakerState.OPEN

    clock.tick(30.0)  # reset window elapsed
    assert b.state is BreakerState.HALF_OPEN

    probe = _ok("recovered")  # the single probe -- a real LLM call
    fallback = _fb()
    out = await _drain(b.stream(probe, fallback))
    assert out == ["recovered"]
    assert probe.calls == 1  # invoked exactly once
    assert fallback.calls == 0
    assert b.state is BreakerState.CLOSED  # probe success -> CLOSED, streak 0


# ── AC#8: probe failure -> OPEN with the reset timer restarted from zero ───────


async def test_probe_failure_restarts_timer(clock: _Clock) -> None:
    b = _breaker(threshold=1, reset=30.0)
    await _drain(b.stream(_hard(), _fb()))  # trip at t=1000
    clock.tick(30.0)  # t=1030 -> HALF_OPEN
    assert b.state is BreakerState.HALF_OPEN

    # Probe hard-fails -> back to OPEN, timer restarted from t=1030 (not the original trip).
    assert await _drain(b.stream(_hard(), _fb())) == ["FALLBACK"]
    assert b.state is BreakerState.OPEN

    clock.tick(29.9)  # only ~30s since the ORIGINAL trip, but <30s since the probe failure
    assert b.state is BreakerState.OPEN  # timer measured from the probe failure

    clock.tick(0.1)  # now a full 30s after the probe failure
    assert b.state is BreakerState.HALF_OPEN


# ── AC#9: streaming failure semantics (pre-first-token vs mid-stream) ──────────


async def test_pre_first_token_hard_failure_serves_fallback(clock: _Clock) -> None:
    b = _breaker(threshold=5)
    protected = _hard()  # raises before any token
    fallback = _fb()
    out = await _drain(b.stream(protected, fallback))
    assert out == ["FALLBACK"]  # transparently served degraded for THIS request
    assert fallback.calls == 1
    # ...and the failure was recorded (streak advanced).
    assert b._streak == 1  # noqa: SLF001 -- white-box assertion on the recorded streak


async def test_mid_stream_hard_failure_propagates_no_fallback(clock: _Clock) -> None:
    b = _breaker(threshold=1)
    protected = _Factory(("partial",), raises=TimeoutError(), raise_after=1)  # yields then fails
    fallback = _fb()

    got: list[str] = []
    with pytest.raises(TimeoutError):
        async for tok in b.stream(protected, fallback):
            got.append(tok)

    assert got == ["partial"]  # partial answer already delivered
    assert fallback.calls == 0  # NOT re-emitted after tokens went out
    assert b.state is BreakerState.OPEN  # failure still recorded (protects future requests)


async def test_cancellation_propagates_and_not_counted(clock: _Clock) -> None:
    # asyncio.CancelledError (client disconnect / outer timeout) subclasses BaseException, so
    # `except Exception` lets it propagate untouched -- it is NOT a Groq hard failure. With
    # threshold=1, any counted failure would trip; state staying CLOSED proves streak untouched.
    b = _breaker(threshold=1)
    protected = _Factory(raises=asyncio.CancelledError())  # cancelled before any token
    fallback = _fb()

    with pytest.raises(asyncio.CancelledError):
        await _drain(b.stream(protected, fallback))

    assert b.state is BreakerState.CLOSED  # streak NOT advanced -> not tripped
    assert fallback.calls == 0  # cancellation is not degraded output


# ── AC#10: wiring (AgentContext dep + synthesize_node routing + degraded_stream) ─


def test_agent_context_carries_breaker() -> None:
    fields = {f.name for f in AgentContext.__dataclass_fields__.values()}
    assert "breaker" in fields
    assert AgentContext.__dataclass_fields__["breaker"].type == "SynthesisCircuitBreaker"


async def test_degraded_stream_formats_chunks() -> None:
    chunks = [
        ScoredChunk(
            chunk=RetrievedChunk(
                chunk_id="c1",
                text="Revenue grew 8%.",
                section="Item 7",
                ticker="AAPL",
                period_year=2023,
                filing_type="10-K",
            ),
            rerank_score=2.0,
        )
    ]
    out = "".join([tok async for tok in degraded_stream(chunks)])
    assert "temporarily unavailable" in out  # honest degraded preamble, not an error
    assert "[AAPL 10-K 2023 · Item 7]" in out  # source tag
    assert "Revenue grew 8%." in out  # chunk text surfaced verbatim


async def test_degraded_stream_empty() -> None:
    out = "".join([tok async for tok in degraded_stream([])])
    assert "No relevant filing excerpts" in out


async def test_synthesize_node_routes_open_breaker_to_degraded() -> None:
    """OPEN breaker in the context -> synthesize_node's answer_stream is the degraded path,
    and the LLM is never streamed."""

    class _NoStreamLLM:
        def astream(self, messages: Any) -> AsyncGenerator[Any, None]:
            raise AssertionError("LLM must not be streamed while the breaker is OPEN")

    breaker = _breaker(threshold=1, reset=30.0)
    breaker._record_hard_failure()  # noqa: SLF001 -- trip directly (threshold 1) for the test
    assert breaker.state is BreakerState.OPEN

    ctx = AgentContext(
        llm=cast(ChatGroq, _NoStreamLLM()),
        reranker=cast(CrossEncoder, object()),
        pool=cast(Pool, object()),
        allowed_tickers=frozenset({"AAPL"}),
        breaker=breaker,
    )
    reranked = [
        ScoredChunk(
            chunk=RetrievedChunk(
                chunk_id="c1",
                text="Cash flow was strong.",
                section="Item 7",
                ticker="AAPL",
                period_year=2023,
                filing_type="10-K",
            ),
            rerank_score=1.0,
        )
    ]
    state: AgentState = cast(
        AgentState,
        {
            "original_query": "q",
            "request_id": "r",
            "user_id": None,
            "query_plan": None,
            "unavailable_tickers": [],
            "query": "q",
            "iteration": 0,
            "retrieved_chunks": [],
            "reranked_chunks": reranked,
            "confidence": "high",
            "confidence_reason": "none",
            "coverage_gaps": [],
            "citations": [],
            "answer_stream": None,
        },
    )
    out = await synthesize_node(state, Runtime(context=ctx))
    text = "".join([tok async for tok in out["answer_stream"]])
    assert "Cash flow was strong." in text  # degraded evidence surfaced
    assert "[AAPL 10-K 2023 · Item 7]" in text
    # One citation per reranked chunk regardless of degraded mode.
    assert [c.chunk_id for c in out["citations"]] == ["c1"]
