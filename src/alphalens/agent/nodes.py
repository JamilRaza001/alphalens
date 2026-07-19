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
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from asyncpg import Pool
from langchain_groq import ChatGroq
from langgraph.runtime import Runtime
from sentence_transformers import CrossEncoder

from alphalens.agent.circuit_breaker import SynthesisCircuitBreaker
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
    RetrievedChunk,
    ScoredChunk,
)
from alphalens.config import get_settings
from alphalens.etl.embeddings import EmbeddingClient  # S8 query embedder (jina-v3)

_log = logging.getLogger(__name__)


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
    breaker: SynthesisCircuitBreaker  # S14: guards the Groq synthesis stream, built at cold-start
    embedder: EmbeddingClient  # S15 (D4): jina-v3 query embeddings, cold-start built
    ticker_roster: Mapping[str, str]  # S16 (D3a): ticker->name, grounds Plan resolution
    # Plan year-rail bounds (config-derived at cold-start). Routed through the context, like
    # allowed_tickers/ticker_roster, so every Plan rail reads its gate set from one place.
    corpus_min_year: int
    corpus_max_year: int


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


def split_concatenated_years(requested: list[int]) -> list[int]:
    """Repair years the LLM concatenated into one integer: 20232024 -> [2023, 2024].

    The model has been observed emitting "2023 vs 2024" as the single int 20232024 even at
    temperature 0, under strict constrained decoding, and with an explicit negative example in
    the system prompt naming that exact wrong value. The prompt is only a nudge; this is the
    deterministic repair, and it is the load-bearing half.

    A value whose digit-length is a multiple of 4 and > 4 is split into successive 4-digit
    years. Anything else -- a normal 4-digit year, or genuinely malformed garbage whose length
    is not a multiple of 4 -- passes through untouched for ``validate_years`` to drop and flag.
    Runs UPSTREAM of that gate so a repaired, in-bounds year reaches retrieval normally.
    """
    repaired: list[int] = []
    for y in requested:
        digits = str(y)
        # isdigit() also rejects negatives (str() carries the sign), leaving them for the gate.
        if digits.isdigit() and len(digits) > 4 and len(digits) % 4 == 0:
            parts = [int(digits[i : i + 4]) for i in range(0, len(digits), 4)]
            _log.warning("Plan emitted concatenated year %d; repaired to %s", y, parts)
            repaired.extend(parts)
        else:
            repaired.append(y)
    # Order-preserving dedup: the repair can create duplicates ([2023, 20232024] -> 2023 twice),
    # and each dup year multiplies the retrieval fan-out. Mirrors retrieve_node's sub_question
    # dedup below.
    return list(dict.fromkeys(repaired))


def validate_years(
    requested: list[int], *, min_year: int, max_year: int
) -> tuple[list[int], list[int]]:
    """Split planned years into (kept, dropped) against the corpus year bounds.

    The twin of ``validate_tickers``: the prompt only *guides* the LLM; this is the HARD gate.
    Catches structured-decoding garbage -- notably the observed concatenation of "2023 vs 2024"
    into the single integer 20232024 -- before it reaches retrieval, where an implausible year
    matches zero rows and degrades into a false "no coverage" answer.
    """
    kept = [y for y in requested if min_year <= y <= max_year]
    dropped = [y for y in requested if not (min_year <= y <= max_year)]
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
    # temperature=0 pinned INVARIANTLY here (S16/D-temp) -- constrained/structured decoding
    # depends on it and must NOT track the config-driven synthesis temperature. model_copy sets
    # a real attribute on a real ChatGroq instance (no reliance on a call-kwarg propagating
    # through the with_structured_output RunnableSequence); the shared context.llm is untouched.
    deterministic = runtime.context.llm.model_copy(update={"temperature": 0.0})
    structured = deterministic.with_structured_output(
        QueryPlan,
        method="json_schema",
        strict=True,  # D3: constrained decoding
    )
    messages = [
        # system prompt is FIXED (roster baked, identical every call) -> Groq caches it
        (
            "system",
            build_plan_system_prompt(
                runtime.context.allowed_tickers, runtime.context.ticker_roster
            ),
        ),
        ("human", build_plan_user_msg(state["original_query"])),
    ]
    plan = cast(QueryPlan, await structured.ainvoke(messages))
    kept, dropped = validate_tickers(plan.tickers, runtime.context.allowed_tickers)
    plan.tickers = kept  # drop-and-note (partial-query policy)
    # Repair concatenated years BEFORE the bounds gate: a repaired, in-bounds year must flow to
    # retrieval normally rather than be dropped as garbage.
    repaired = split_concatenated_years(plan.time_range.years)
    kept_years, dropped_years = validate_years(
        repaired,
        min_year=runtime.context.corpus_min_year,
        max_year=runtime.context.corpus_max_year,
    )
    plan.time_range.years = kept_years  # drop-and-note -- same rail pattern as tickers
    return {
        "query_plan": plan,
        "unavailable_tickers": dropped,
        "unavailable_years": dropped_years,
    }


