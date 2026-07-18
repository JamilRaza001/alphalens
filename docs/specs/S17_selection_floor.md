# Spec S17 — Cell-Aware Selection (Per-Pair Floor)

> Spec **S17** (selection_floor) · Retrieval-quality pass, **Problem 1 / Lever #1** · v8 cross-ref: §7.2 (Rerank/Select)
> targets: `src/alphalens/agent/nodes.py` (selection inside `rerank_node`)
> (+ small `config.py` / `state.py` / `prompts.py` piggybacks below).
> Format: L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).
> Consumes **verbatim**: S12 state, S13 nodes/prompts, S15 retrieve + `merge_dedup`.
> **This spec changes keep-SELECTION only. It does NOT touch retrieval, RRF, or the rerank SCORING call.**

---

## Terminology (locked — kills the 4-vs-24 ambiguity)

- **Cell** = the *retrieval* unit: `(ticker × year × sub_question)`. The Apple/Microsoft query fans out to
  **24 cells** (2 × 2 × 6), not 4. `merge_dedup` (nodes.py:401) flattens all cells and **discards cell provenance.**
- **Pair** = the *selection* grain: `(ticker, period_year)`. The same query has **4 pairs**. The floor operates on
  pairs, never on cells. This is the grain the reframe established and the grain `compute_coverage_gaps` already uses.

Knob names follow this: `floor_per_pair`, **not** `floor_per_cell` — using "cell" would re-introduce the exact
confusion the reframe fixed.

---

## Problem (recon-localized, Session 32)

The last live run ("Apple vs Microsoft R&D 2023 vs 2024") returned **7 AAPL : 1 MSFT chunks, MSFT-2023 absent** — and
the answer falsely claimed no MSFT was retrieved. Recon proved the bug is in **selection, not retrieval**:

- MSFT actually produced *more* candidates than AAPL (43 vs 38); MSFT-2024's best R&D chunk ranked #1 on its
  vector arm; every cell returned a full `n_per_cell` set.
- `rerank_node` scores every survivor, then does **one global sort + `scored[:rerank_top_n]` slice**
  (nodes.py:418, `rerank_top_n=8`). AAPL took all 8 slots; MSFT took 0. Margin was razor-thin (lowest kept 2.2051,
  highest rejected 2.0891 → MSFT lost the last slot by **0.116**). A cliff, not a decisive loss.

**Cheap-fix survivor:** `merge_dedup` discards *cell* provenance but each chunk keeps its `ticker` + `period_year`
fields → the `(ticker, year)` grouping is reconstructable **without** re-threading provenance and **without** touching
`merge_dedup`.

