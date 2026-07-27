# Test Matrix v1 — Live agent scenarios (S1–S5)

> **Purpose:** gather real failure signal from a full corpus on a committed baseline, before designing v2.
> Diagnostic only — findings feed v2 planning, not immediate fixes.
> **Harness:** `python scripts/run_query.py "<question>"` — read the footer.
> **Baseline:** post-C1 (`capacity_drops` split) + post-footer-patch (`ee404e1`).

## Live config (recon-confirmed — NOT the S17 spec doc's illustrative values)

| Knob | Live value |
|---|---|
| `floor_per_pair` | 2 |
| `max_context_chunks` (cap) | 8 |
| `rerank_top_n` | does not exist (removed by S17) |

**Branch boundaries**, where `n_pairs` = distinct `(ticker, period_year)` pairs **among retrieval survivors**:

| `n_pairs` | Branch | Behaviour | writes `dropped_for_capacity` |
|---|---|---|---|
| ≤ 4 | (a) normal | every pair gets full floor (2) | no — `[]` |
| 5–8 | (b) graduated | all pairs kept, depth cut to 1 | no — `[]` |
| ≥ 9 | (c) hard overflow | keeps 8 strongest pairs, drops rest | **yes** |

Only branch (c) populates `capacity_drops`. Scenarios are sized by **cell count** to target a specific branch.

---

## Footer read-off

```
confidence= reason= coverage_gaps= capacity_drops= unavailable= unavailable_years= latency=
```

`reason` values: `coverage` (true gap, LLM skipped) · `llm` (LLM judged insufficient) · `none` (high).

---

## Scenarios

### S1 — Narrow / depth
**Cells:** 1 ticker × 1 year = **1 pair** → branch (a). Floor takes 2, remaining 6 filled by global score — so up to 8 chunks all on one cell. Maximum depth case.

> **Query:** `What were Apple's total net sales and how did management explain the change in fiscal 2023?`

**Expect:** `confidence=high` `reason=none` `coverage_gaps=[]` `capacity_drops=[]` `unavailable=[]`
**Also record:** does the answer contain an actual dollar figure with a citation? — this is the **dead `lex_arm` probe** (recon-only in v1; fix is v2). A qualitative-only answer with no figures is the signal.

---

### S2 — Breadth + skew (floor guarantee at the boundary)
**Cells:** 4 tickers × 1 year = **4 pairs** → branch (a) at its exact boundary (4 × 2 = 8 = cap). Zero global fill: every pair gets exactly 2 chunks, nothing more. This is the cleanest possible test of the floor — without it, a systematically under-ranked ticker gets squeezed to zero.

> **Query:** `Compare the fiscal 2024 revenue growth drivers for Apple, Microsoft, Google, and Amazon.`

