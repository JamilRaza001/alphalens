# Spec S_C1 — Capacity-drop vs Coverage-gap split in Evaluate

> Fixes **C1**. Targets: `src/alphalens/agent/nodes.py` (`evaluate_node`) + `src/alphalens/agent/state.py`.
> Format: L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).
> **Bug:** the S17 capacity floor (`rerank_node`) trims in-corpus `(ticker, year)` pairs for context
> budget and records them in `dropped_for_capacity`. Those trimmed pairs vanish from `reranked`, so
> `compute_coverage_gaps` (which returns `needed - present`) reports them as coverage gaps →
> `evaluate_node`'s precedence rule forces `confidence="low"`, `confidence_reason="coverage"`. Two
> distinct causes (evidence-missing vs budget-trimmed) collapse into one signal, corrupting the S4
> (graduated overflow) test-matrix reading.

## Decisions applied (locked — Opt 3, signal-first v1)

1. **D-C1a — Partition in Evaluate.** `evaluate_node` splits the raw coverage-check misses into true
   `coverage_gaps` (evidence genuinely absent) and `capacity_drops` (present-but-budget-trimmed) by
   **set membership** in `dropped_for_capacity`.
2. **D-C1b — Precedence on true gaps only.** The deterministic coverage precedence (`low`/`coverage`,
   LLM-skip) fires on non-empty **`coverage_gaps`** only. Capacity drops never force `low`.
3. **D-C1c — Record the split.** `capacity_drops` is written to state on every Evaluate return path,
   as inspectable v2 evidence (how many needed cells went to *budget* vs *genuinely missing*).
   **Write-only in v1** — no node reads it yet.
4. **D-C1d — Synthesize untouched.** The existing one-line capacity disclosure (synthesize reads
   `state["dropped_for_capacity"]` → `build_synthesize_user_msg`, prompts.py:155–167) is **unchanged**.
   The rich honesty rail (`confidence_reason="capacity"`, do-message path, synthesize reading the
   precise `capacity_drops`) is **C1 part 2 → v2**.

## Recon checkpoints (CC confirms live BEFORE implementing — S28 lesson)

- **R1 — `select_with_floor` `dropped` semantics.** Paste `select_with_floor` (nodes.py ~421–458).
  Confirm whether a pair returned in `dropped` can *also* retain ≥1 chunk in `selected` (a partial
  trim). The fix's partition is set-membership and is **robust either way** (see Gotcha 1) — this
  checkpoint is to surface surprises, not to gate the design. Also note if `dropped` can contain
  duplicate keys (we `set()` it, so dedup is already handled).
