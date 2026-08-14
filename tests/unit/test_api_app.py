"""Unit tests for src/alphalens/api/app.py (S18) -- AC1, AC2, AC3, AC11, AC12.

Fakes only: zero Groq calls, zero Neon connections. ``build_context`` / ``build_graph`` are
monkeypatched at their ``alphalens.api.app`` binding, so the lifespan runs its real control flow
against a counting spy and a close-counting pool.

TestClient-driven cases are deliberately SYNC defs: ``TestClient`` is synchronous and drives the
ASGI app on its own portal thread, which does not compose with an already-running event loop.
Lifespan-internals cases are async and drive ``lifespan(app)`` directly.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest
from asyncpg import Pool
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_groq import ChatGroq
from sentence_transformers import CrossEncoder

import alphalens.api.app as app_mod
from alphalens.agent.circuit_breaker import BreakerState, SynthesisCircuitBreaker
from alphalens.agent.nodes import AgentContext, degraded_stream, stream_synthesis
from alphalens.agent.state import (
    AgentState,
    Citation,
    QueryPlan,
    RetrievedChunk,
    ScoredChunk,
    TimeRange,
)
from alphalens.api.app import DEFAULT_CORS_ORIGINS, create_app, lifespan, query_stream
from alphalens.api.schemas import QueryRequest
from alphalens.etl.embeddings import EmbeddingClient

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakePool:
    """asyncpg Pool stand-in: counts closes. The lifespan never queries it."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _SpyBuildContext:
    """Counting spy over the cold-start seam (AC2).

    Records every invocation AND every pool it handed out, so "called exactly once" and
    "exactly one pool created" are two separate assertions rather than one inference.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.pools: list[_FakePool] = []

    async def __call__(self) -> tuple[Any, Any]:
        self.calls += 1
        pool = _FakePool()
        self.pools.append(pool)
        # A real AgentContext (see `_fake_context` below): /query reads `ctx.breaker` for the
        # terminal event, so a bare sentinel would only work for the /health-only cases.
        return _fake_context(), pool


def _install_spies(monkeypatch: pytest.MonkeyPatch) -> _SpyBuildContext:
    spy = _SpyBuildContext()
    monkeypatch.setattr(app_mod, "build_context", spy)
    monkeypatch.setattr(app_mod, "build_graph", lambda: object())
    return spy


# ── AC1: import-safety ────────────────────────────────────────────────────────


def test_import_with_no_dotenv_and_scrubbed_env(tmp_path: Path) -> None:
    """AC1, verified honestly rather than by proxy: import the module in a FRESH interpreter
    whose environment carries none of the app's variables and whose CWD contains no `.env`.

    A module-level `get_settings()`, DB connect, or model load would raise (ValidationError or
    a connection error) and the subprocess would exit non-zero. The in-process spy tests below
    cannot show this, because this interpreter has already loaded the repo's real `.env`.
    """
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "SYSTEMROOT")}
    probe = "import alphalens.api.app as m; assert not hasattr(m, 'app'); print('IMPORT_OK')"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        cwd=tmp_path,  # no .env discoverable from here
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr}"
    assert "IMPORT_OK" in result.stdout


def test_create_app_acquires_no_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: `create_app()` registers middleware and routes and nothing else -- resources are
    acquired only when the lifespan enters."""
    spy = _install_spies(monkeypatch)
    create_app()
    assert spy.calls == 0


def test_create_app_returns_a_fresh_instance_each_call() -> None:
    """AC1: a factory, not a module-level singleton -- so tests never share app state."""
    assert create_app() is not create_app()


# ── AC2: single cold start ────────────────────────────────────────────────────