**Expect:** `confidence=high` `reason=none` `coverage_gaps=[]` `capacity_drops=[]`
**Also record:** citations must show **all 4 tickers, 2 chunks each**. Specifically check MSFT is present — the known cross-encoder skew (Lever #2, v2) predicts MSFT under-ranks on full-query reranking. If MSFT appears with exactly its floor-2 and nothing more, the floor is doing the work the reranker isn't.

---

### S3 — Temporal / discrete years
**Cells:** 1 ticker × 2 **non-contiguous** years = **2 pairs** → branch (a). Deliberately skips the intervening years to test the discrete-year rail (Gotcha 1): a `range(min, max)` bug would over-require 2022 and 2023 and fabricate two coverage gaps.

> **Query:** `How did Microsoft's operating margin in fiscal 2021 compare to fiscal 2024?`

**Expect:** `confidence=high` `reason=none` `coverage_gaps=[]` `capacity_drops=[]`
**Fail signal:** any `(MSFT, 2022)` or `(MSFT, 2023)` appearing in `coverage_gaps` = discrete-year rail broken.

---

### S4a — Graduated overflow (coverage kept, depth cut)
**Cells:** 3 tickers × 2 years = **6 pairs** → branch (b). All 6 pairs survive at depth 1 each. Nothing is dropped.

> **Query:** `Compare R&D spending for Apple, Microsoft, and Google across fiscal 2023 and fiscal 2024.`

**Expect:** `coverage_gaps=[]` `capacity_drops=[]` — **`capacity_drops` must be empty**; graduated reduces depth, it does not drop pairs.
**Also record:** citations should show ~1 chunk per cell across 6 cells. Answer quality at depth-1 is the signal — is one chunk per cell enough to answer comparatively?

---

### S4b — Hard overflow (the C1 proof)
**Cells:** 5 tickers × 2 years = **10 pairs** → branch (c). Keeps 8 strongest, drops the rest into `dropped_for_capacity`. Those dropped pairs then surface as raw coverage-check misses — **pre-C1 this forced `low`/`coverage`**.

> **Query:** `Compare cloud and advertising revenue trends for Apple, Microsoft, Google, Amazon, and Meta across fiscal 2023 and fiscal 2024.`

**Expect (the C1 fix, end to end):**
- `capacity_drops=` **non-empty** (the trimmed pairs)
- `coverage_gaps=[]`
- `reason=` **not `coverage`** — must be `none` or `llm`

**Fail signal:** `reason=coverage` with the dropped pairs listed in `coverage_gaps` = C1 regression.
**Note:** the exact drop count is not predictable — `n_pairs` counts retrieval survivors, so if retrieval under-surfaces a cell it becomes a true coverage gap instead. Judge on **shape**, not count.

---

### S5a — True coverage gap (honesty rail, real corpus hole)
**Cells:** 1 ticker × 1 year, targeting the **known** NVDA 2021 hole (no filing row — M1 `date_from` floor, deferred to v2). Genuine absent evidence, not a budget trim. The exact complement of S4b.

> **Query:** `What did NVIDIA report for data center revenue in fiscal 2021?`

**Expect:** `confidence=low` `reason=coverage` `coverage_gaps=[('NVDA', 2021)]` `capacity_drops=[]`
**Also record:** does the answer **say** it lacks the filing, or does it fabricate/silently substitute a different year? Honest refusal is the pass. Evaluate's LLM call is skipped on this path (2 Groq calls, not 3).

---

### S5b — Out-of-corpus ticker (plan ticker-rail)
**Cells:** a company outside the 10-ticker corpus, caught pre-retrieval by the Plan validator.

> **Query:** `Compare Coca-Cola's and Apple's operating margin in fiscal 2024.`

**Expect:** `unavailable=['KO']` (or whatever symbol Plan resolves) populated; AAPL 2024 answered normally.
**Also record:** does the answer disclose the unavailable ticker rather than quietly answering only for Apple? Silent omission is the failure mode.

---

## Run protocol

1. Run scenarios **in order**, one at a time. Paste the full footer + citations block for each.
2. Do not change config between runs — the branch math above assumes `floor_per_pair=2`, `max_context_chunks=8`.
3. **Groq budget:** 7 runs × 3 calls = ~21 (S5a is 2 — coverage path skips Evaluate). Free tier ~30–45/day, so one full pass fits with rerun headroom. Do not batch-run blindly; a failed run mid-matrix should stop the pass.

## Capture template (per scenario)

```
Scenario:
Footer:      confidence=  reason=  coverage_gaps=  capacity_drops=  unavailable=  latency=
Citations:   <ticker year count per cell>
Pass/fail:   <against Expect above>
Observation: <the "Also record" item — this is the v2 signal>
```

## Known blind spots (expected, not failures)

- **Dead `lex_arm`** — exact dollar figures may be missed. Recon-only in v1; probe is S1.
- **Cartesian false-low** (Gotcha 2) — `needed` is the full ticker × year product, so asymmetric queries over-generate cells. All scenarios above are deliberately symmetric to keep the signal clean; per-sub-question pairing is v2 (Fix B).
- **Cross-encoder skew** — MSFT under-ranking (Lever #2, v2). Probed in S2, not fixed here.

---

## Results (live pass, single run each)

**Score: 5 PASS / 2 FAIL.** Corrected from the initial 6/1 — see S3.

| # | Footer | vs Expect | Verdict |
|---|---|---|---|
| S1 | high/none, gaps [] drops [] | met | PASS |
| S2 | high/none, 2/2/2/2 | met | PASS (but see design errors) |
| S3 | **low/llm**, gaps [] | **Expect said high/none** | **FAIL** |
| S4a | low/llm, drops [] | met (drops empty as required) | PASS |
| S4b | low/llm, drops = AMZN ×2, gaps [] | met — C1 proven | PASS |
| S5a | low/coverage, gaps [(NVDA,2021)] | met — C1 complement | PASS |
| S5b | unavailable=[], plan.tickers=[AAPL] | **Expect said unavailable=['KO']** | **FAIL** |

**C1 proven from both sides:** S4b (capacity trim → 2 pairs in `capacity_drops`, gaps `[]`, Evaluate
LLM ran) vs S5a (real hole → 1 pair in `coverage_gaps`, drops `[]`, Evaluate LLM skipped — 4.77s vs
23.41s). S4b's prose distinguished both causes unprompted: "omitted for context-capacity reasons" for
Amazon vs "not disclosed in the excerpts" for Google/Meta. The v1 one-line disclosure delivered
without extra work.

### S5b root cause — CONFIRMED by recon (0 Groq calls)

The query used the company **name** ("Coca-Cola"), not the symbol. The roster maps only the 10
in-corpus companies, and the ticker instruction carries **no worked negative example** for an
out-of-corpus company — only a general "never invent tickers" clause. The Plan LLM therefore emitted
no ticker for Coca-Cola at all, so `validate_tickers` received nothing to drop and
`unavailable_tickers` stayed `[]`.

Ruled out by recon: the footer reads the correct key (`unavailable_tickers`), and `plan_node`'s
return dict is the **only** write site for that key in all of `src/` — no clobber.

**The gate is not broken. The gate never received the input.** This is the inverse of the year-rail
lesson: there the prompt under-guided and the deterministic gate caught the garbage; here the prompt
*over-guided* and the gate had nothing left to catch. The drop happened inside the model, invisibly.

The answer's disclosure ("No Coca-Cola filing excerpts were retrieved") survived only incidentally —
Synthesize sees the raw query text — and it is **wrongly framed**: it reads as transient/retryable
when the truth is structural.

Open probe (1 Groq call, not yet run): dump the raw `QueryPlan` for this query and check whether
`entities` retains the string "Coca-Cola" while `tickers=[AAPL]`. Recon confirmed `entities` and
`sub_questions` are ungated free-text `list[str]`, so the signal *may* survive there. If it does, the
fix is cheap and v1-scoped; if not, the fix means changing the prompt or schema and belongs in v2.

### Recurring findings (v2 signal, ranked)

1. **Tabular/derived financials not retrievable — strongest signal.** Operating margin failed 2/2
   (S3, S5b). S4a surfaced an Apple 10-K chunk containing the table header and row labels but **no
   numbers**. Prose figures retrieve fine (S1 $383.3B, S4b $12.9B). Root cause is ETL/chunking, not
   `lex_arm` — this **inverts** the original "dead lex_arm = missed figures" premise and gives the
   deferred iXBRL parser fix concrete evidence. Lexical-arm recon drops in priority accordingly.
2. **10-K/10-Q conflation — structural, confirmed by recon.** `filing_type` is a display field only:
   never in the retrieval WHERE clause, never in any cell key. Cells are `(ticker, year,
   sub_question)`; coverage is `(ticker, year)`. Quarterly volume therefore outvotes annual filings on
   annual questions **by design**. Hit S1 (top-3 all 10-Q), S2 (6/8 10-Q), S3 (FY2021 4/4 10-Q, zero
   10-K). Correctness risk, not cosmetic.
3. **Year concatenation — systematic.** `20212024` / `20232024`, fired 3/3 on multi-year queries and
   0/2 on single-year. The repair shim caught every case; it is load-bearing.
4. **Section detector gaps.** MSFT/META/AMZN chunks unstructured. Separately, R&D/revenue queries
   returned chunks labelled "Item 1. Legal Proceedings" that contained cloud-revenue content —
   suspected 10-Q Part I Item 1 (Financial Statements) vs Part II Item 1 collision. Hypothesis,
   recon pending.
5. **MSFT skew did NOT reproduce.** In S4b, MSFT took the top-2 slots and gave the strongest
   quantitative answer. This is **n=1: insufficient evidence, not counter-evidence.** Lever #2 must be
   re-validated across multiple runs in v2 — neither fixed blindly nor dropped blindly.
6. **Table formatting breaks** — markdown padding blow-out plus `<br>` tags. Frontend track note.

### Design errors in this matrix (correct in v2)

- **One verdict per scenario is the wrong granularity.** Pass/fail measured only the *control plane*
  (branch math, footer signals). The *data plane* (filing type, correctness of figures) was captured
  as prose observation with no criterion — which is why finding #2 went unscored across 3 scenarios.
  v2 needs a separate **rail verdict** and **answer verdict** per scenario.
- **S2 is vacuous.** At the boundary 4 × 2 = 8 = cap there is zero global fill, so the floor hands
  every pair exactly 2 regardless of score — the scenario **cannot** reveal skew, which was its stated
  purpose. Skew needs headroom: 3 pairs = 6 floor + 2 global fill, then observe where those 2 land.
- **S3 was scored on its narrow fail-signal** (`(MSFT,2022)`/`(MSFT,2023)` in gaps) while its own
  Expect line went unchecked. The discrete-year rail did hold; the scenario still failed.

### Untested rails (blind spots the original blind-spot section missed)

- **The year rail was never exercised.** No scenario produced a non-empty `unavailable_years`. It has
  the identical soft-guide + hard-gate shape as the ticker rail, so **S5b's failure mode is suspect by
  symmetry**: an out-of-bounds year (2019, 2030) would likely be omitted by the LLM the same way,
  leaving `unavailable_years=[]` with no signal. Highest-priority missing scenario.
- **The `years=[]` hole is pre-documented but untested.** S16 records it as known-and-accepted: if
  *all* years are dropped, `needed` becomes vacuously empty → `coverage_gaps=[]` → Evaluate falls to
  the LLM branch and lands `confidence_reason="llm"` rather than `"coverage"`. A documented failure
  with no scenario written for it.
- **`filing_type` was never asserted** in any criterion, despite breaking in 3 of 7 scenarios.
- **Disclosure wording was never a criterion.** S4b's correct two-cause distinction and S5b's
  wrong transient framing were both post-hoc observations, not checks.

### Proposed scenarios for matrix v2

| # | Targets |
|---|---|
| S2' | 3 pairs (6 floor + 2 global fill) — skew with actual headroom |
| S6 | Out-of-bounds year — the year-rail twin of S5b |
| S7 | All years dropped — the documented `years=[]` vacuous-coverage hole |
| S8 | Annual question on a cell holding both 10-K and 10-Q — assert filing-type mix |
| S5b-i / S5b-ii | Out-of-corpus by ticker symbol vs by company name — separates roster-resolution failure from gate bypass |
