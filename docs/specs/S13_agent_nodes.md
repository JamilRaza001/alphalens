# Spec S13 — Agent Nodes (`agent_nodes`)

> Spec **S13** (agent_nodes) · v8 cross-ref: §7.2 (Node Responsibilities), §5.2 (Agent Loop) · targets:
> `src/alphalens/agent/nodes.py` + `src/alphalens/agent/prompts.py`
> Format: L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).
> Consumes the S12 state contract (`state.py`) verbatim; **no graph wiring here** (→ S16).

**Decisions applied (locked in Session 26 + this authoring session):**

1. **D1 — Scope.** `nodes.py` = 5 node functions + 2 pure helpers; `prompts.py` = fixed system prompts + user-msg builders. **Graph wiring (`StateGraph`/`add_node`/`.compile()`) deferred to S16.**
2. **Choice A — Retrieve is contract-only.** `retrieve_node` gets a full signature + docstring contract; body is `raise NotImplementedError` — the per-cell fan-out implementation lands in **S15**. Freezes `nodes.py`'s public surface now so S16 imports never hit a missing node.
3. **Choice B — DI via LangGraph Runtime static context** (framework-native, LangGraph 1.0). Nodes take `(state, runtime: Runtime[AgentContext])`; deps (`llm`, `reranker`, `pool`, `allowed_tickers`) are built **once at cold-start** and injected by reference. No factory closures, no module-level singletons for the injected deps.
4. **D2 — LLM = Groq `openai/gpt-oss-120b`** (config-driven `settings.groq_model`).
5. **D3 — Structured output = `with_structured_output(method="json_schema", strict=True)`, `temperature=0`** for the two typed-extraction nodes (Plan → `QueryPlan`, Evaluate → `EvalVerdict`). Synthesize streams free text (no schema).
6. **D4 — Evaluate = two-signal** (deterministic coverage-check + LLM sufficiency), **coverage precedence**. All three `confidence_reason` branches live in v1.
7. **D5 — Retrieval = per-cell fan-out**, implemented in S15 (see Choice A stub).
8. **D6 — HyDE not used.**
9. **Guardrails (4-rail model, proportionate subset):** input-rail = ticker allowlist-validate on Plan (drop-and-note for partial queries); output-rail = strict schema (D3); tool-call rail = parameterized SQL (S15 gotcha); honesty-rail = low-confidence + unavailable-ticker surfacing on Synthesize.

**State-schema piggybacks (small `state.py` / `config.py` additions applied alongside S13):**

- `EvalVerdict(BaseModel)` — Evaluate's structured-output boundary type → add to `state.py` beside `QueryPlan`.
- `AgentState["unavailable_tickers"]: list[str]` — out-of-corpus tickers dropped by the input-rail; surfaced by Synthesize. Defaults to `[]`.
- `Settings.rerank_top_n: int` — config-driven top-N for the Rerank node (eval-tunable).
- *(Optional, D3)* add `Field(description=...)` to `QueryPlan` fields to improve extraction.

---

### Goal