def test_build_context_called_once_across_n_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2/D2: N sequential requests to a running app cold-start exactly once and create exactly
    one pool -- asserted with a counting spy, not by inspection."""
    spy = _install_spies(monkeypatch)
    with TestClient(create_app()) as client:
        for _ in range(3):
            assert client.get("/health").status_code == 200
    assert spy.calls == 1
    assert len(spy.pools) == 1


def test_lifespan_stashes_the_three_state_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `app.state` names are a de-facto contract for the v2 Lambda spec; pin them."""
    _install_spies(monkeypatch)
    app = create_app()
    with TestClient(app):
        assert app.state.agent_context is not None
        assert app.state.pool is not None
        assert app.state.graph is not None


# ── AC3: pool teardown ────────────────────────────────────────────────────────


def test_pool_closed_once_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: closed exactly once, and no Pool object survives lifespan exit."""
    spy = _install_spies(monkeypatch)
    app = create_app()
    with TestClient(app) as client:
        client.get("/health")
    assert spy.pools[0].close_calls == 1
    assert app.state.pool is None


async def test_pool_closed_once_when_the_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: the `finally` holds when something inside the lifespan's scope raises -- the pool is
    still closed exactly once and the state attribute is still cleared."""
    spy = _install_spies(monkeypatch)
    app = create_app()

    with pytest.raises(RuntimeError, match="in-flight failure"):
        async with lifespan(app):
            raise RuntimeError("in-flight failure")

    assert spy.pools[0].close_calls == 1
    assert app.state.pool is None
    assert app.state.agent_context is None
    assert app.state.graph is None


