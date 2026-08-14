"""AlphaLens v8 -- FastAPI + SSE local app (S18).

WRAPPER ONLY (D1). This module puts the existing v1 agent behind a local HTTP surface. Every
retrieval, reranking, evaluation and honesty decision continues to happen exactly where it
already happens -- nothing here re-implements a node, the graph, or the streaming seam. If a
change here seems to require editing ``nodes.py`` / ``graph.py`` / ``prompts.py`` / ``state.py``,
that is a signal to stop and re-scope.

Import-safety (AC1): importing this module performs no DB connection, no model load and no
``await``. ``create_app()`` is likewise side-effect-free -- it registers middleware and routes
and nothing else. In particular it does NOT call ``get_settings()``: that would validate the
entire environment at construction time and raise with no ``.env`` present (S18 Q6/Q7), which is
why CORS origins arrive as a parameter rather than from config.

Resources are acquired exactly once, when the lifespan enters (D2).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from alphalens.agent.circuit_breaker import BreakerState
from alphalens.agent.context import build_context
from alphalens.agent.graph import build_graph
from alphalens.agent.nodes import AgentContext
from alphalens.agent.state import AgentState
from alphalens.api.schemas import (
    CitationOut,
    DoneEvent,
    ErrorEvent,
    MetaEvent,
    QueryRequest,
)

logger = logging.getLogger(__name__)

AgentGraph = CompiledStateGraph[AgentState, AgentContext, AgentState, AgentState]

# D8: explicit allowlist, never `["*"]`. Next.js dev serves on :3000 while this app binds to
# 127.0.0.1:8000 -- different origins, so the browser blocks the request without CORS.
DEFAULT_CORS_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """D2 cold-start seam: build the context + graph ONCE, close the pool on shutdown.

    The app-scope twin of run_query.py's try/finally (S16 G7). ``build_context()`` opens an
    asyncpg pool AND loads the ~80 MB cross-encoder, so doing this per request would add seconds
    to every query and open a pool per request.

    ``app.state`` attribute names -- ``agent_context`` / ``pool`` / ``graph`` -- are a de-facto
    contract: the v2 Lambda spec and any future middleware read the same three names. Renaming
    them is a breaking change, not a refactor.
    """
    ctx, pool = await build_context()
    try:
        app.state.agent_context = ctx
        app.state.pool = pool
        # build_graph() is inside the try so that a topology failure still closes the pool.
        app.state.graph = build_graph()
        logger.info("agent cold start complete: context, pool and graph ready")
        yield
    finally:
        await pool.close()
        # AC3: no Pool object survives lifespan exit -- the attribute is cleared, not merely
        # closed, so a post-shutdown reader gets None rather than a dead pool.
        app.state.pool = None
        app.state.agent_context = None
        app.state.graph = None
        logger.info("agent shutdown complete: pool closed")


# ── SSE plumbing ─────────────────────────────────────────────────────────────


def _sse(event: str, payload: BaseModel | list[Any] | dict[str, Any]) -> dict[str, str]:
    """Frame one sse-starlette event dict with a JSON-encoded ``data`` field (D6/Q8).

    Single chokepoint for encoding, so no call site can emit raw text by accident. Raw text in
    ``data:`` is NOT newline-safe -- SSE splits the payload on ``\\n`` and the client rejoins the
    parts, so a token that is exactly ``"\\n"`` (or ends in one) does not round-trip. That is not
    hypothetical here: ``degraded_stream`` emits newline-heavy chunks and ``LOW_CONFIDENCE_CAVEAT``
    ends in ``"\\n\\n"``.
    """
    data = payload.model_dump_json() if isinstance(payload, BaseModel) else json.dumps(payload)
    return {"event": event, "data": data}


def _breaker_open(ctx: AgentContext) -> bool:
    """Read breaker state for the terminal event (N1/N2).

    Reports what the BREAKER is, NOT whether fallback content was served. The two diverge on the
    CLOSED-boundary path (``circuit_breaker.py:140-145``): a CLOSED breaker can hard-fail before
    the first token, serve ``degraded_stream``, and record a failure that leaves the streak below
    threshold -- excerpts go out while this still reads False. Documented, deferred to v2.

    A second, narrower imprecision on the ``phase="stream"`` path: the failure that ended the
    drain may itself be the one that trips the breaker, so a True here can describe a state this
    very request caused rather than a pre-existing outage. Both are accepted -- the field claims
    only what it measures.
    """
    return ctx.breaker.state is BreakerState.OPEN


def _error_event(exc: BaseException, phase: str, ctx: AgentContext) -> ErrorEvent:
    """Build the in-band error payload (AC7).

    ``message`` is the exception's TYPE plus the phase and nothing else. ``str(exc)`` is
    deliberately never included: asyncpg and httpx routinely put DSNs, connection strings and
    response bodies in there, and this payload goes straight to the browser.
    """
    return ErrorEvent(
        message=f"{type(exc).__name__} during {phase}",
        phase=phase,
        breaker_open=_breaker_open(ctx),
    )


async def query_stream(app: FastAPI, req: QueryRequest) -> AsyncIterator[dict[str, str]]:
    """The SSE generator: run the graph, emit ``meta``, drain, emit ``citations`` then ``done``.

    Event order on the happy path is exactly ``meta`` -> ``token``* -> ``citations`` -> ``done``
    (AC4). ``request_id`` is minted here with ``uuid4()`` (Q12) and the intake mirrors
    run_query.py's five keys exactly (S16 G4).

    This generator never raises to the caller: both failure phases become a terminal ``error``
    event (AC7). ``except Exception`` -- never ``BaseException`` -- so ``CancelledError`` and
    ``GeneratorExit`` propagate untouched on client disconnect (AC10), exactly as S14's breaker
    does at ``circuit_breaker.py:136``.
    """
    ctx: AgentContext = app.state.agent_context
    graph: AgentGraph = app.state.graph
    request_id = str(uuid4())
    token_count = 0

    # AgentState is an all-required TypedDict; the nodes fill the rest. The cast keeps
    # mypy --strict happy with the partial intake literal, mirroring run_query.py:40-49.
    intake = cast(
        AgentState,
        {
            "original_query": req.question,
            "query": req.question,  # == original_query in v1 (L6)
            "request_id": request_id,
            "user_id": req.user_id,
            "iteration": 0,
        },
    )

    # ── Phase "graph" ────────────────────────────────────────────────────────
    # t0 starts immediately before ainvoke, matching run_query.py:51, so DoneEvent.latency_s and
    # the CLI footer's `latency=` measure the same span.
    t0 = time.monotonic()
    try:
        # langgraph types ainvoke's return as `dict[str, Any] | Any`; the compiled graph's
        # output schema IS AgentState (graph.py:34), so name it as such at this one boundary.
        final_state = cast(AgentState, await graph.ainvoke(intake, context=ctx))
    except Exception as exc:
        # No final_state exists, so there is no meta to emit -- the error IS the whole stream.
        logger.error("query %s failed during graph execution", request_id, exc_info=True)
        yield _sse("error", _error_event(exc, "graph", ctx))
        return

    # D4: metadata brackets the answer. This lands after ainvoke returns and before any token,
    # which is what makes AC5 structural rather than merely asserted.
    yield _sse("meta", MetaEvent.from_state(final_state, request_id))

    stream = final_state["answer_stream"]
    if stream is None:
        # ainvoke succeeded, so the honesty-rail metadata is in hand and was emitted above --
        # exactly what the user needs when there is no answer. The phase is still "graph": the
        # graph failed to produce a stream, so the drain never began (Q10).
        missing = RuntimeError("graph returned no answer_stream")
        logger.error("query %s returned no answer_stream", request_id, exc_info=missing)
        yield _sse("error", _error_event(missing, "graph", ctx))
        return

    # ── Phase "stream" ───────────────────────────────────────────────────────
    try:
        try:
            async for token in stream:  # D3: S14's lazy breaker gate fires on this first pull
                token_count += 1
                yield _sse("token", {"text": token})
        finally:
            # `async for` does NOT close its iterator. Without this, a client disconnect would
            # leave the breaker-wrapped Groq generator suspended and unclosed (AC10). No yields
            # in here: yielding while a GeneratorExit is in flight raises RuntimeError.
            await stream.aclose()
    except Exception as exc:
        # Citations are deliberately DROPPED here: they describe a complete answer, and citing
        # sources for a truncated one is misleading. `error` is terminal -- no trailing `done`.
        logger.error("query %s failed while draining the answer", request_id, exc_info=True)
        yield _sse("error", _error_event(exc, "stream", ctx))
        return

    # AC15: ONE citations event carrying the whole array, emitted even when empty.
    yield _sse(
        "citations", [CitationOut.from_citation(c).model_dump() for c in final_state["citations"]]
    )
    yield _sse(
        "done",
        DoneEvent(
            latency_s=time.monotonic() - t0,
            token_count=token_count,
            breaker_open=_breaker_open(ctx),
        ),
    )


def create_app(cors_origins: list[str] | None = None) -> FastAPI:
    """App factory. Registers the lifespan, CORS (D8) and the routes.

    A factory rather than a module-level ``app = FastAPI()`` so importing this module has no
    side effects and tests can build a fresh instance per case.

    ``cors_origins`` is a PARAMETER, not a config read (S18 Q6/Q7): ``CORSMiddleware`` must be
    registered at construction time, and reaching for ``get_settings()`` here would validate the
    whole environment inside ``create_app()`` -- raising ``ValidationError`` with no ``.env``
    present and breaking AC1.

    No compression middleware is registered, deliberately (G3): gzip buffers the response and
    would silently defeat token-by-token streaming.
    """
    app = FastAPI(title="AlphaLens API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins) if cors_origins is not None else DEFAULT_CORS_ORIGINS,
        allow_credentials=False,  # D7: no auth, no cookies -- nothing to send credentials for
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness only (AC12) -- touches no DB, no LLM and no reranker.

        A health check that queries Neon turns a DB blip into a restart loop, and readiness
        semantics only matter once something is orchestrating the process (a v2 concern). This
        route deliberately does not read ``app.state``, so it answers before the lifespan has
        run and after it has torn down.
        """
        return {"status": "ok"}

    @app.post("/query")
    async def query(req: QueryRequest, request: Request) -> EventSourceResponse:
        """POST + SSE body (D5) -- `EventSource` is GET-only and cannot carry this request.

        Validation happens HERE, before the stream opens: an invalid body is a 422 from FastAPI
        and never reaches the generator (AC13). Once the first byte flushes the status is spent
        (G2), which is why every later failure is an in-band `error` event instead.
        """
        return EventSourceResponse(query_stream(request.app, req))

    return app
