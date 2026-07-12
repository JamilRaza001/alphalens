# Spec S15 — Retrieve Node (`retrieve_node` body)

> Spec **S15** (retrieve_node) · v8 cross-ref: §7.2 (Node Responsibilities, row 2), §5.2 (Agent Loop) · target:
> `src/alphalens/agent/nodes.py` (fills the S13 `retrieve_node` stub) + small `config.py` / `AgentContext` piggybacks.
> Format: L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).
> Consumes the S12 state contract + S13 node/DI surface verbatim. **No graph wiring here** (→ S16).
> Types/SQL verified against the **live schema** (`chunks` / `filings`) and the S8 `EmbeddingClient` query path.

---

**Decisions applied (locked in Session 29, this authoring session):**

1. **D1 — per-cell k/n (LOCKED).** `k_vector = 20`, `k_lexical = 20` (symmetric), RRF constant `c = 60`
   (standard smoothing, not a tuning knob), `n_per_cell = 10` RRF survivors per cell → merge. All four are
   **config-driven** `Settings` fields (env-overridable, eval-tunable defaults). Rationale: `k` deep so a
   chunk that is weak in one retriever but strong in the other still reaches RRF (that is the whole value of
   hybrid); `n` tight because it **multiplies across cells** and feeds the CPU-bound reranker.
2. **D2 — `rerank_top_n = 8` (LOCKED).** Bumped from the S13 placeholder `5`. Gives 4-cell comparison queries
   ~2-per-cell breathing room without blowing the free-tier synthesis token budget. Adaptive scaling
   (`min(cap, base + per_cell × n_cells)`) is a **v2 lever** (pairs with grouped-rerank/floor). `Settings.rerank_top_n`
   already exists (S13); only its default changes to `8`.
3. **D3 — cell definition (reconciles v8 §7.2 ↔ S13 docstring; CONFIRM at review).** A retrieval cell is the
   full triple **`(ticker × year × sub_question)`**. The `(ticker, year)` pair is the **WHERE filter**
   (parameterized) and also the **coverage unit** (`compute_coverage_gaps` needs each `(ticker, year)` present);
   the `sub_question` is the **search text** (drives both the jina-v3 query embedding and the lexical
   `plainto_tsquery`). v8 §7.2's "(ticker × sub-question)" and S13's "(ticker, year)" are two partial views of
   this same triple — the cross product reconciles them. `needed` cells mirror S13's `compute_coverage_gaps`
   (full `tickers × years` cartesian; cartesian false-low accepted in v1 → v2 fix).
4. **D4 — `AgentContext.embedder` piggyback (DERIVED, NECESSARY).** Add `embedder: EmbeddingClient` to
   `AgentContext`, built **once at cold-start** (Choice B pattern, same as `llm`/`reranker`/`pool`/`breaker`).
   `retrieve_node` is the only consumer in v1. Without it there is no query-embedding path.

**Invariants (carry-forward, non-negotiable):**

- **jina-v3 query embeddings, enforced.** The corpus is 100% jina-v3; a query embedded in nomic space is a
  different vector space ⇒ meaningless cosine ⇒ silent garbage retrieval. `embed_query` MUST return
  `model_version == "jina-v3"`; a nomic result (Jina quota exhausted at query time) is a **hard, loud failure**,
  never a silent fallback into a wrong-space search.
- **Single join `chunks → filings`.** `ticker` = `filings.ticker`; `period_year = EXTRACT(YEAR FROM filings.period_end)::int`;
  `filing_type = filings.filing_type`. FK is 1-to-1 (16,676 chunks == 16,676 joined rows — no fan-out). **No `companies` join.**
  The join lives *inside* the per-cell search SQL, so enrichment is free (one shot), not a second pass.
- **Parameterized SQL (tool-call guardrail).** Every LLM-derived value (`ticker`, `year`, `sub_question` text,
  embedding vector, k/n/c) goes through asyncpg `$1, $2, …` placeholders. **Never** f-string / `.format` / `%` into SQL.
  This is the injection surface; string interpolation here is a defect.

**`config.py` additions (applied alongside S15):**

```python
# Settings — S15 retrieval knobs (all env-overridable: RETRIEVAL_K_VECTOR, etc.)
retrieval_k_vector: int   = 20   # HNSW candidates fetched per cell (before fusion)
retrieval_k_lexical: int  = 20   # ts_rank_cd candidates fetched per cell (before fusion)
retrieval_rrf_c: int      = 60   # RRF smoothing constant in 1/(c + rank); standard, not tuned per-run
retrieval_n_per_cell: int = 10   # RRF survivors kept per cell → merge
# rerank_top_n: int = 8          # CHANGED default (was 5, S13); field already exists
```