# ── Node 2: Retrieve -- per-cell fan-out hybrid retrieval (S15) ──────────────

# Parameterized per-cell hybrid query (S15). Every LLM-derived value is an asyncpg
# placeholder ($1..$8) -- NEVER interpolate (injection surface, tool-call rail).
#   $1 query_vector::vector | $2 ticker | $3 year | $4 k_vector
#   $5 sub_question (tsquery) | $6 k_lexical | $7 rrf_c | $8 n_per_cell
# `vec` ranks by HNSW cosine; `lex` ranks by lexical search via ts_rank_cd (NOT BM25 -- L1);
# `fused` combines them with Reciprocal Rank Fusion over a FULL OUTER JOIN so a chunk found
# by only ONE retriever is still scored. The final SELECT joins chunks->filings once, so each
# survivor is enriched (ticker/period_year/filing_type) in the same shot.
_HYBRID_CELL_SQL = """
WITH vec AS (
    SELECT c.chunk_id,
           ROW_NUMBER() OVER (ORDER BY c.embedding <=> $1::vector) AS rnk
    FROM chunks c
    JOIN filings f ON c.filing_id = f.filing_id
    WHERE f.ticker = $2
      AND EXTRACT(YEAR FROM f.period_end)::int = $3
      AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> $1::vector
    LIMIT $4
),
lex AS (
    SELECT c.chunk_id,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank_cd(c.tsv, plainto_tsquery('english', $5)) DESC
           ) AS rnk
    FROM chunks c
    JOIN filings f ON c.filing_id = f.filing_id
    WHERE f.ticker = $2
      AND EXTRACT(YEAR FROM f.period_end)::int = $3
      AND c.tsv @@ plainto_tsquery('english', $5)
    ORDER BY ts_rank_cd(c.tsv, plainto_tsquery('english', $5)) DESC
    LIMIT $6
),
fused AS (
    SELECT COALESCE(vec.chunk_id, lex.chunk_id) AS chunk_id,
           COALESCE(1.0 / ($7 + vec.rnk), 0.0)
         + COALESCE(1.0 / ($7 + lex.rnk), 0.0) AS rrf_score
    FROM vec
    FULL OUTER JOIN lex ON vec.chunk_id = lex.chunk_id
)
SELECT c.chunk_id::text                     AS chunk_id,
       c.text                               AS text,
       c.section                            AS section,
       f.ticker                             AS ticker,
       EXTRACT(YEAR FROM f.period_end)::int AS period_year,
       f.filing_type                        AS filing_type,
       c.metadata                           AS metadata
FROM fused
JOIN chunks  c ON c.chunk_id  = fused.chunk_id
JOIN filings f ON c.filing_id = f.filing_id
ORDER BY fused.rrf_score DESC
LIMIT $8
"""


# ── Pure helper: plan → retrieval cells (testable, no DB) ─────────────────────
def plan_to_cells(plan: QueryPlan) -> list[tuple[str, int, str]]:
    """Full (ticker, year, sub_question) product the query requires (D3).

    The (ticker, year) pair mirrors compute_coverage_gaps' `needed` (cartesian; false-low
    accepted in v1). Empty tickers / years / sub_questions ⇒ [] (nothing to retrieve; not
    an error).
    """
    return [
        (t, y, sq) for t in plan.tickers for y in plan.time_range.years for sq in plan.sub_questions
    ]


# ── Pure helper: merge + dedup across cells (testable, no DB) ─────────────────
def merge_dedup(cell_results: list[list[RetrievedChunk]]) -> list[RetrievedChunk]:
    """Flatten per-cell survivors; keep the first occurrence per chunk_id (a chunk can
    surface under two sub-questions of the same ticker/year). Order-stable so tests are
    deterministic.
    """
    seen: set[str] = set()
    merged: list[RetrievedChunk] = []
    for cell in cell_results:
        for rc in cell:
            if rc.chunk_id not in seen:
                seen.add(rc.chunk_id)
                merged.append(rc)
    return merged