**Out of scope (Lever #2, separately gated):** the reranker scores each chunk against the *full comparison query*
(`state["query"]`), not the sub-question that fetched it — so within a pair, the chunk that name-drops the louder
entity can still outscore the one holding the actual R&D figure. Lever #1 guarantees a pair gets its slots; it does
**not** fix *which* chunks win inside a pair. See "Known residual" below.

---

## Goal

Replace the single global sort-and-slice with a **cell-aware selection** that guarantees every non-empty
`(ticker, year)` pair a floor of chunks (from its own top-scored candidates), fills remaining budget by global rerank
score (merit preserved), and caps total context at `max_context_chunks`. When the query is broad enough that pairs ×
floor exceeds the cap, **degrade honestly**: reduce depth before dropping coverage, and when coverage must be dropped,
report the dropped pairs so Synthesize can disclose them (evidence existed, capacity didn't) rather than silently
omitting or falsely claiming "not found."

Non-goal: sub-question-scoped reranking (Lever #2), provenance restoration, map-reduce breadth (v2).

---

## Config (piggyback — `config.py`)

| Knob | Default | Env | Role |
|------|---------|-----|------|
| `floor_per_pair` | `2` | `FLOOR_PER_PAIR` | Guaranteed chunks per non-empty `(ticker, year)` pair |
| `max_context_chunks` | `20` | `MAX_CONTEXT_CHUNKS` | Hard cap on chunks passed to Synthesize (the selection budget) |

- **Remove `rerank_top_n`.** Recon confirms nodes.py:418 is its *only* consumer; `max_context_chunks` replaces its
  role as the keep count. Migrate that reference and delete the knob (G2 — reconfirm no second consumer before deleting).
- Light validation only: `floor_per_pair >= 1`, `max_context_chunks >= 1`, `floor_per_pair <= max_context_chunks`.
  No `floor_per_pair <= n_per_cell` constraint — a pair aggregates multiple cells, so it can hold far more than
  `n_per_cell` candidates; the `min(floor, len(pair))` reclaim (below) handles thin pairs anyway.

*Why these numbers:* `floor_per_pair=2` gives minimum within-pair redundancy (one chunk rarely holds both the figure
and its context); `max_context_chunks=20` ≈ 20 × ~400 tok ≈ 8 K tokens — comfortably inside gpt-oss-120b context and
Groq free-tier per-call budget. Realistic queries (2–3 tickers × 2–3 years = 4–9 pairs) never overflow: 9 × 2 = 18 ≤ 20.

---

## State (piggyback — `state.py`)

Add one key, distinct in meaning from the existing coverage keys:

- `dropped_for_capacity: list[tuple[str, int]]` — `(ticker, year)` pairs that **had candidates** but were dropped
  because breadth exceeded the cap. Semantically separate from `coverage_gaps` (no evidence in corpus) and
  `unavailable_tickers` / `unavailable_years` (outside the universe). Default `[]`.
  - G3: match the live `state.py` typing convention (TypedDict key vs Pydantic field) and confirm whether a reducer
    is needed — this is written once by `rerank_node`, read once by `synthesize_node`, so last-write-wins / no reducer
    is expected. Verify against how `coverage_gaps` is declared and mirror it.

---

## Function Signatures

```python
# ── src/alphalens/agent/nodes.py ── PURE selection helper (no I/O, deterministic) ──
def select_with_floor(
    scored: list[ScoredChunk],          # every reranked survivor, already scored (scoring UNCHANGED)
    *,
    floor_per_pair: int,
    max_context_chunks: int,
) -> tuple[list[ScoredChunk], list[tuple[str, int]]]:
    """Cell-aware keep-selection replacing the global sort-and-slice.

    Returns (selected, dropped_for_capacity):
      selected              -> chunks to pass downstream, len <= max_context_chunks
      dropped_for_capacity  -> (ticker, year) pairs that had candidates but got 0 slots (hard-overflow only)

    Algorithm:
      1. Group `scored` by (chunk.ticker, chunk.period_year).        # G1/G4: confirm exact accessors + year type
      2. Sort each pair's list by rerank score desc (stable tie-break: chunk_id).
      3. n_pairs = number of pairs.
      4. Pick effective_floor + kept pairs:
           a. n_pairs * floor_per_pair <= cap
                -> effective_floor = floor_per_pair;  keep all pairs;  dropped = []
           b. n_pairs <= cap  (but a. failed)                        # GRADUATED: reduce depth, keep coverage
                -> effective_floor = max(1, cap // n_pairs);  keep all pairs;  dropped = []
           c. n_pairs > cap                                          # HARD OVERFLOW: coverage must be cut
                -> effective_floor = 1
                   rank pairs by their best (top-1) score desc; keep top `cap`, drop the rest
                   dropped = the dropped pairs' (ticker, year) keys
      5. reserved = concat, for each KEPT pair: pair_chunks[: min(effective_floor, len(pair_chunks))]
                                                                     # min(...) = empty-cell reclaim
      6. remaining = cap - len(reserved)
      7. fill = [c for c in scored if c not reserved AND c's pair is kept], by global score desc, take `remaining`
      8. selected = reserved + fill, then sort by score desc for final context order (best evidence first)
      9. return selected, dropped
    """

# ── src/alphalens/agent/nodes.py ── rerank_node change (SCORING untouched) ──
#   ... existing: score every candidate against state["query"]  (nodes.py:409-417 — DO NOT CHANGE — Lever #2 owns this)
#   REPLACE the global `scored.sort(...); return {"reranked_chunks": scored[:rerank_top_n]}` (≈:418) with:
#     selected, dropped = select_with_floor(
#         scored,
#         floor_per_pair=ctx.settings.floor_per_pair,          # G5: confirm settings access path in node
#         max_context_chunks=ctx.settings.max_context_chunks,
#     )
#     return {"reranked_chunks": selected, "dropped_for_capacity": dropped}
```

```python
# ── src/alphalens/agent/prompts.py ── Synthesize honesty-rail extension (minimal) ──
#   In the Synthesize system/user prompt builder, add ONE conditional block:
#   if dropped_for_capacity is non-empty, instruct the model to state plainly which (ticker, year) pairs
#   were omitted DUE TO CONTEXT LIMITS (not absence of data) and to suggest the user narrow or batch the query.
#   If empty -> emit nothing (no disclosure noise on normal queries).
```

```python
# ── tests/unit/test_selection_floor.py ── PURE, fakes only ──
def test_floor_guarantees_every_pair() -> None: ...           # AC3
def test_empty_cell_reclaim_frees_slots() -> None: ...        # AC4
def test_graduated_reduction_keeps_all_pairs() -> None: ...   # AC5
def test_hard_overflow_drops_and_reports() -> None: ...       # AC6
def test_s31_shape_msft_survives() -> None: ...               # AC8 — 7-AAPL/1-MSFT fixture, MSFT pair >= 1
def test_determinism_stable_ties() -> None: ...               # AC7
```

---

## Acceptance Criteria

1. **Knobs.** `floor_per_pair=2` and `max_context_chunks=20` exist, env-overridable, with the light validators above.
   `rerank_top_n` is removed and its single call site migrated to `max_context_chunks`.
2. **Pure helper.** `select_with_floor` performs no I/O and is deterministic for a given input; covered by unit tests
   with fake `ScoredChunk`s in default CI (no live resources).
3. **Floor guarantee (normal).** When `n_pairs * floor_per_pair <= cap`, every non-empty pair contributes exactly
   `min(floor_per_pair, len(pair))` chunks; the rest of the budget is filled by global score desc; total `<= cap`.
4. **Empty-cell reclaim.** A pair with fewer than `floor_per_pair` candidates reserves only what it has; the freed
   slots go to the fill pool (never wasted).
5. **Graduated reduction.** When `n_pairs * floor > cap` but `n_pairs <= cap`, the effective floor drops to
   `max(1, cap // n_pairs)`, **all** pairs remain covered, and `dropped_for_capacity == []`.
6. **Hard overflow.** When `n_pairs > cap`, exactly `cap` pairs are kept (ranked by best per-pair score), each gets 1
   slot, `len(selected) == cap`, and every dropped pair appears in `dropped_for_capacity`.
7. **Determinism.** Ties broken stably (score desc, then `chunk_id`); identical inputs → identical output ordering.
8. **Regression on the actual bug.** Given an S31-shaped fixture (AAPL-heavy pool, one thin MSFT pair, margins within
   ~0.1), the MSFT `(ticker, year)` pair is guaranteed `>= 1` chunk — the 7:1 wipeout **cannot recur**.
9. **Scoring untouched.** `rerank_node` still scores **all** candidates against `state["query"]`; only the keep step
   changed. The full-query scoring line is unmodified (explicitly reserved for Lever #2).
10. **Honest disclosure.** `dropped_for_capacity` reaches `synthesize_node`; when non-empty the answer discloses the
    omitted pairs and suggests narrowing/batching; when empty, no disclosure text is emitted.
11. **No provenance re-thread.** Grouping uses only the surviving `ticker` / `period_year` fields; `merge_dedup` is
    unchanged and cell (triple) provenance stays discarded.
12. **Gates.** `ruff` clean, `mypy --strict` clean (incl. the new state key and helper return type); selection unit
    tests pass in default CI.
13. **Live proof.** A re-run of the Apple/Microsoft 2023-vs-2024 query shows balanced ticker **and** year coverage
    (no pair at 0 while its evidence exists). This run is the representative shape that later feeds S16/AC14 and the
    Lever #2 gating decision.

---

## Known residual (feeds Lever #2 gating — deliberate)

Lever #1 fixes **how many** slots each pair gets, not **which** chunks fill them: within a pair, the reserved chunks
are still its top-scored under the *full-query* reranker. So MSFT-2023 is guaranteed 2 slots, but those 2 may be its
"Apple-mentioning" chunks rather than its strongest R&D chunks. AC13's live run is the measurement that decides
whether Lever #2 (sub-question-scoped reranking) is still needed or whether the floor alone is sufficient in practice.
**Do not pre-build Lever #2 into this spec.**

---

## Gotchas (live-verify checkpoints — S28 discipline)

- **G1 — Chunk field accessors.** Confirm the exact path to `ticker` and `period_year` on `ScoredChunk` (flat
  `chunk.ticker` vs nested `chunk.retrieved.ticker` vs `chunk.metadata[...]`). Recon said the fields survive
  `merge_dedup`; **reconfirm they are still present on the object `rerank_node` holds** before grouping. Do not assume.
- **G2 — `rerank_top_n` sole consumer.** Before deleting the knob, grep the repo to confirm nodes.py:418 is its only
  reference. If a test or doc references it, migrate those too (v8 §7.2 already describes the global-sort behavior —
  update it to the floor behavior in the same docs pass).
- **G3 — State key convention.** Add `dropped_for_capacity` the way `coverage_gaps` is declared (TypedDict vs Pydantic;
  reducer or none). Single-writer/single-reader → expect no reducer, but verify.
- **G4 — `period_year` type.** Confirm `int` vs `str` on the chunk; the pair key and the `dropped_for_capacity` list
  must use the same type consistently, or grouping silently splits "2023" from 2023.
- **G5 — Settings access inside the node.** Confirm how `rerank_node` reaches settings (`ctx.settings.*` vs a captured
  `settings` vs a config object). Mirror the existing pattern in that node; don't introduce a new access style.
- **G6 — `ScoredChunk` score attribute.** Confirm the score field name (`.score` vs `.rerank_score`) used at :418 and
  reuse it verbatim in the helper.
- **G7 — Downstream length assumption.** Confirm nothing downstream (synthesize context assembly, citation builder)
  hard-codes "exactly `rerank_top_n`/8 chunks." Selection now returns a variable count `<= cap`.
- **RECON coexistence.** The `# RECON` instrumentation is still in the working tree; this fix and the RECON cleanup
  (`git checkout src/alphalens/agent/nodes.py scripts/run_query.py` for the instrumentation lines) are **separate
  steps** — do not conflate them, and do not let the fix ride on RECON-only lines.
```
