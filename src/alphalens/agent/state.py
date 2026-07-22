"""AlphaLens v8 -- Agent State (S12).

`AgentState` is the single in-memory contract threaded through all 5 LangGraph
nodes (Plan -> Retrieve+Filter -> Rerank -> Evaluate -> Synthesize, L19). Every
node reads from it and returns a partial update that LangGraph merges. This
module defines ONLY the state shape and its supporting boundary types -- there
is no node logic here (that is S13/S15).

Locked decisions encoded:
  - L5  retrieved_chunks accumulates across iterations via an operator.add reducer.
  - L6  no Refine node in v1; query == original_query and iteration stays 0.
  - L7  original_query / request_id / user_id are write-once anchors.
  - L19 single-pass 5-node graph.

Spec-local decisions (supersede v8 §7.1 where noted):
  - D1 confidence is a Literal["low", "high"] label, not a float.
  - D2 tickers / intent / entities live ONLY inside query_plan -- no top-level copies.
  - D3 confidence_reason records which signal drove the verdict (coverage/llm/none).
  - D4 RetrievedChunk is a fresh agent-side type; the ETL Chunk is ingestion-side.

NOTE: the ETL Chunk (src/alphalens/etl/chunker.py) is a frozen ingestion
value-object with no chunk_id/ticker/year -- it is deliberately NOT imported or
subclassed here (D4). RetrievedChunk is DB-derived and join-enriched instead.
"""

import operator
from collections.abc import AsyncGenerator  # not typing.AsyncGenerator (UP035)
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

# Query intent classes emitted by the Plan node.
Intent = Literal["comparative", "temporal", "factual", "qualitative"]


class TimeRange(BaseModel):
    """Discrete reporting years the query targets -- NOT a continuous span."""

    # "2022 vs 2024" -> [2022, 2024]; 2023 intentionally absent. A naive
    # range(start, end) would over-require 2023 and create false coverage gaps.
    # The "never concatenate" clause is not hypothetical: the model was observed emitting
    # [20232024] for "2023 vs 2024", which matches zero filings. nodes.validate_years is the
    # hard gate; this description is only the soft guide.
    years: list[int] = Field(
        description=(
            "Discrete list of the exact reporting years named in the query, as separate "
            "4-digit integers -- ONE list entry per year. E.g. '2023 vs 2024' -> [2023, 2024]. "
            "NEVER concatenate two years into a single number: [20232024] is WRONG. "
            "Do NOT fill in intermediate years: '2022 vs 2024' -> [2022, 2024], not "
            "[2022, 2023, 2024]."
        )
    )


class QueryPlan(BaseModel):
    """Structured output of the Plan node (LLM boundary -> validated here).

    Sole home of tickers / intent / entities (D2) -- AgentState carries no
    top-level copies of these fields.
    """

    tickers: list[str] = Field(
        description="Uppercase stock ticker symbols the query is about, e.g. ['AAPL', 'MSFT']."
    )
    intent: Intent = Field(
        description="Single query-intent class: comparative, temporal, factual, or qualitative."
    )
    time_range: TimeRange = Field(description="Discrete reporting years the query targets.")
    sub_questions: list[str] = Field(
        description=(
            "The distinct METRICS or TOPICS the query asks about -- the 'what' only, one entry "
            "per reporting concept (e.g. 'research and development spending'). NEVER bake a "
            "company name or a year into a sub-question: the tickers and time_range.years fields "
            "carry those, and each topic is cross-joined against every (ticker, year) cell. "
            "'Compare Apple and Microsoft R&D spend 2023 vs 2024' -> ['research and development "
            "spending'], NOT one entry per company or per year."
        )
    )
    entities: list[str] = Field(
        description="Salient non-ticker entities (people, products, segments, metrics) named in the query."
    )


class EvalVerdict(BaseModel):
    """Structured output of the Evaluate node's LLM sufficiency signal (D4).

    Consumed only when the deterministic coverage check found no gaps. Field
    order is deliberate: `reasoning` FIRST gives strict constrained decoding a
    chain-of-thought lift before it commits to the boolean. The node uses ONLY
    `.sufficient` for control flow; `reasoning` is captured for Opik tracing.
    """

    reasoning: str = Field(
        description="Brief justification for the sufficiency judgment (chain-of-thought, logged for tracing)."
    )
    sufficient: bool = Field(description="True iff the reranked evidence fully answers the query.")