Implement the 5 LangGraph nodes (`Plan → Retrieve → Rerank → Evaluate → Synthesize`) as standalone
`(state, runtime) -> dict` units, each returning a **partial** `AgentState` update that LangGraph will
merge (per S12's reducers). Nodes are decoupled from the graph so each is unit-testable in isolation by
injecting a fake `AgentContext` and fixture chunks. Retrieve is a deliberate contract-only stub (S15).
Prompts live in a separate `prompts.py` so system prompts stay fixed (Groq prompt-caching) and prompt
iteration is decoupled from node logic.

---

### Function Signatures

```python
# ── src/alphalens/agent/nodes.py ───────────────────────────────────────────────
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Awaitable, Callable

from asyncpg import Pool
from langchain_groq import ChatGroq
from langgraph.runtime import Runtime
from sentence_transformers import CrossEncoder

from alphalens.config import get_settings
from alphalens.agent.state import (
    AgentState, QueryPlan, EvalVerdict, RetrievedChunk, ScoredChunk, Citation,
)
from alphalens.agent.prompts import (
    build_plan_system_prompt, build_plan_user_msg,
    EVALUATE_SYSTEM_PROMPT, build_evaluate_user_msg,
    SYNTHESIZE_SYSTEM_PROMPT, build_synthesize_user_msg,
)


# ── DI container: static run-dependencies (Choice B — Runtime static context) ──
@dataclass
class AgentContext:
    """Immutable per-run dependencies, injected via LangGraph Runtime context.
    Constructed ONCE at app cold-start; passed by reference to graph.invoke(..., context=ctx)."""
    # As of S13 — this list GROWS in later specs. Cumulative live shape (9 fields, as of S16):
    # + breaker (S14), + embedder (S15), + ticker_roster / corpus_min_year / corpus_max_year (S16).
    # nodes.py is ground truth; see docs/specs/S16_graph_wiring.md for the current list.
    llm: ChatGroq                    # Groq gpt-oss-120b (D2)
    reranker: CrossEncoder           # ms-marco-MiniLM-L-6-v2, startup-loaded (L3)
    pool: Pool                       # asyncpg pool (consumed by retrieve_node in S15)
    allowed_tickers: frozenset[str]  # DB-derived allowlist (companies table) — input-rail gate


# Every node conforms to this shape → S16 wiring is uniform.
Node = Callable[[AgentState, Runtime["AgentContext"]], Awaitable[dict[str, Any]]]


# ── Pure helper: input-rail (guardrail) ──────────────────────────────────────
def validate_tickers(
    requested: list[str], allowed: frozenset[str]
) -> tuple[list[str], list[str]]:
    """Split planned tickers into (kept, dropped) against the corpus allowlist.
    The prompt only *guides* the LLM; this is the HARD gate (prompt-inject + validate)."""
    kept    = [t for t in requested if t in allowed]
    dropped = [t for t in requested if t not in allowed]
    return kept, dropped


# ── Pure helper: deterministic coverage-check (D4A) ──────────────────────────
def compute_coverage_gaps(
    plan: QueryPlan, reranked: list[ScoredChunk]
) -> list[tuple[str, int]]:
    """(ticker, year) cells the query needs but retrieval did not surface.
    Years are DISCRETE (Gotcha 1); cartesian false-low accepted in v1 (Gotcha 2)."""
    needed  = {(t, y) for t in plan.tickers for y in plan.time_range.years}
    present = {(sc.chunk.ticker, sc.chunk.period_year) for sc in reranked}
    return sorted(needed - present)


# ── Node 1: Plan — LLM decompose + input-rail ────────────────────────────────
async def plan_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    structured = runtime.context.llm.with_structured_output(
        QueryPlan, method="json_schema", strict=True          # D3: constrained decoding
    )
    messages = [
        # system prompt is FIXED (roster baked, identical every call) → Groq caches it
        # S16/D3a: now also passes runtime.context.ticker_roster (grounded resolution)
        ("system", build_plan_system_prompt(
            runtime.context.allowed_tickers, runtime.context.ticker_roster
        )),
        ("human",  build_plan_user_msg(state["original_query"])),
    ]
    plan: QueryPlan = await structured.ainvoke(messages)
    kept, dropped = validate_tickers(plan.tickers, runtime.context.allowed_tickers)
    plan.tickers = kept                                        # drop-and-note (partial-query policy)
    return {"query_plan": plan, "unavailable_tickers": dropped}


# ── Node 2: Retrieve — CONTRACT ONLY (body → S15) ────────────────────────────
async def retrieve_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    """Per-cell fan-out (D5): for each needed (ticker, year) cell, run the hybrid
    HNSW-vector + tsvector-lexical query with that cell's WHERE filter concurrently
    (asyncio.gather on runtime.context.pool, ≤ pool max=5), RRF-merge, then enrich via a
    single chunks→filings join → list[RetrievedChunk]. Appends to state['retrieved_chunks']
    (operator.add reducer, S12).
    INVARIANT: query embeddings MUST be jina-v3 (corpus is 100% jina-v3).
    Body lands in S15 — this stub reserves the public surface (Choice A)."""
    raise NotImplementedError("retrieve_node body → S15")


# ── Node 3: Rerank — cross-encoder, in-process (L3) ──────────────────────────
async def rerank_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    chunks = state["retrieved_chunks"]
    if not chunks:
        return {"reranked_chunks": []}
    pairs  = [(state["query"], c.text) for c in chunks]
    # CrossEncoder.predict is sync + CPU-bound → offload so we don't block the event loop
    scores = await asyncio.to_thread(runtime.context.reranker.predict, pairs)
    scored = [ScoredChunk(chunk=c, rerank_score=float(s)) for c, s in zip(chunks, scores)]
    scored.sort(key=lambda sc: sc.rerank_score, reverse=True)
    return {"reranked_chunks": scored[: get_settings().rerank_top_n]}


# ── Node 4: Evaluate — two-signal, coverage precedence (D4) ───────────────────
async def evaluate_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    plan     = state["query_plan"]
    reranked = state["reranked_chunks"]
    gaps     = compute_coverage_gaps(plan, reranked)
    if gaps:                                                   # structural signal wins (precedence)
        return {"confidence": "low", "confidence_reason": "coverage", "coverage_gaps": gaps}

    structured = runtime.context.llm.with_structured_output(
        EvalVerdict, method="json_schema", strict=True         # D3
    )
    verdict: EvalVerdict = await structured.ainvoke([
        ("system", EVALUATE_SYSTEM_PROMPT),
        ("human",  build_evaluate_user_msg(state["query"], reranked)),
    ])
    if not verdict.sufficient:
        return {"confidence": "low", "confidence_reason": "llm", "coverage_gaps": []}
    return {"confidence": "high", "confidence_reason": "none", "coverage_gaps": []}


# ── Node 5: Synthesize — Groq stream + citations + honesty-rail ───────────────
async def synthesize_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    citations = [
        Citation(
            chunk_id=sc.chunk.chunk_id, ticker=sc.chunk.ticker,
            filing_type=sc.chunk.filing_type, period_year=sc.chunk.period_year,
            section=sc.chunk.section,
        )
        for sc in state["reranked_chunks"]
    ]
    messages = [
        ("system", SYNTHESIZE_SYSTEM_PROMPT),
        ("human",  build_synthesize_user_msg(
            query=state["query"],
            reranked=state["reranked_chunks"],
            confidence=state["confidence"],
            unavailable_tickers=state["unavailable_tickers"],
        )),
    ]
    stream = stream_synthesis(runtime.context.llm, messages)   # S14 wraps THIS seam
    return {"answer_stream": stream, "citations": citations}


# ── Groq streaming seam — S14 circuit-breaker wraps this function ─────────────
async def stream_synthesis(
    llm: ChatGroq, messages: list[tuple[str, str]]
) -> AsyncGenerator[str, None]:
    """Thin, wrappable streaming boundary. S14 replaces/decorates this to add the
    circuit breaker (OPEN → degraded response: top reranked chunks, no LLM synthesis)."""
    async for chunk in llm.astream(messages):
        if chunk.content:
            yield chunk.content
```

```python
# ── src/alphalens/agent/prompts.py ─────────────────────────────────────────────
from alphalens.agent.state import ScoredChunk

# System prompts are MODULE-LEVEL CONSTANTS (fixed strings) → cacheable by Groq.
# Keep them stable; iterate deliberately (prompt diffs are the v1→v2 quality lever).

# SUPERSEDED BY S16/D3a — now takes the ticker roster too; see docs/specs/S16_graph_wiring.md.
def build_plan_system_prompt(
    allowed_tickers: frozenset[str], ticker_roster: Mapping[str, str]
) -> str:
    """Fixed instructions + the (static, v1) corpus roster baked in → identical every call
    → cache-friendly. The roster (ticker→name, rendered ticker-sorted for stability) GUIDES the
    LLM and grounds word→ticker resolution; validate_tickers is still the hard gate."""
    ...

def build_plan_user_msg(query: str) -> str: ...

EVALUATE_SYSTEM_PROMPT: str = ...          # instructs binary sufficiency self-assessment → EvalVerdict
def build_evaluate_user_msg(query: str, reranked: list[ScoredChunk]) -> str: ...

SYNTHESIZE_SYSTEM_PROMPT: str = ...        # answer + inline citations; surface low-confidence flag
def build_synthesize_user_msg(
    query: str, reranked: list[ScoredChunk], confidence: str, unavailable_tickers: list[str],
) -> str: ...
```

---

### Acceptance Criteria

1. **DI shape.** Every node has signature `async def name(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]`; no node reads a module-level client/model/pool. `AgentContext` is a frozen-by-convention dataclass holding `llm`, `reranker`, `pool`, `allowed_tickers`.
2. **Partial-update contract.** Each node returns **only** the keys it changes; constructing that dict type-checks under **mypy --strict**. No node returns the full state.
3. **Plan.** Returns `query_plan` (validated `QueryPlan`) and `unavailable_tickers`. Every ticker in `query_plan.tickers` is a member of `allowed_tickers`; any requested ticker outside the allowlist appears in `unavailable_tickers`, not in the plan (drop-and-note).
4. **Plan output schema.** `QueryPlan` is produced via `strict=True, method="json_schema"`; `temperature=0`. Output is schema-valid by construction (not post-hoc validated).
5. **Retrieve (stub).** `retrieve_node` exists with the documented signature + contract docstring and raises `NotImplementedError`. It is **not** invoked by any S13 test that expects a value; the jina-v3 invariant is stated in the docstring.
6. **Rerank.** Consumes `state["retrieved_chunks"]`, emits `reranked_chunks` = top-`settings.rerank_top_n` `ScoredChunk`s sorted by `rerank_score` desc. Empty input → `[]`. The cross-encoder call runs off the event loop (`asyncio.to_thread`). `reranked_chunks` has no reducer → it **replaces** (S12 criterion 3).
7. **Evaluate — coverage precedence.** `compute_coverage_gaps` non-empty ⇒ `{"low","coverage", gaps}` **without** an LLM call. Empty gaps + `EvalVerdict.sufficient is False` ⇒ `{"low","llm", []}`. Empty gaps + sufficient ⇒ `{"high","none", []}`. (Mirrors S12 criterion 5.)
8. **Evaluate — discrete years.** `compute_coverage_gaps` treats `time_range.years` as a discrete set (a `[2022, 2024]` plan never reports 2023 as a gap).
9. **Synthesize.** Returns `answer_stream` (async generator of `str`) and `citations` (one `Citation` per reranked chunk, fields copied 1:1). The Groq call is made **only** through `stream_synthesis` (the S14 seam), never inlined.
10. **Prompts decoupled.** All prompt text lives in `prompts.py`; `nodes.py` imports builders/constants. System prompts are fixed strings (no per-call interpolation of variable content into the system role).
11. **Unit-testability.** Each of Plan / Rerank / Evaluate / Synthesize is exercised with a fake `AgentContext` (fake `llm`, fake `reranker`) and hand-built fixture `state` — no live Groq/DB/graph. Evaluate's three branches and the discrete-year gotcha have explicit tests.

---

### Gotchas

- **`Runtime` construction in tests — pin at implementation.** `Runtime` is normally injected by the graph executor (absent in S13). For standalone unit tests, build a minimal `Runtime[AgentContext]` (or the smallest shim its constructor allows) around a fake `AgentContext`. The exact constructor differs by `langgraph` version — **verify against the installed version at implementation time**; do not hardcode from memory.
- **`retrieve_node` is a deliberate gap, not an oversight.** The `NotImplementedError` is an honest guard: any integration path that reaches Retrieve before S15 fails loudly instead of silently passing stub/empty data (which would fake a coverage gap). Do **not** write an end-to-end integration test until S15 lands real retrieval and S16 wires the graph.
- **Coverage helper — two known traps.** (1) *Discrete years*: iterate `time_range.years` directly; never `range(min, max)` — that over-requires intermediate years and fabricates gaps. (2) *Cartesian false-low* (accepted v1): `needed` is a full ticker×year product, so asymmetric queries ("AAPL 2022 vs MSFT 2024") over-generate cells and may under-claim. Fixing this needs per-sub-question `(ticker,year)` pairing → **v2**.
- **Reranker: load once, score off-thread.** The `CrossEncoder` is built at cold-start (L3, ~80MB) and lives on `AgentContext` — never re-instantiate inside the node. `.predict` is synchronous/CPU-bound; `asyncio.to_thread` keeps the event loop free (matters once fan-out makes the pipeline concurrent).
- **Tool-call rail belongs to S15, but lock the rule now.** Retrieve's `WHERE` filters use LLM-derived `ticker`/`year`. In S15 these MUST go through **parameterized queries** (asyncpg `$1, $2` placeholders) — never f-string/`.format` into SQL. This is the tool-call guardrail; string interpolation here is an injection surface.
- **Prompt caching depends on byte-identical system prompts.** `build_plan_system_prompt` bakes the (static v1) corpus roster (S16/D3a — it was the bare allowlist as of S13), so it must return the *same* string every call to hit Groq's cache. The roster is rendered ticker-sorted precisely to keep that string stable. If the `companies` seed ever changes, the roster (and prompt) regenerate once at the next cold-start — expected. Keep variable content (the user query, the chunks) in the `human` turn only — as of S16 the roster is the *only* interpolation in the Plan template.
- **`strict=True` schema constraints.** Constrained decoding requires `additionalProperties: false` + all properties required. Pydantic v2 + LangChain generate this, **but verify the nested `TimeRange` inside `QueryPlan`** serializes correctly (nested models are the usual failure point). Same check for `EvalVerdict`.
- **Three LLM calls per query now.** Plan + Evaluate + Synthesize all hit Groq (D4 kept LLM sufficiency in v1). Budget accordingly (~30–45 full agent-queries/day on free tier). Evaluate's LLM call is **skipped** whenever coverage gaps exist (precedence) — a natural token saving on the failing path.
- **`astream` chunk shape.** Guard `chunk.content` (can be empty/`None` on some deltas); yield only truthy content so the SSE stream stays clean.
- **`unavailable_tickers` vs `coverage_gaps` are different failure classes.** `unavailable_tickers` = requested company not in the 10-company corpus at all (input-rail, pre-retrieval). `coverage_gaps` = an in-corpus `(ticker, year)` cell that retrieval missed (post-rerank). Keep them distinct in state and in the synthesized answer's wording.