- **R2 — No early reader of `capacity_drops`.** `grep -rn "capacity_drops" src/` after adding the key
  and before wiring: confirm nothing reads `state["capacity_drops"]` upstream of `evaluate_node`
  (it's a required TypedDict key with no reducer — an early read would `KeyError`).
- **R3 — AgentState construction.** Confirm node-written keys (`coverage_gaps`, `dropped_for_capacity`)
  are filled **only** by their writing node and are *not* required in the `graph.ainvoke(...)` input /
  any state initializer. `capacity_drops` must follow the **same** pattern (no input default needed).
  If an initializer does seed every key, seed `capacity_drops: []` there too.

---

## Goal

Make Evaluate distinguish *why* a needed `(ticker, year)` cell is absent from the reranked context.
A cell trimmed by the S17 budget floor is no longer counted as an evidence gap: true evidence gaps
still force `low`/`coverage` (precedence preserved), while capacity drops are recorded separately and
leave confidence to the LLM sufficiency judgment. The split is persisted as a v2 signal.

---

## Function Signatures / Logic

### `state.py` — add beside `coverage_gaps`

```python
# (ticker, year) pairs that surfaced as coverage-check misses but whose cause is the
# S17 capacity floor (see dropped_for_capacity), NOT absent evidence. Written once by
# evaluate_node; read by NO node in v1 -- kept as inspectable v2 signal (how many needed
# cells went to budget vs genuinely missing). No reducer -- replaced.
capacity_drops: list[tuple[str, int]]
```

### `nodes.py` — `evaluate_node` (replace current body)

```python
async def evaluate_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    plan = state["query_plan"]
    reranked = state["reranked_chunks"]

    raw_gaps = compute_coverage_gaps(plan, reranked)   # needed - present (helper UNCHANGED)
    capacity = set(state["dropped_for_capacity"])      # pairs trimmed by the S17 floor (rerank_node)

    # Partition the raw misses by CAUSE. A pair absent from `present` is a real coverage
    # gap ONLY if it was not budget-trimmed; if it's in the capacity set, its evidence
    # existed and was cut for context space -- that is not "missing".
    coverage_gaps = [p for p in raw_gaps if p not in capacity]
    capacity_drops = [p for p in raw_gaps if p in capacity]

    if coverage_gaps:  # TRUE evidence gap -> structural precedence wins, LLM call skipped
        return {
            "confidence": "low",
            "confidence_reason": "coverage",
            "coverage_gaps": coverage_gaps,
            "capacity_drops": capacity_drops,
        }

    # No true gap. Capacity drops (if any) do NOT force low -- the LLM judges sufficiency
    # over the context it actually has.
    deterministic = runtime.context.llm.model_copy(update={"temperature": 0.0})  # S16/D-temp
    structured = deterministic.with_structured_output(
        EvalVerdict, method="json_schema", strict=True  # D3
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
        return {
            "confidence": "low",
            "confidence_reason": "llm",
            "coverage_gaps": [],
            "capacity_drops": capacity_drops,
        }
    return {
        "confidence": "high",
        "confidence_reason": "none",
        "coverage_gaps": [],
        "capacity_drops": capacity_drops,
    }
```

`compute_coverage_gaps` is **not** modified — the partition lives in Evaluate so the helper stays a
pure `needed - present` primitive.

---

## Acceptance Criteria

1. **Capacity-only.** All raw misses ∈ `dropped_for_capacity`, no true gap ⇒ Evaluate does **not** take
   the coverage-precedence path; it proceeds to the LLM branch and returns the LLM's verdict
   (`high`/`none`, or `low`/`llm`) — **never** `low`/`coverage`. `capacity_drops` == those pairs;
   `coverage_gaps` == `[]`. *(This is the S4-signal regression guard.)*
2. **True-gap.** A pair absent from present **and** not in `dropped_for_capacity` ⇒ `low`/`coverage`,
   the pair ∈ `coverage_gaps`, LLM call skipped.
3. **Mixed.** A true gap **and** a capacity drop both present ⇒ `low`/`coverage`; `coverage_gaps` holds
   only the true gap(s), `capacity_drops` holds only the trimmed pair(s); the two lists are disjoint
   and together partition `raw_gaps`.
4. **No-gap.** `raw_gaps == []` ⇒ LLM branch exactly as pre-fix; `capacity_drops == []`; behavior
   byte-identical to today on this path (aside from the extra key).
5. **Key on every path.** `capacity_drops` is present in the returned dict on all three Evaluate return
   statements (coverage / llm / none); the mixed case folds into the coverage return.
6. **Helper untouched.** `compute_coverage_gaps` signature + body unchanged.
7. **Synthesize untouched.** `synthesize_node` / `prompts.py` unchanged; the capacity disclosure still
   reads `state["dropped_for_capacity"]`.
8. **State + types.** `state.py` gains `capacity_drops: list[tuple[str, int]]`; `mypy --strict` clean;
   any full-state construction (recon R3) still type-checks.
9. **Gates green:** `ruff`, `mypy --strict`, `pytest`. New unit tests cover AC1–AC4 with a fake
   `AgentContext` + hand-built fixture `state` (no live Groq/DB). AC1's capacity-only test is explicit.

---

## Gotchas

- **Partition is set-membership → robust to S17 internals.** `set(dropped_for_capacity)` dedups any
  per-chunk duplicate keys; we only test membership. Whatever granularity `select_with_floor` records
  at, a pair is a `capacity_drop` iff it is both a raw miss **and** in the set. The fix does not depend
  on R1's answer — R1 only surfaces surprises.
- **A partially-trimmed pair is NOT a capacity_drop.** If S17 trimmed some but not all of a pair's
  chunks, the pair stays in `present` ⇒ not in `raw_gaps` ⇒ not in `capacity_drops`. Correct — it is
  genuinely represented. Only *fully-absent-yet-trimmed* pairs land in `capacity_drops`. This is
  precisely why synthesize's v1 disclosure (which lists the coarser raw `dropped_for_capacity`,
  including partial trims) is left as-is; tightening it to `capacity_drops` is v2.
- **Token cost shifts on the capacity-only path.** Pre-fix, *any* gap hit coverage precedence and
  skipped the Evaluate LLM call. Now a capacity-only case proceeds to that LLM call (one extra Groq
  request on that path). Correct — capacity is no longer a hard gap — but budget it in the test matrix
  (~30–45 agent-queries/day free tier).
- **Required TypedDict key.** `AgentState` has no `total=False`, so `capacity_drops` is required. Every
  Evaluate return sets it; recon R2/R3 confirm nothing reads it upstream and no initializer breaks.
- **`capacity_drops` is write-only in v1.** Do **not** wire it into synthesize now — that is C1 part 2.
  Its v1 job is test assertions + the v2 signal trail.

---

## Out of scope / deferred (C1 part 2 → v2)

- Rich honesty rail: a distinct `confidence_reason="capacity"`, the do-message path, and synthesize
  reading `capacity_drops` for a precise "trimmed for space" line.
- Realigning synthesize's disclosure from the coarse `dropped_for_capacity` to the precise
  `capacity_drops`.
- Cartesian false-low (per-sub-question `(ticker, year)` pairing) — unchanged, still v2.