---

### Goal

Replace the `retrieve_node` `NotImplementedError` stub (S13 Choice A) with real **per-cell fan-out hybrid
retrieval**. For each `(ticker × year × sub_question)` cell the node runs one hybrid query — HNSW dense
(jina-v3 query vector vs `chunks.embedding`, cosine) **fused with** lexical `ts_rank_cd(tsv, plainto_tsquery)`
via **Reciprocal Rank Fusion** — under a parameterized `WHERE ticker AND year` filter, joined to `filings`
so each surviving row is already enriched (`ticker`, `period_year`, `filing_type`). Cells run concurrently
(`asyncio.gather`, bounded by the asyncpg pool, max 5). Survivors merge with **`chunk_id` dedup** into a
`list[RetrievedChunk]`, appended to `state["retrieved_chunks"]` (S12 `operator.add` reducer). Fan-out
guarantees each cell a **fair shot at the reranker** — it does not force final inclusion; a genuinely empty
cell contributes nothing and surfaces honestly as a downstream coverage gap.

---

### Function Signatures

```python
# ── src/alphalens/agent/nodes.py — AgentContext piggyback (D4) ────────────────
from alphalens.etl.embeddings import EmbeddingClient   # S8 query embedder

@dataclass
class AgentContext:
    llm: ChatGroq
    reranker: CrossEncoder
    pool: Pool
    allowed_tickers: frozenset[str]
    breaker: SynthesisCircuitBreaker
    embedder: EmbeddingClient            # NEW (S15): jina-v3 query embeddings, cold-start built


# ── Pure helper: plan → cells (testable, no DB) ──────────────────────────────
def plan_to_cells(plan: QueryPlan) -> list[tuple[str, int, str]]:
    """Full (ticker, year, sub_question) product the query requires.
    (ticker, year) mirrors compute_coverage_gaps' `needed` (cartesian; false-low accepted, v1).
    Empty tickers / years / sub_questions ⇒ [] (nothing to retrieve; not an error)."""
    return [
        (t, y, sq)
        for t in plan.tickers
        for y in plan.time_range.years
        for sq in plan.sub_questions
    ]


# ── Pure helper: merge + dedup across cells (testable, no DB) ─────────────────
def merge_dedup(cell_results: list[list[RetrievedChunk]]) -> list[RetrievedChunk]:
    """Flatten per-cell survivors; keep first occurrence per chunk_id (a chunk can surface
    under two sub-questions of the same ticker/year). Order-stable so tests are deterministic."""
    seen: set[str] = set()
    merged: list[RetrievedChunk] = []
    for cell in cell_results:
        for rc in cell:
            if rc.chunk_id not in seen:
                seen.add(rc.chunk_id)
                merged.append(rc)
    return merged


# ── DB helper: one hybrid+RRF query per cell (join enriches in-shot) ──────────
async def hybrid_search_cell(
    pool: Pool,
    *,
    query_vector: list[float],   # jina-v3, 768-d, for THIS sub_question
    sub_question: str,           # lexical search text (plainto_tsquery)
    ticker: str,
    year: int,
    k_vector: int,
    k_lexical: int,
    rrf_c: int,
    n_per_cell: int,
) -> list[RetrievedChunk]:
    """Run the parameterized hybrid query for one cell and return up to n_per_cell enriched
    RetrievedChunks, RRF-ordered. All inputs are asyncpg placeholders — no interpolation.
    NOTE: lexical ranking is ts_rank_cd (NEVER call this 'BM25' — v8 L1)."""


# ── Node 2: Retrieve — real body (replaces the S13 stub) ─────────────────────
async def retrieve_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    """Per-cell fan-out hybrid retrieval (D3). See spec S15."""
    ctx  = runtime.context
    cfg  = get_settings()
    plan = state["query_plan"]

    cells = plan_to_cells(plan)
    if not cells:
        return {"retrieved_chunks": []}

    # Embed each UNIQUE sub_question once (jina-v3), reuse across its (ticker, year) combos.
    unique_subqs = list(dict.fromkeys(plan.sub_questions))          # order-preserving dedup
    embeds = await asyncio.gather(*(ctx.embedder.embed_query(sq) for sq in unique_subqs))
    vec_by_subq: dict[str, list[float]] = {}
    for sq, res in zip(unique_subqs, embeds):
        if res.model_version != "jina-v3":                          # INVARIANT — loud, not silent
            raise RuntimeError(
                f"query embedding fell back to {res.model_version!r}; corpus is jina-v3. "
                "Refusing to retrieve in a mismatched vector space."
            )
        vec_by_subq[sq] = res.vectors[0]

    # Fan out: one hybrid query per cell, concurrency bounded by the asyncpg pool (max 5).
    cell_results = await asyncio.gather(*(
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
    ))

    return {"retrieved_chunks": merge_dedup(cell_results)}
```

