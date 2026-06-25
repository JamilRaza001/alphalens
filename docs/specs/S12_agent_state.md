# Spec S12 — Agent State (`agent_state`)

> Spec **S12** (agent_state) · v8 cross-ref: spec 09 · target: `src/alphalens/agent/state.py`
> Format: L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).
> Types verified against the **live DB** (chunks / filings / companies) and the existing
> `src/alphalens/etl/chunker.py:Chunk` — not against v8 §6 (which is partly stale).

**Decisions applied (supersedes v8 §7.1 where noted):**
1. **D1 — `confidence: Literal["low","high"]`** (was `float`). LLM emits the label; a deterministic coverage check can force `"low"`.
2. **D2 — single source of truth (Option B).** `tickers` / `intent` / `entities` live **only** inside `query_plan`; v8's top-level duplicates are removed.
3. **D3 — explicit `confidence_reason`.** Records *which* signal drove the verdict (`coverage` / `llm` / `none`) — self-documenting instead of derived-from-two-fields.
4. **D4 — new agent-side `RetrievedChunk`.** The ETL `Chunk` (chunker.py) is **ingestion-side** — it has no `chunk_id`/`ticker`/`year` (those live in the DB / on `filings`). Agent needs a DB-derived, join-enriched chunk, so a distinct type is defined here. ETL `Chunk` is left untouched and is **not** imported.

---

### Goal

`AgentState` is the single in-memory contract threaded through all 5 LangGraph nodes (Plan → Retrieve+Filter → Rerank → Evaluate → Synthesize). Every node reads from it and returns a partial update that LangGraph merges. This spec defines the state's fields and types, which fields are write-once **anchors** (L7), which **accumulate** via a reducer (L5), and the supporting boundary types (`QueryPlan`, `RetrievedChunk`, `ScoredChunk`, `Citation`). It encodes L6 (no Refine in v1) and L19 (5-node single-pass). **No node logic here** — that is S13.

---

### Function Signatures

```python
from typing import Annotated, Any, AsyncGenerator, Literal
from operator import add
from typing_extensions import TypedDict
from pydantic import BaseModel

# NOTE: the ETL Chunk (src/alphalens/etl/chunker.py) is ingestion-side and is NOT used here.

Intent = Literal["comparative", "temporal", "factual", "qualitative"]


class TimeRange(BaseModel):
    """Discrete reporting years the query targets — NOT a continuous span."""
    years: list[int]  # "2022 vs 2024" -> [2022, 2024]; 2023 intentionally absent


class QueryPlan(BaseModel):
    """Structured output of the Plan node (LLM boundary -> validated here).
    Sole home of tickers / intent / entities (D2)."""
    tickers: list[str]            # ["AAPL", "MSFT"]
    intent: Intent
    time_range: TimeRange
    sub_questions: list[str]
    entities: list[str]           # v1 metadata filtering + v2 KG traversal


class RetrievedChunk(BaseModel):
    """One piece of evidence pulled from the DB and enriched via chunks -> filings
    (single join). Built by the Retrieve node (S15). Agent-side, query-time."""
    chunk_id: str                 # chunks.chunk_id (uuid, as str)
    text: str                     # chunks.text
    section: str | None           # chunks.section (NULLABLE in DB)
    ticker: str                   # filings.ticker  (single join; NOT via companies)
    period_year: int              # EXTRACT(YEAR FROM filings.period_end)
    filing_type: str              # filings.filing_type -> '10-K' | '10-Q'
    metadata: dict[str, Any] = {} # passthrough of chunks.metadata when useful


class ScoredChunk(BaseModel):
    """A RetrievedChunk paired with its cross-encoder rerank score."""
    chunk: RetrievedChunk
    rerank_score: float


class Citation(BaseModel):
    """A source reference surfaced in the final answer (derived from a RetrievedChunk)."""
    chunk_id: str                 # uuid
    ticker: str
    filing_type: str              # '10-K' | '10-Q'
    period_year: int
    section: str | None


class AgentState(TypedDict):
    # ── Anchors: set once at intake, NEVER mutated by any node (L7) ──
    original_query: str
    request_id: str
    user_id: str | None

    # ── Plan: set once by Node 1; sole home of tickers/intent/entities (D2) ──
    query_plan: QueryPlan

    # ── Mutable per pass ──
    query: str                    # == original_query in v1 (v3 Refine rewrites it — L6)
    iteration: int                # always 0 in v1; increments only in v3 retry loop

    # ── Accumulated: operator.add reducer (L5, forward-compatible with v3) ──
    retrieved_chunks: Annotated[list[RetrievedChunk], add]
    reranked_chunks: list[ScoredChunk]    # NO reducer — replaced each pass

    # ── Evaluate output ──
    confidence: Literal["low", "high"]                    # D1
    confidence_reason: Literal["coverage", "llm", "none"] # D3
    coverage_gaps: list[tuple[str, int]]                  # missing (ticker, year); [] = full

    # ── Final output ──
    citations: list[Citation]
    answer_stream: AsyncGenerator[str, None] | None
```