class RetrievedChunk(BaseModel):
    """One piece of evidence pulled from the DB and enriched via a single
    chunks -> filings join. Built by the Retrieve node (S15). Agent-side,
    query-time -- distinct from the ingestion-side ETL Chunk (D4).
    """

    chunk_id: str  # chunks.chunk_id (uuid, carried as str -- never int)
    text: str  # chunks.text
    section: str | None  # chunks.section (NULLABLE in DB)
    ticker: str  # filings.ticker (single join; NOT via companies)
    period_year: int  # EXTRACT(YEAR FROM filings.period_end)
    filing_type: str  # filings.filing_type -> '10-K' | '10-Q'
    # Passthrough of chunks.metadata when useful. default_factory=dict gives a
    # fresh dict per instance (self-documenting; Pydantic v2 copies defaults too).
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScoredChunk(BaseModel):
    """A RetrievedChunk paired with its cross-encoder rerank score."""

    chunk: RetrievedChunk
    rerank_score: float


class Citation(BaseModel):
    """A source reference surfaced in the final answer (derived from a RetrievedChunk)."""

    chunk_id: str  # uuid
    ticker: str
    filing_type: str  # '10-K' | '10-Q'
    period_year: int
    section: str | None


class AgentState(TypedDict):
    """In-memory contract merged across the 5-node single-pass graph (L19).

    Plain TypedDict -- all keys required. Nodes fill the state incrementally via
    LangGraph partial-update merges, which works with all-required keys; there
    are no optional-key semantics in v1, so no total=False.
    """

    # -- Anchors: set once at intake, NEVER mutated by any node (L7) --
    original_query: str
    request_id: str
    user_id: str | None

    # -- Plan: set once by Node 1; sole home of tickers/intent/entities (D2) --
    query_plan: QueryPlan
    # Requested tickers dropped by the Plan input-rail because they are not in the
    # 10-company corpus at all (pre-retrieval). Distinct from coverage_gaps, which
    # are in-corpus (ticker, year) cells that retrieval MISSED (post-rerank).
    # No reducer -- replaced. Surfaced by Synthesize's honesty-rail.
    unavailable_tickers: list[str]
    # Requested years dropped by the Plan year-rail because they are not plausible corpus
    # years -- e.g. the LLM concatenating "2023 vs 2024" into 20232024. Pre-retrieval, the
    # exact twin of unavailable_tickers; distinct from coverage_gaps, which are in-corpus
    # (ticker, year) cells that retrieval MISSED. No reducer -- replaced. Surfaced by
    # Synthesize's honesty-rail.
    unavailable_years: list[int]

    # -- Mutable per pass --
    query: str  # == original_query in v1 (v3 Refine rewrites it -- L6)
    iteration: int  # always 0 in v1; increments only in v3 retry loop

    # -- Accumulated: operator.add reducer (L5, forward-compatible with v3) --
    retrieved_chunks: Annotated[list[RetrievedChunk], operator.add]
    reranked_chunks: list[ScoredChunk]  # NO reducer -- replaced each pass
    # (ticker, year) pairs that HAD candidates but were dropped by the Rerank selection step
    # because query breadth (n_pairs x floor) exceeded max_context_chunks (S17). Semantically
    # DISTINCT from coverage_gaps (no evidence in the corpus) and unavailable_tickers/years
    # (outside the universe): here the evidence existed but context capacity did not. Written
    # once by rerank_node, read once by synthesize_node's honesty rail. No reducer -- replaced.
    dropped_for_capacity: list[tuple[str, int]]

    # -- Evaluate output --
    # Derived then stored: the Evaluate node (S13) runs a deterministic coverage
    # check first; non-empty coverage_gaps forces confidence="low" regardless of
    # the LLM's own label, and confidence_reason makes the trigger explicit.
    confidence: Literal["low", "high"]  # D1
    confidence_reason: Literal["coverage", "llm", "none"]  # D3
    coverage_gaps: list[tuple[str, int]]  # missing (ticker, year); [] = full
    # (ticker, year) pairs that surfaced as coverage-check misses but whose cause is the
    # S17 capacity floor (see dropped_for_capacity), NOT absent evidence. Written once by
    # evaluate_node; read by NO node in v1 -- kept as inspectable v2 signal (how many needed
    # cells went to budget vs genuinely missing). No reducer -- replaced.
    capacity_drops: list[tuple[str, int]]

    # -- Final output --
    citations: list[Citation]
    answer_stream: AsyncGenerator[str, None] | None