**Per-cell hybrid SQL (parameterized; join serves both filter and enrichment):**

```sql
-- $1 query_vector::vector | $2 ticker | $3 year | $4 k_vector
-- $5 sub_question (tsquery) | $6 k_lexical | $7 rrf_c | $8 n_per_cell
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
fused AS (                       -- Reciprocal Rank Fusion: sum of 1/(c + rank) over both lists
    SELECT COALESCE(vec.chunk_id, lex.chunk_id) AS chunk_id,
           COALESCE(1.0 / ($7 + vec.rnk), 0.0)
         + COALESCE(1.0 / ($7 + lex.rnk), 0.0) AS rrf_score
    FROM vec
    FULL OUTER JOIN lex ON vec.chunk_id = lex.chunk_id
)
SELECT c.chunk_id::text                        AS chunk_id,
       c.text                                  AS text,
       c.section                               AS section,       -- nullable
       f.ticker                                AS ticker,
       EXTRACT(YEAR FROM f.period_end)::int    AS period_year,
       f.filing_type                           AS filing_type,
       c.metadata                              AS metadata
FROM fused
JOIN chunks  c ON c.chunk_id  = fused.chunk_id
JOIN filings f ON c.filing_id = f.filing_id
ORDER BY fused.rrf_score DESC
LIMIT $8;
```

---

### Acceptance Criteria

1. **jina-v3 enforced.** `embed_query` is used for query text; if any result's `model_version != "jina-v3"`,
   `retrieve_node` raises (loud) and issues **no** search. A test injects a fake embedder returning
   `"nomic-embed-text-v1.5"` and asserts the raise + that `pool` is never touched.
2. **Parameterized only.** Every dynamic value (`ticker`, `year`, tsquery text, vector, k/n/c) is bound as an
   asyncpg placeholder. No f-string/`.format`/`%`/concatenation appears in any SQL string. (Grep-level check.)
3. **Single-join enrichment.** Every returned `RetrievedChunk` has non-null `chunk_id`, `ticker`, `period_year`,
   `filing_type`; `section` may be `None`. `ticker`/`period_year` come from `filings` via the in-query join;
   **no `companies` reference** appears.
4. **Fan-out shape.** With `plan.tickers = [A, B]`, `years = [2023]`, `sub_questions = [q1, q2]`,
   `plan_to_cells` yields exactly 4 cells `(A,2023,q1),(A,2023,q2),(B,2023,q1),(B,2023,q2)`; `hybrid_search_cell`
   is invoked once per cell.
5. **Embed-once.** Each **unique** sub_question is embedded exactly once even when it appears in multiple cells
   (`embed_query` call count == distinct sub_questions), verified with a counting fake embedder.
6. **RRF correctness.** `merge`/fusion places a chunk ranked highly in **both** retrievers above one ranked
   highly in only one; a chunk present in a single list is still scored (its missing side contributes 0).
   Tested on canned rank lists.
7. **Dedup.** A `chunk_id` returned by two cells appears **once** in the merged output, first occurrence kept;
   output order is deterministic. Tested via `merge_dedup` on overlapping cell lists.
8. **Config-driven.** `k_vector`, `k_lexical`, `rrf_c`, `n_per_cell` read from `get_settings()` (not literals in
   the node); overriding `RETRIEVAL_N_PER_CELL` in env changes the `LIMIT` bound. `rerank_top_n` default is `8`.
9. **Empty/absent cells.** A cell whose filter matches no rows returns `[]` and contributes nothing (no
   fabricated placeholder) — it becomes a downstream coverage gap, honestly. Empty `plan.tickers`/`years`/
   `sub_questions` ⇒ node returns `{"retrieved_chunks": []}` without hitting the DB.
10. **Reducer contract.** `retrieve_node` returns `{"retrieved_chunks": [...]}`; under S12's `operator.add` this
    **appends**. In v1 (single pass) it is the sole writer; forward-compatible with the v3 retry loop.
11. **mypy --strict + ruff** clean on the changed files; all inputs/outputs fully type-hinted.

---

### Gotchas