---

### Acceptance Criteria

1. `AgentState` is a `TypedDict`; constructing it with all keys type-checks under **mypy strict**.
2. `retrieved_chunks` uses `Annotated[list[RetrievedChunk], operator.add]`; two node updates each returning `{"retrieved_chunks": [...]}` **append** (merge), not overwrite.
3. `reranked_chunks` has **no** reducer; a node returning it **replaces** the prior value.
4. `confidence` accepts only `"low"` / `"high"`; `confidence_reason` only `"coverage"` / `"llm"` / `"none"`.
5. The Evaluate-node contract (tested in S13) holds: `coverage_gaps` non-empty ⇒ `confidence="low"` and `confidence_reason="coverage"`; empty gaps + LLM-low ⇒ `"low"`/`"llm"`; otherwise `"high"`/`"none"`.
6. `query_plan` is the **only** location of `tickers` / `intent` / `entities`; `AgentState` has **no** top-level copies (D2).
7. `original_query`, `request_id`, `user_id` are never reassigned after Node 1 (anchors — convention + comment; node-contract test in S13).
8. `RetrievedChunk` carries non-null `chunk_id`, `ticker`, `period_year`, `filing_type`; `section` may be `None`. The ETL `Chunk` is **not** imported or subclassed.
9. `coverage_gaps` holds `(ticker, year)` tuples the query required but retrieval did not cover; defaults to `[]`.
10. `QueryPlan`, `TimeRange`, `RetrievedChunk`, `ScoredChunk`, `Citation` are Pydantic `BaseModel`s.

---

### Gotchas

- **`RetrievedChunk` is agent-side, not the ETL `Chunk`.** chunker.py's `Chunk` is a frozen ingestion value-object (text + position only) with no id/ticker/year — it pre-dates the DB insert. Reusing it would leave `ticker`/`period_year` unavailable and silently break the coverage check. Keep the two types separate.
- **`ticker` + `period_year` come from a single join `chunks → filings`.** Verified live: `filings.ticker` and `filings.period_end` are NOT NULL; `chunks.filing_id → filings.filing_id` is a 1-to-1 FK (16,676 chunks == 16,676 joined rows, no fan-out). **No `companies` join needed.** `period_year = EXTRACT(YEAR FROM filings.period_end)`. Populating these is the Retrieve node's job (S15).
- **IDs are UUIDs.** `chunk_id` / `filing_id` are `uuid` in the DB → carry as `str`, never `int`.
- **`confidence` is derived then stored.** S13's Evaluate node runs the deterministic coverage check first; a non-empty `coverage_gaps` forces `"low"` regardless of the LLM's own label. `confidence_reason` makes the trigger explicit — this pair is the v1 failure evidence feeding v2 (KG) / v3 (retry).
- **`TimeRange` is discrete years, not a span.** A naïve `range(start, end)` over-requires 2023 for "2022 vs 2024" and produces false coverage gaps. Plan must emit the exact target years.
```