# ── Pure helper: Reciprocal Rank Fusion reference (testable, no DB) ───────────
def rrf_fuse(vec_ranked: list[str], lex_ranked: list[str], *, c: int) -> list[tuple[str, float]]:
    """Fuse two rank-ordered chunk_id lists via RRF: score = 1/(c + rank) summed over both
    retrievers, missing side contributes 0. Returns (chunk_id, score) sorted score-descending.

    IMPORTANT: this MIRRORS the RRF formula in `_HYBRID_CELL_SQL` (the `fused` CTE) and must be
    kept in sync with it. Production per-cell fusion runs in SQL; this pure helper exists as the
    Python-testable reference (AC6) -- if the SQL formula changes, change this too, or they drift.
    """
    scores: dict[str, float] = {}
    for rank, chunk_id in enumerate(vec_ranked, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (c + rank)
    for rank, chunk_id in enumerate(lex_ranked, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (c + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


# ── DB helper: one hybrid+RRF query per cell (join enriches in-shot) ──────────
async def hybrid_search_cell(
    pool: Pool,
    *,
    query_vector: list[float],  # jina-v3, 768-d, for THIS sub_question
    sub_question: str,  # lexical search text (plainto_tsquery)
    ticker: str,
    year: int,
    k_vector: int,
    k_lexical: int,
    rrf_c: int,
    n_per_cell: int,
) -> list[RetrievedChunk]:
    """Run the parameterized hybrid query for one cell and return up to n_per_cell enriched
    RetrievedChunks, RRF-ordered. All inputs are asyncpg placeholders -- no interpolation.

    Lexical ranking is ts_rank_cd (NOT BM25 -- L1). The vector is bound as a list[float] and
    relies on the pgvector codec registered at pool init (init=register_pgvector, same as
    etl/runner.py); the SQL's $1::vector cast is belt-and-braces.
    """
    rows = await pool.fetch(
        _HYBRID_CELL_SQL,
        query_vector,
        ticker,
        year,
        k_vector,
        sub_question,
        k_lexical,
        rrf_c,
        n_per_cell,
    )
    return [
        RetrievedChunk(
            chunk_id=row["chunk_id"],
            text=row["text"],
            section=row["section"],  # nullable
            ticker=row["ticker"],
            period_year=row["period_year"],
            filing_type=row["filing_type"],
            metadata=_decode_metadata(row["metadata"]),
        )
        for row in rows
    ]


def _decode_metadata(raw: Any) -> dict[str, Any]:
    """asyncpg returns a jsonb column as a JSON string (no codec registered); decode to dict.
    Tolerates an already-decoded dict (if a jsonb codec is ever registered) and NULL.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        decoded: Any = json.loads(raw)
        return cast(dict[str, Any], decoded)
    return cast(dict[str, Any], raw)


async def retrieve_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    """Per-cell fan-out hybrid retrieval (D3): for each (ticker × year × sub_question) cell, run
    one hybrid HNSW-vector + ts_rank_cd-lexical query (RRF-fused) under a parameterized WHERE
    filter, concurrently (asyncio.gather on runtime.context.pool, <= pool max=5), enriched via a
    single chunks->filings join. Survivors merge with chunk_id dedup into a list[RetrievedChunk],
    appended to state['retrieved_chunks'] (operator.add reducer, S12). See spec S15.

    INVARIANT: query embeddings MUST be jina-v3 (corpus is 100% jina-v3) -- a nomic fallback at
    query time is a hard, loud failure, never a silent wrong-vector-space search.
    """
    ctx = runtime.context
    cfg = get_settings()
    plan = state["query_plan"]

    cells = plan_to_cells(plan)
    if not cells:  # empty tickers/years/sub_questions -> nothing to retrieve, DB untouched
        return {"retrieved_chunks": []}

    # Embed each UNIQUE sub_question once (jina-v3), reuse across its (ticker, year) combos.
    unique_subqs = list(dict.fromkeys(plan.sub_questions))  # order-preserving dedup
    embeds = await asyncio.gather(*(ctx.embedder.embed_query(sq) for sq in unique_subqs))
    vec_by_subq: dict[str, list[float]] = {}
    for sq, res in zip(unique_subqs, embeds, strict=True):
        if res.model_version != "jina-v3":  # INVARIANT -- loud, not silent
            raise RuntimeError(
                f"query embedding fell back to {res.model_version!r}; corpus is jina-v3. "
                "Refusing to retrieve in a mismatched vector space."
            )
        vec_by_subq[sq] = res.vectors[0]

    # Fan out: one hybrid query per cell, concurrency bounded by the asyncpg pool (max 5).
    cell_results = await asyncio.gather(
        *(
            hybrid_search_cell(
                ctx.pool,
                query_vector=vec_by_subq[sq],
                sub_question=sq,
                ticker=t,
                year=y,
                k_vector=cfg.retrieval_k_vector,
                k_lexical=cfg.retrieval_k_lexical,
                rrf_c=cfg.retrieval_rrf_c,
                n_per_cell=cfg.retrieval_n_per_cell,
            )
            for (t, y, sq) in cells
        )
    )

    merged = merge_dedup(cell_results)
    return {"retrieved_chunks": merged}


# ── Pure helper: cell-aware keep-selection (S17, testable, no I/O) ───────────
def select_with_floor(
    scored: list[ScoredChunk],
    *,
    floor_per_pair: int,
    max_context_chunks: int,
) -> tuple[list[ScoredChunk], list[tuple[str, int]]]:
    """Cell-aware keep-selection replacing the global sort-and-slice (S17, Lever #1).

    Groups the reranked survivors by their surviving ``(ticker, period_year)`` pair (cell
    provenance is already discarded by ``merge_dedup`` -- G11: only these two flat fields are
    used, no re-thread), guarantees each non-empty pair a floor of chunks from its OWN
    top-scored candidates, fills the remaining budget by global rerank score (merit preserved),
    and caps the total at ``max_context_chunks``. Degrades honestly when breadth overflows the
    cap: reduce depth before dropping coverage, and report any pair dropped for capacity.

    Returns ``(selected, dropped_for_capacity)``:
      selected              -> chunks to pass downstream, ``len <= max_context_chunks``
      dropped_for_capacity  -> (ticker, year) pairs that HAD candidates but got 0 slots
                               (hard-overflow only; ``[]`` otherwise)

    Pure and deterministic: ties break stably by rerank score desc, then ``chunk_id``.
    """
    cap = max_context_chunks
    if not scored:
        return [], []

    # 1. Group by (ticker, period_year) -- the selection grain (never the retrieval cell).
    groups: dict[tuple[str, int], list[ScoredChunk]] = {}
    for sc in scored:
        groups.setdefault((sc.chunk.ticker, sc.chunk.period_year), []).append(sc)

    # 2. Sort each pair by score desc, stable tie-break on chunk_id.
    for pair_chunks in groups.values():
        pair_chunks.sort(key=lambda sc: (-sc.rerank_score, sc.chunk.chunk_id))

    n_pairs = len(groups)

    # 4. Pick effective_floor + the set of kept pairs.
    dropped: list[tuple[str, int]] = []
    if n_pairs * floor_per_pair <= cap:  # (a) normal: every pair gets the full floor
        effective_floor = floor_per_pair
        kept_pairs = set(groups)
    elif n_pairs <= cap:  # (b) GRADUATED: reduce depth, keep all coverage
        effective_floor = max(1, cap // n_pairs)
        kept_pairs = set(groups)
    else:  # (c) HARD OVERFLOW: coverage must be cut -- keep the strongest `cap` pairs
        effective_floor = 1
        ranked = sorted(
            groups.items(),
            key=lambda kv: (-kv[1][0].rerank_score, kv[1][0].chunk.chunk_id),
        )
        kept_pairs = {key for key, _ in ranked[:cap]}
        dropped = sorted(key for key, _ in ranked[cap:])

    # 5. Reserve each kept pair's floor (min(floor, len) via slice = empty-cell reclaim).
    reserved: list[ScoredChunk] = []
    reserved_ids: set[str] = set()
    for key, pair_chunks in groups.items():
        if key not in kept_pairs:
            continue
        for sc in pair_chunks[:effective_floor]:
            reserved.append(sc)
            reserved_ids.add(sc.chunk.chunk_id)

    # 6-7. Fill the freed/remaining budget by global score desc from kept, non-reserved chunks.
    remaining = cap - len(reserved)
    fill: list[ScoredChunk] = []
    if remaining > 0:
        fill_pool = [
            sc
            for sc in scored
            if sc.chunk.chunk_id not in reserved_ids
            and (sc.chunk.ticker, sc.chunk.period_year) in kept_pairs
        ]
        fill_pool.sort(key=lambda sc: (-sc.rerank_score, sc.chunk.chunk_id))
        fill = fill_pool[:remaining]

    # 8. Final context order: best evidence first (stable).
    selected = reserved + fill
    selected.sort(key=lambda sc: (-sc.rerank_score, sc.chunk.chunk_id))
    return selected, dropped


# ── Node 3: Rerank -- cross-encoder, in-process (L3) ─────────────────────────
async def rerank_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    chunks = state["retrieved_chunks"]
    if not chunks:
        return {"reranked_chunks": [], "dropped_for_capacity": []}
    pairs = [(state["query"], c.text) for c in chunks]
    # CrossEncoder.predict is sync + CPU-bound -> offload so we don't block the event loop.
    # Wrapped in a lambda so mypy resolves predict's overload at the call site; pairs is
    # cast to Any because predict's typed input union rejects list[tuple[str, str]] (invariance).
    # SCORING is against state["query"] (the FULL query) -- untouched, Lever #2 owns this (S17/AC9).
    scores = await asyncio.to_thread(lambda: runtime.context.reranker.predict(cast(Any, pairs)))
    scored = [
        ScoredChunk(chunk=c, rerank_score=float(s)) for c, s in zip(chunks, scores, strict=True)
    ]
    scored.sort(key=lambda sc: sc.rerank_score, reverse=True)
    # S17: cell-aware per-pair floor replaces the global `scored[:rerank_top_n]` slice.
    cfg = get_settings()
    selected, dropped = select_with_floor(
        scored,
        floor_per_pair=cfg.floor_per_pair,
        max_context_chunks=cfg.max_context_chunks,
    )
    return {"reranked_chunks": selected, "dropped_for_capacity": dropped}


# ── Node 4: Evaluate -- two-signal, coverage precedence (D4) ──────────────────
async def evaluate_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    plan = state["query_plan"]
    reranked = state["reranked_chunks"]
    gaps = compute_coverage_gaps(plan, reranked)
    if gaps:  # structural signal wins (precedence) -- LLM call skipped (token saving)
        return {"confidence": "low", "confidence_reason": "coverage", "coverage_gaps": gaps}

    # temperature=0 pinned INVARIANTLY (S16/D-temp), same rationale as plan_node.
    deterministic = runtime.context.llm.model_copy(update={"temperature": 0.0})
    structured = deterministic.with_structured_output(
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
                unavailable_years=state["unavailable_years"],
                dropped_for_capacity=state["dropped_for_capacity"],
            ),
        ),
    ]
    ctx = runtime.context

    # Real LLM stream (the S13 seam), deferred so the breaker gates it lazily at drain time.
    def protected() -> AsyncGenerator[str, None]:
        return stream_synthesis(ctx.llm, messages)

    # Degraded stream: top reranked SEC chunks, deterministic, NO LLM (honesty rail).
    def fallback() -> AsyncGenerator[str, None]:
        return degraded_stream(state["reranked_chunks"])

    # S14: route synthesis through the circuit breaker (OPEN -> serve `fallback`).
    return {"answer_stream": ctx.breaker.stream(protected, fallback), "citations": citations}


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


# ── Degraded (no-LLM) fallback -- served when the S14 breaker is OPEN ──────────
async def degraded_stream(chunks: list[ScoredChunk]) -> AsyncGenerator[str, None]:
    """No-LLM fallback: stream the top reranked chunk texts verbatim with source tags.

    Served when the circuit breaker is OPEN (Groq outage). This is a REAL answer -- the
    honesty rail -- not an error: surface the retrieved evidence, never fabricate synthesis.
    Lives HERE (not circuit_breaker.py) so the breaker stays AgentState-agnostic.
    """
    yield (
        "_Synthesis is temporarily unavailable; showing the most relevant source "
        "excerpts directly._\n"
    )
    if not chunks:
        yield "\nNo relevant filing excerpts were retrieved for this query.\n"
        return
    for sc in chunks:
        c = sc.chunk
        yield f"\n[{c.ticker} {c.filing_type} {c.period_year} · {c.section}]\n{c.text}\n"