- **Live-verify the seams (S28 lesson — do NOT trust this pseudocode blindly).** Before wiring, confirm against
  the installed code: (a) `EmbeddingClient.embed_query` exact async signature + that `.vectors[0]` / `.model_version`
  are the real attribute names (S8); (b) how the codebase passes a `list[float]` embedding to asyncpg + pgvector —
  **reuse the S9 upsert pattern** (`register_vector` on the connection, or a `'[...]'` string cast to `$1::vector`);
  do not invent a new encoding; (c) `pool.fetch(...)` returns `asyncpg.Record` rows — map fields by name, and
  `Record["metadata"]` may arrive as a JSON string needing `json.loads` (check S9's read/write convention).
- **jina-v3 fallback at query time is the silent-corruption trap.** `EmbeddingClient` will transparently drop to
  nomic once Jina quota trips — correct for ingestion migration, **fatal for a single query** (query lands in
  nomic space, corpus is jina space, cosine is noise). The invariant check turns a silent quality collapse into a
  loud failure. Do not "handle" it by retrieving anyway.
- **`ts_rank_cd`, never "BM25" (v8 L1).** Comments and any docstrings say "lexical search via `ts_rank_cd`".
  Postgres FTS is not BM25; mislabeling it is a documented banned phrasing.
- **`EXTRACT(YEAR FROM period_end)` is not index-sargable**, but the corpus is tiny (~200 filings) and
  `idx_filings_ticker_date` narrows on `ticker` first, so the year extraction runs on a handful of rows.
  Acceptable for v1 — **do not** add a functional index or a generated `period_year` column for this.
  `period_year` semantics match S12 exactly (`EXTRACT(YEAR FROM filings.period_end)`), so coverage-check and
  retrieval agree by construction. (Fiscal-year filers, e.g. AAPL Sep-end, map to the calendar year of
  `period_end` — same rule everywhere, so no drift.)
- **Cell count multiplies: `|tickers| × |years| × |sub_questions|`.** A 3×3×3 query is 27 cells; `asyncio.gather`
  fires all, but the asyncpg pool (max 5) serializes execution — fine for v1 latency (~30–45 queries/day, latency
  non-critical). Embedding is per **unique** sub_question (not per cell), so embed cost scales with sub_questions,
  not the full product.
- **`FULL OUTER JOIN` in RRF is deliberate.** A chunk that appears in only the vector list (or only the lexical
  list) must still be scored; an `INNER JOIN` would silently drop hybrid's single-retriever finds — exactly the
  chunks fan-out+hybrid exist to rescue. Keep it `FULL OUTER`.
- **`c.embedding IS NOT NULL` guard.** Any chunk that failed embedding (should be none — corpus is 100% embedded,
  but the column is nullable) must not enter the vector CTE; the cosine operator on NULL is undefined-ish and
  pollutes ordering. Cheap defensive guard.
- **Fan-out guarantees a fair pool, not final inclusion.** A cell's chunks can all lose at rerank and never reach
  `rerank_top_n` — that is correct (merit-based), not a bug. The value of fan-out is that no cell is *starved at
  retrieval*, so a reported coverage gap means "data genuinely thin," not "our search was shallow." Do not add a
  per-cell floor here (that is a **v2 selection-stage** lever, and was rejected at retrieval-stage as a symptom patch).
- **`plainto_tsquery` vs `websearch_to_tsquery`.** `plainto_tsquery('english', $5)` treats the sub_question as a
  bag of AND-ed lexemes — robust for short analytic sub-questions and injection-safe. If a sub_question is long/
  operator-like, `websearch_to_tsquery` is an option — but pin `plainto_tsquery` for v1 (predictable, no operator
  surface). Flag if CC finds sub_questions arriving as long sentences that tank lexical recall.

---

### Out of Scope / Deferred

- **Reranking / selection** (`rerank_node` already exists, S13) — S15 only produces `retrieved_chunks`.
- **Graph wiring** (`StateGraph` / `add_node` / `.compile()` / `context=` at invoke) → **S16**; first real
  end-to-end run happens there.
- **Grouped rerank + per-cell floor**, **MMR/dedup on final selection**, **adaptive `rerank_top_n`**,
  **per-sub-question `(ticker, year)` pairing** (kills cartesian false-low) → **v2**.
- **HyDE** — ruled out (D6, S13): small query-document shape gap, hybrid handles precise entities, financial
  facts are hallucination-intolerant.
- **Cerebras fallback lane / not-hardcoding model IDs** → v2 infra.
- **S16 dependency:** the agent asyncpg pool MUST be built with `init=register_pgvector` (same as
  `etl/runner.py:133`), or the `list[float]`→`$1::vector` binding fails at first live run. All S15
  unit tests use a fake pool and will pass regardless — this only surfaces in S16's live run.