async def test_pool_closed_when_graph_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: `build_graph()` sits INSIDE the try, so a topology failure cannot leak the pool that
    `build_context()` already opened."""
    spy = _SpyBuildContext()
    monkeypatch.setattr(app_mod, "build_context", spy)

    def _boom() -> Any:
        raise RuntimeError("topology failure")

    monkeypatch.setattr(app_mod, "build_graph", _boom)
    app = create_app()

    with pytest.raises(RuntimeError, match="topology failure"):
        async with lifespan(app):
            pytest.fail("lifespan body must not run when build_graph raises")

    assert spy.pools[0].close_calls == 1
    assert app.state.pool is None


# ── AC11: CORS ────────────────────────────────────────────────────────────────
#
# Starlette 1.0.1 behaviour, OBSERVED before these tests were written (S18 execution note):
#   * allowed preflight   -> 200 with `access-control-allow-origin: <origin>`
#   * disallowed preflight-> 400 "Disallowed CORS origin", and it STILL emits
#     `access-control-allow-methods` / `-allow-headers` / `-max-age` -- but NOT
#     `access-control-allow-origin`.
# So the negative assertion is on the ABSENCE OF `access-control-allow-origin` specifically.
# Asserting "no access-control-* headers at all" would fail against this Starlette and would be
# testing the wrong thing: allow-origin is the header that actually grants the browser access.


def test_preflight_from_localhost_3000_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_spies(monkeypatch)
    response = TestClient(create_app()).options(
        "/query",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_preflight_from_unlisted_origin_gets_no_allow_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_spies(monkeypatch)
    response = TestClient(create_app()).options(
        "/query",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_simple_request_from_unlisted_origin_gets_no_allow_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-preflighted request is not blocked server-side, but it must not come back with the
    header that would let the browser hand the body to the page."""
    _install_spies(monkeypatch)
    response = TestClient(create_app()).get("/health", headers={"Origin": "http://evil.test"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_origins_are_a_parameter_and_never_wildcard() -> None:
    """D8 + Q6/Q7: origins arrive as a `create_app` argument -- no config field, no
    `get_settings()` call -- and the default allowlist never contains `"*"`."""
    assert "*" not in DEFAULT_CORS_ORIGINS
    assert DEFAULT_CORS_ORIGINS == ["http://localhost:3000", "http://127.0.0.1:3000"]

    custom = TestClient(create_app(cors_origins=["http://localhost:4321"])).options(
        "/query",
        headers={
            "Origin": "http://localhost:4321",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert custom.headers["access-control-allow-origin"] == "http://localhost:4321"


# ── AC12: health ──────────────────────────────────────────────────────────────


def test_health_is_liveness_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC12: 200 `{"status": "ok"}` WITHOUT touching the DB, the LLM or the reranker.

    Driven through a TestClient built WITHOUT the context manager, so the lifespan never runs:
    the route answers with no context, no pool and no graph in existence, which is the strongest
    available statement that it reads none of them.
    """
    spy = _install_spies(monkeypatch)
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert spy.calls == 0


def test_health_answers_after_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Liveness must not depend on lifespan state that shutdown has already cleared."""
    _install_spies(monkeypatch)
    app = create_app()
    client = TestClient(app)
    with client:
        pass  # startup + shutdown
    assert client.get("/health").json() == {"status": "ok"}
    assert cast(Any, app.state).pool is None


# ══════════════════════════════════════════════════════════════════════════════
# POST /query -- the SSE stream (AC4, AC5, AC6, AC7, AC10, AC13, AC15)
# ══════════════════════════════════════════════════════════════════════════════


class _NoStreamLLM:
    """Guard fake: fails loudly if the LLM is ever streamed.

    Copied (not imported) from tests/unit/test_circuit_breaker.py:363 -- it is defined INSIDE a
    test body there, so it is not an importable symbol. Same guard shape as S_CR Phase 4.
    """

    def astream(self, messages: Any) -> AsyncGenerator[Any, None]:
        raise AssertionError("LLM must not be streamed while the breaker is OPEN")


class _FakeGraph:
    """Compiled-graph stand-in: returns a canned final state, or raises from ``ainvoke``."""

    def __init__(self, state: AgentState | None = None, error: Exception | None = None) -> None:
        self._state = state
        self._error = error
        self.calls = 0

    async def ainvoke(self, intake: Any, *, context: Any = None) -> AgentState:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._state is not None
        return self._state


def _fake_context(breaker: SynthesisCircuitBreaker | None = None) -> AgentContext:
    """A real AgentContext whose only live member is the breaker -- the one field the API reads.

    Mirrors the construction in tests/unit/test_nodes.py:131: fakes cast into the typed fields.
    """
    return AgentContext(
        llm=cast(ChatGroq, _NoStreamLLM()),
        reranker=cast(CrossEncoder, object()),
        pool=cast(Pool, object()),
        allowed_tickers=frozenset({"AAPL"}),
        breaker=breaker
        if breaker is not None
        else SynthesisCircuitBreaker(failure_threshold=3, reset_timeout_seconds=30.0),
        embedder=cast(EmbeddingClient, object()),
        ticker_roster={},
        corpus_min_year=2021,
        corpus_max_year=2026,
    )


async def _stream(*tokens: str) -> AsyncGenerator[str, None]:
    for token in tokens:
        yield token


def _scored(text: str = "Cash flow was strong.") -> ScoredChunk:
    return ScoredChunk(
        chunk=RetrievedChunk(
            chunk_id="c1",
            text=text,
            section="Item 7",
            ticker="AAPL",
            period_year=2023,
            filing_type="10-K",
        ),
        rerank_score=1.0,
    )


def _query_state(**overrides: Any) -> AgentState:
    base: dict[str, Any] = {
        "original_query": "q",
        "request_id": "unused -- query_stream mints its own",
        "user_id": None,
        "query_plan": QueryPlan(
            tickers=["AAPL"],
            intent="factual",
            time_range=TimeRange(years=[2023]),
            sub_questions=["revenue"],
            entities=[],
            unresolved_companies=[],
        ),
        "unavailable_tickers": [],
        "unavailable_companies": [],
        "unavailable_years": [],
        "query": "q",
        "iteration": 0,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "dropped_for_capacity": [],
        "confidence": "high",
        "confidence_reason": "none",
        "coverage_gaps": [],
        "capacity_drops": [],
        "citations": [
            Citation(
                chunk_id="c1",
                ticker="AAPL",
                filing_type="10-K",
                period_year=2023,
                section="Item 7",
            )
        ],
        "answer_stream": None,
    }
    base.update(overrides)
    return cast(AgentState, base)


def _stub_app(
    *,
    state: AgentState | None = None,
    error: Exception | None = None,
    ctx: AgentContext | None = None,
) -> FastAPI:
    """An app with `app.state` populated directly -- no lifespan, no cold start."""
    app = create_app()
    app.state.agent_context = ctx if ctx is not None else _fake_context()
    app.state.graph = _FakeGraph(state, error)
    app.state.pool = None
    return app


async def _collect(app: FastAPI, question: str = "q") -> list[dict[str, str]]:
    return [event async for event in query_stream(app, QueryRequest(question=question))]


def _names(events: list[dict[str, str]]) -> list[str]:
    return [event["event"] for event in events]


def _payload(event: dict[str, str]) -> Any:
    return json.loads(event["data"])


def _answer(events: list[dict[str, str]]) -> str:
    return "".join(_payload(e)["text"] for e in events if e["event"] == "token")


# ── AC4 / AC15: event order and the citations array ───────────────────────────


async def test_event_sequence_is_meta_tokens_citations_done() -> None:
    """AC4: exactly `meta` -> `token`* -> `citations` -> `done`, in that order, every time."""
    app = _stub_app(state=_query_state(answer_stream=_stream("Hello", " world")))
    events = await _collect(app)

    assert _names(events) == ["meta", "token", "token", "citations", "done"]
    assert _answer(events) == "Hello world"


async def test_citations_is_one_array_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC15: ONE event carrying an array -- never one event per citation."""
    two = [
        Citation(
            chunk_id=f"c{i}", ticker="AAPL", filing_type="10-K", period_year=2023, section=None
        )
        for i in (1, 2)
    ]
    app = _stub_app(state=_query_state(answer_stream=_stream("x"), citations=two))
    events = await _collect(app)

    assert _names(events).count("citations") == 1
    payload = _payload(events[-2])
    assert isinstance(payload, list)
    assert [c["chunk_id"] for c in payload] == ["c1", "c2"]
    assert payload[0]["section"] is None


async def test_citations_event_is_emitted_when_the_list_is_empty() -> None:
    """AC4/AC15: `[]` is still emitted -- the empty case must not be indistinguishable from
    'not yet arrived'."""
    app = _stub_app(state=_query_state(answer_stream=_stream("x"), citations=[]))
    events = await _collect(app)

    assert _names(events) == ["meta", "token", "citations", "done"]
    assert _payload(events[-2]) == []


async def test_every_event_payload_is_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """D6/Q8: one parse path for S19. A newline-only token must round-trip intact, which raw
    text in `data:` cannot do."""
    app = _stub_app(state=_query_state(answer_stream=_stream("\n", "line\n\n")))
    events = await _collect(app)

    for event in events:
        json.loads(event["data"])  # raises if any payload is raw text
    assert _answer(events) == "\nline\n\n"


async def test_done_carries_latency_and_the_token_event_count() -> None:
    """Q11: `token_count` counts EMITTED `token` events, not model tokens."""
    app = _stub_app(state=_query_state(answer_stream=_stream("a", "b", "c")))
    events = await _collect(app)

    done = _payload(events[-1])
    assert done["token_count"] == 3
    assert done["latency_s"] >= 0.0
    assert done["breaker_open"] is False


async def test_meta_is_built_from_the_returned_state() -> None:
    """The wire metadata is the honesty rail, not a re-derivation."""
    app = _stub_app(
        state=_query_state(
            answer_stream=_stream("x"),
            confidence="low",
            confidence_reason="coverage",
            coverage_gaps=[("AAPL", 2024)],
            unavailable_companies=["Coca-Cola"],
        )
    )
    meta = _payload((await _collect(app))[0])

    assert meta["confidence"] == "low"
    assert meta["confidence_reason"] == "coverage"
    assert meta["coverage_gaps"] == [{"ticker": "AAPL", "year": 2024}]
    assert meta["unavailable_companies"] == ["Coca-Cola"]
    assert meta["request_id"]  # server-minted (Q12)


async def test_request_id_is_server_minted_and_differs_per_request() -> None:
    """Q12: no client-supplied id -- `QueryRequest` has no field for one."""
    app = _stub_app(state=_query_state(answer_stream=_stream("x")))
    first = _payload((await _collect(app))[0])["request_id"]

    app.state.graph = _FakeGraph(_query_state(answer_stream=_stream("x")))
    second = _payload((await _collect(app))[0])["request_id"]

    assert first != second
    assert "request_id" not in QueryRequest.model_fields


# ── AC5: the un-drained invariant ─────────────────────────────────────────────


async def test_no_token_is_emitted_before_ainvoke_returns() -> None:
    """AC5 (S16 AC11): the drain -- and therefore S14's lazy breaker gate -- happens INSIDE the
    SSE generator, strictly after `ainvoke` has returned.

    Pinned by execution order rather than by inspection: the stream records when its first
    `__anext__` runs, and that must not happen until the consumer pulls the first `token`.
    """
    order: list[str] = []

    async def _instrumented() -> AsyncGenerator[str, None]:
        order.append("drain")
        yield "x"

    class _RecordingGraph(_FakeGraph):
        async def ainvoke(self, intake: Any, *, context: Any = None) -> AgentState:
            order.append("ainvoke")
            return await super().ainvoke(intake, context=context)

    app = create_app()
    app.state.agent_context = _fake_context()
    app.state.graph = _RecordingGraph(_query_state(answer_stream=_instrumented()))

    generator = query_stream(app, QueryRequest(question="q"))
    first = await generator.__anext__()
    assert first["event"] == "meta"
    assert order == ["ainvoke"], "the answer stream must still be un-drained at meta time"

    second = await generator.__anext__()
    assert second["event"] == "token"
    assert order == ["ainvoke", "drain"]
    await generator.aclose()


# ── AC6: the degraded path ────────────────────────────────────────────────────


async def test_open_breaker_streams_degraded_and_reports_breaker_open() -> None:
    """AC6: breaker forced OPEN -> a well-formed sequence, the degraded preamble as `token`
    events, `DoneEvent.breaker_open is True`, and NO LLM call.

    The no-LLM guard is `_NoStreamLLM.astream` raising `AssertionError`. Note that an
    `AssertionError` escaping the drain would be caught by the generator's `except Exception` and
    turned into an `error` event rather than failing the test outright -- which is exactly why
    the event-name sequence is asserted exactly, with no `error` in it.
    """
    breaker = SynthesisCircuitBreaker(failure_threshold=1, reset_timeout_seconds=30.0)
    breaker._record_hard_failure()  # noqa: SLF001 -- trip directly, as test_circuit_breaker does
    assert breaker.state is BreakerState.OPEN

    ctx = _fake_context(breaker=breaker)
    chunks = [_scored()]

    def _protected() -> AsyncGenerator[str, None]:
        return stream_synthesis(ctx.llm, [("system", "s"), ("human", "h")])

    def _fallback() -> AsyncGenerator[str, None]:
        return degraded_stream(chunks)

    state = _query_state(
        answer_stream=ctx.breaker.stream(_protected, _fallback),
        reranked_chunks=chunks,
    )
    events = await _collect(_stub_app(state=state, ctx=ctx))

    assert _names(events) == ["meta", "token", "token", "citations", "done"]
    assert "Synthesis is temporarily unavailable" in _answer(events)
    assert "Cash flow was strong." in _answer(events)
    assert _payload(events[-1])["breaker_open"] is True


async def test_closed_breaker_reports_breaker_open_false() -> None:
    """The other side of AC6: a healthy run must not claim the breaker was open."""
    app = _stub_app(state=_query_state(answer_stream=_stream("x")))
    assert _payload((await _collect(app))[-1])["breaker_open"] is False


# ── AC7: in-band errors ───────────────────────────────────────────────────────


async def test_graph_exception_yields_a_single_error_event() -> None:
    """AC7: an exception during `ainvoke` -> one `error` event, phase "graph", no tokens and no
    meta (there is no final_state to build meta from)."""
    app = _stub_app(error=RuntimeError("neon exploded"))
    events = await _collect(app)

    assert _names(events) == ["error"]
    assert _payload(events[0])["phase"] == "graph"
    assert _payload(events[0])["message"] == "RuntimeError during graph"


async def test_missing_answer_stream_yields_meta_then_error_phase_graph() -> None:
    """AC7/Q10: `ainvoke` succeeded, so the honesty-rail metadata is in hand and IS emitted --
    it is exactly what the user needs when there is no answer. The phase is still "graph": the
    drain never began. No `token` events precede the error."""
    app = _stub_app(state=_query_state(answer_stream=None))
    events = await _collect(app)

    assert _names(events) == ["meta", "error"]
    assert _payload(events[1])["phase"] == "graph"
    assert _payload(events[1])["message"] == "RuntimeError during graph"


async def test_mid_drain_exception_keeps_tokens_then_errors_phase_stream() -> None:
    """AC7: the tokens emitted so far survive, then a terminal `error` with phase "stream".
    Citations are deliberately dropped -- they describe a complete answer."""

    async def _breaks() -> AsyncGenerator[str, None]:
        yield "one"
        yield "two"
        raise ConnectionError("groq dropped the connection")

    app = _stub_app(state=_query_state(answer_stream=_breaks()))
    events = await _collect(app)

    assert _names(events) == ["meta", "token", "token", "error"]
    assert _answer(events) == "onetwo"
    assert _payload(events[-1])["phase"] == "stream"
    assert _payload(events[-1])["message"] == "ConnectionError during stream"
    assert "citations" not in _names(events)


async def test_error_message_never_leaks_the_exception_text() -> None:
    """AC7: no traceback, no DSN, no DB URL. asyncpg and httpx put connection strings in
    `str(exc)`, so the message is built from the exception TYPE alone."""
    secret = "postgresql://user:hunter2@ep-neon.aws.neon.tech/alphalens?sslmode=require"
    app = _stub_app(error=ConnectionError(secret))
    payload = _payload((await _collect(app))[0])

    assert payload["message"] == "ConnectionError during graph"
    assert "hunter2" not in json.dumps(payload)
    assert "postgresql" not in json.dumps(payload)
    assert "Traceback" not in json.dumps(payload)


async def test_error_event_carries_breaker_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """N1: `error` is terminal and `done` never fires, so the breaker signal rides here. A run
    whose breaker is OPEN and which then fails mid-drain must still report it."""

    async def _breaks() -> AsyncGenerator[str, None]:
        yield "partial"
        raise ConnectionError("dropped")

    breaker = SynthesisCircuitBreaker(failure_threshold=1, reset_timeout_seconds=30.0)
    breaker._record_hard_failure()  # noqa: SLF001
    ctx = _fake_context(breaker=breaker)

    app = _stub_app(state=_query_state(answer_stream=_breaks()), ctx=ctx)
    events = await _collect(app)

    assert _names(events) == ["meta", "token", "error"]
    assert _payload(events[-1])["breaker_open"] is True


async def test_generator_never_raises_to_the_caller() -> None:
    """AC7: whatever happens, `query_stream` terminates normally."""
    app = _stub_app(error=ValueError("boom"))
    events = await _collect(app)  # would propagate if the generator re-raised
    assert _names(events) == ["error"]


# ── AC10: client disconnect ───────────────────────────────────────────────────


async def test_disconnect_closes_the_underlying_stream_and_logs_no_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC10: closing the SSE generator mid-stream closes the answer stream underneath it and
    logs nothing at ERROR.

    `async for` does NOT close its iterator, so without the generator's `finally` the
    breaker-wrapped Groq stream would be left suspended and unclosed on a closed browser tab.
    """
    closed = False

    async def _closable() -> AsyncGenerator[str, None]:
        nonlocal closed
        try:
            yield "one"
            yield "two"
        finally:
            closed = True

    app = _stub_app(state=_query_state(answer_stream=_closable()))

    with caplog.at_level(logging.DEBUG):
        generator = query_stream(app, QueryRequest(question="q"))
        assert (await generator.__anext__())["event"] == "meta"
        assert (await generator.__anext__())["event"] == "token"
        await generator.aclose()  # the client hung up

    assert closed, "the underlying answer stream must be closed"
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_pool_is_unaffected_by_a_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC10: the pool is untouched and the next request succeeds."""
    spy = _install_spies(monkeypatch)
    monkeypatch.setattr(app_mod, "build_graph", lambda: _FakeGraph(_query_state()))
    app = create_app()

    with TestClient(app):
        app.state.graph = _FakeGraph(_query_state(answer_stream=_stream("a", "b")))
        generator = query_stream(app, QueryRequest(question="q"))
        await generator.__anext__()
        await generator.aclose()

        app.state.graph = _FakeGraph(_query_state(answer_stream=_stream("c")))
        assert _names(await _collect(app)) == ["meta", "token", "citations", "done"]

    assert spy.pools[0].close_calls == 1


# ── AC13: validation happens before the stream opens ──────────────────────────


def test_empty_question_is_422_and_never_opens_a_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC13/G2: fail BEFORE you flush. The client gets a real status code, not an in-band
    `error` event -- request validation and stream errors are handled in different places."""
    _install_spies(monkeypatch)
    response = TestClient(_stub_app(state=_query_state())).post("/query", json={"question": ""})

    assert response.status_code == 422
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_missing_question_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_spies(monkeypatch)
    response = TestClient(_stub_app(state=_query_state())).post("/query", json={})
    assert response.status_code == 422


# ── End-to-end through the real EventSourceResponse ───────────────────────────


def test_post_query_emits_wellformed_sse_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the D6 wiring is real: the framing below is produced by sse-starlette, not by the
    generator's dicts, and it is what `curl -N` and S19's `fetch` reader will see."""
    ctx = _fake_context()
    state = _query_state(answer_stream=_stream("Hello", " world"))

    async def _fake_build_context() -> tuple[Any, Any]:
        return ctx, _FakePool()

    monkeypatch.setattr(app_mod, "build_context", _fake_build_context)
    monkeypatch.setattr(app_mod, "build_graph", lambda: _FakeGraph(state))

    with TestClient(create_app()) as client:
        response = client.post("/query", json={"question": "How did AAPL revenue trend?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    body = response.text
    assert body.index("event: meta") < body.index("event: token")
    assert body.index("event: token") < body.index("event: citations")
    assert body.index("event: citations") < body.index("event: done")
    assert 'data: {"text": "Hello"}' in body


def test_build_context_called_once_across_n_query_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2, driven through the real endpoint rather than /health."""
    spy = _SpyBuildContext()
    monkeypatch.setattr(app_mod, "build_context", spy)
    monkeypatch.setattr(app_mod, "build_graph", lambda: _FakeGraph(_query_state()))
    app = create_app()

    with TestClient(app) as client:
        for _ in range(3):
            app.state.graph = _FakeGraph(_query_state(answer_stream=_stream("x")))
            assert client.post("/query", json={"question": "q"}).status_code == 200

    assert spy.calls == 1
    assert len(spy.pools) == 1
