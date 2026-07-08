"""AlphaLens v8 -- Agent nodes (S13).

The 5 LangGraph nodes (Plan -> Retrieve -> Rerank -> Evaluate -> Synthesize, L19)
as standalone ``(state, runtime) -> dict`` units. Each returns a PARTIAL AgentState
update that LangGraph merges per S12's reducers -- no node returns full state. Nodes
are decoupled from the graph (wiring is S16) so each is unit-testable by injecting a
fake ``AgentContext`` and fixture chunks.

Decisions (S13): DI via LangGraph Runtime static context (Choice B); Retrieve is a
contract-only stub whose fan-out body lands in S15 (Choice A); structured extraction
via ``with_structured_output(method="json_schema", strict=True)`` at temperature 0
(D3); Evaluate is two-signal with coverage precedence (D4); Synthesize streams free
text through the ``stream_synthesis`` seam that S14 wraps with the circuit breaker.

Guardrails: input-rail (ticker allowlist gate on Plan), output-rail (strict schema),
tool-call rail (parameterized SQL -- enforced in S15), honesty-rail (low-confidence +
unavailable-ticker surfacing on Synthesize).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from asyncpg import Pool
from langchain_groq import ChatGroq
from langgraph.runtime import Runtime
from sentence_transformers import CrossEncoder

from alphalens.agent.prompts import (
    EVALUATE_SYSTEM_PROMPT,
    SYNTHESIZE_SYSTEM_PROMPT,
    build_evaluate_user_msg,
    build_plan_system_prompt,
    build_plan_user_msg,
    build_synthesize_user_msg,
)
from alphalens.agent.state import (
    AgentState,
    Citation,
    EvalVerdict,
    QueryPlan,
    ScoredChunk,
)
from alphalens.config import get_settings


# ── DI container: static run-dependencies (Choice B -- Runtime static context) ──
@dataclass
class AgentContext:
    """Immutable per-run dependencies, injected via LangGraph Runtime context.

    Constructed ONCE at app cold-start; passed by reference to
    ``graph.invoke(..., context=ctx)``. Frozen by convention -- nodes read, never write.
    """

    llm: ChatGroq  # Groq gpt-oss-120b (D2)
    reranker: CrossEncoder  # ms-marco-MiniLM-L-6-v2, startup-loaded (L3)
    pool: Pool  # asyncpg pool (consumed by retrieve_node in S15)
    allowed_tickers: frozenset[str]  # DB-derived allowlist (companies table) -- input-rail gate


# Every node conforms to this shape -> S16 wiring is uniform.
Node = Callable[[AgentState, Runtime["AgentContext"]], Awaitable[dict[str, Any]]]


# ── Pure helper: input-rail (guardrail) ──────────────────────────────────────
def validate_tickers(requested: list[str], allowed: frozenset[str]) -> tuple[list[str], list[str]]:
    """Split planned tickers into (kept, dropped) against the corpus allowlist.

    The prompt only *guides* the LLM; this is the HARD gate (prompt-inject + validate).
    """
    kept = [t for t in requested if t in allowed]
    dropped = [t for t in requested if t not in allowed]
    return kept, dropped


# ── Pure helper: deterministic coverage-check (D4A) ──────────────────────────
def compute_coverage_gaps(plan: QueryPlan, reranked: list[ScoredChunk]) -> list[tuple[str, int]]:
    """(ticker, year) cells the query needs but retrieval did not surface.

    Years are DISCRETE (Gotcha 1): iterate ``plan.time_range.years`` directly, never
    ``range(min, max)`` -- that over-requires intermediate years and fabricates gaps.
    Cartesian false-low accepted in v1 (Gotcha 2): ``needed`` is the full ticker x year
    product, so asymmetric queries over-generate cells; per-sub-question pairing is v2.
    """
    needed = {(t, y) for t in plan.tickers for y in plan.time_range.years}
    present = {(sc.chunk.ticker, sc.chunk.period_year) for sc in reranked}
    return sorted(needed - present)


# ── Node 1: Plan -- LLM decompose + input-rail ───────────────────────────────
async def plan_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    structured = runtime.context.llm.with_structured_output(
        QueryPlan,
        method="json_schema",
        strict=True,  # D3: constrained decoding
    )
    messages = [
        # system prompt is FIXED (allowlist baked, identical every call) -> Groq caches it
        ("system", build_plan_system_prompt(runtime.context.allowed_tickers)),
        ("human", build_plan_user_msg(state["original_query"])),
    ]
    plan = cast(QueryPlan, await structured.ainvoke(messages))
    kept, dropped = validate_tickers(plan.tickers, runtime.context.allowed_tickers)
    plan.tickers = kept  # drop-and-note (partial-query policy)
    return {"query_plan": plan, "unavailable_tickers": dropped}


# ── Node 2: Retrieve -- CONTRACT ONLY (body → S15) ───────────────────────────
async def retrieve_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    """Per-cell fan-out (D5): for each needed (ticker, year) cell, run the hybrid
    HNSW-vector + tsvector-lexical query with that cell's WHERE filter concurrently
    (asyncio.gather on runtime.context.pool, <= pool max=5), RRF-merge, then enrich via a
    single chunks->filings join -> list[RetrievedChunk]. Appends to state['retrieved_chunks']
    (operator.add reducer, S12).

    INVARIANT: query embeddings MUST be jina-v3 (corpus is 100% jina-v3).
    Body lands in S15 -- this stub reserves the public surface (Choice A).
    """
    raise NotImplementedError("retrieve_node body → S15")


# ── Node 3: Rerank -- cross-encoder, in-process (L3) ─────────────────────────
async def rerank_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    chunks = state["retrieved_chunks"]
    if not chunks:
        return {"reranked_chunks": []}
    pairs = [(state["query"], c.text) for c in chunks]
    # CrossEncoder.predict is sync + CPU-bound -> offload so we don't block the event loop.
    # Wrapped in a lambda so mypy resolves predict's overload at the call site; pairs is
    # cast to Any because predict's typed input union rejects list[tuple[str, str]] (invariance).
    scores = await asyncio.to_thread(lambda: runtime.context.reranker.predict(cast(Any, pairs)))
    scored = [
        ScoredChunk(chunk=c, rerank_score=float(s)) for c, s in zip(chunks, scores, strict=True)
    ]
    scored.sort(key=lambda sc: sc.rerank_score, reverse=True)
    return {"reranked_chunks": scored[: get_settings().rerank_top_n]}


# ── Node 4: Evaluate -- two-signal, coverage precedence (D4) ──────────────────
async def evaluate_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    plan = state["query_plan"]
    reranked = state["reranked_chunks"]
    gaps = compute_coverage_gaps(plan, reranked)
    if gaps:  # structural signal wins (precedence) -- LLM call skipped (token saving)
        return {"confidence": "low", "confidence_reason": "coverage", "coverage_gaps": gaps}

    structured = runtime.context.llm.with_structured_output(
        EvalVerdict,
        method="json_schema",
        strict=True,  # D3
    )
    verdict = cast(
        EvalVerdict,
        await structured.ainvoke(
            [
                ("system", EVALUATE_SYSTEM_PROMPT),
                ("human", build_evaluate_user_msg(state["query"], reranked)),
            ]
        ),
    )
    if not verdict.sufficient:
        return {"confidence": "low", "confidence_reason": "llm", "coverage_gaps": []}
    return {"confidence": "high", "confidence_reason": "none", "coverage_gaps": []}


# ── Node 5: Synthesize -- Groq stream + citations + honesty-rail ──────────────
async def synthesize_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    citations = [
        Citation(
            chunk_id=sc.chunk.chunk_id,
            ticker=sc.chunk.ticker,
            filing_type=sc.chunk.filing_type,
            period_year=sc.chunk.period_year,
            section=sc.chunk.section,
        )
        for sc in state["reranked_chunks"]
    ]
    messages = [
        ("system", SYNTHESIZE_SYSTEM_PROMPT),
        (
            "human",
            build_synthesize_user_msg(
                query=state["query"],
                reranked=state["reranked_chunks"],
                confidence=state["confidence"],
                unavailable_tickers=state["unavailable_tickers"],
            ),
        ),
    ]
    stream = stream_synthesis(runtime.context.llm, messages)  # S14 wraps THIS seam
    return {"answer_stream": stream, "citations": citations}


# ── Groq streaming seam -- S14 circuit-breaker wraps this function ────────────
async def stream_synthesis(
    llm: ChatGroq, messages: list[tuple[str, str]]
) -> AsyncGenerator[str, None]:
    """Thin, wrappable streaming boundary. S14 replaces/decorates this to add the
    circuit breaker (OPEN -> degraded response: top reranked chunks, no LLM synthesis).

    Guard ``chunk.content`` -- some deltas carry empty/non-str content; yield only
    truthy string content so the SSE stream stays clean.
    """
    async for chunk in llm.astream(messages):
        content = chunk.content
        if isinstance(content, str) and content:
            yield content
