# Spec S_CR — Company honesty rail (out-of-corpus names)

> Fixes **S5b**. Targets: `src/alphalens/agent/state.py`, `nodes.py` (`plan_node`), `prompts.py`.
> Format: L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).
> **Bug:** a company named in the query but absent from the corpus is dropped *silently*.
> Live probe (1 Groq call) on `"Compare Coca-Cola's and Apple's operating margin in fiscal 2024."`
> returned `tickers=['AAPL']`, `unavailable_tickers=[]`. The Plan LLM never emitted a symbol for
> Coca-Cola, so `validate_tickers` received nothing to drop. **The gate is not broken — the gate
> never received the input.** The answer's disclosure survived only incidentally (Synthesize sees
> the raw query text) and was wrongly framed as transient: "No Coca-Cola filing excerpts were
> retrieved" reads as retryable when the truth is structural.
>
> This is the **inverse** of the S16 year-rail failure: there the prompt under-guided and the
> deterministic gate caught the garbage; here the prompt *over-guided* (roster + "never invent
> tickers", with **no worked negative example**) and the gate had nothing left to catch.

## Decisions applied (locked)

1. **D-CR-a — New plan field, not `entities` mining.** The probe confirmed `entities` retains the
   name (`['Coca-Cola', 'operating margin']`) but **mixes companies with metrics** and has no type
   discriminator. Mining it needs a name-vs-metric heuristic, and on an *honesty* rail a false
   positive means telling the user a metric is an unavailable company. Rejected. The LLM does the
   disambiguation instead, in a dedicated field.
2. **D-CR-b — Flat `list[str]`, no nesting, no nullable.** `QueryPlan.unresolved_companies` is a
   plain string list. No `{name, ticker|null}` objects. Rationale corrected after R3: there is
   **no strict-mode enforcement to lean on** — langchain-groq pops and discards `strict=True`
   (`chat_models.py:1171`) and emits no `additionalProperties: false`, so the schema's
   `required` array is the only signal the model gets. A nullable/nested payload would have no
   decoder guarantee behind it, which is exactly why it is not worth taking here.
3. **D-CR-c — Worked negative example is the core of the fix, not the field.** Recon confirmed the
   ticker instruction has **no** worked negative example, while the year instruction does
   (`20232024`). Adding the field without the example will very likely reproduce the same silence
   in the new field. Both ship together or neither works.
4. **D-CR-d — Separate state key, names not symbols.** `unavailable_companies` is distinct from
   `unavailable_tickers`. Two different provenances: "LLM emitted a symbol that failed the
   allowlist" vs "LLM could not resolve a name to the roster at all". Collapsing them repeats the
   exact mistake C1 fixed. It also gives Synthesize the **name** to display — the user asked about
   "Coca-Cola", not "KO".
5. **D-CR-e — Structural framing in the disclosure.** The new clause must state the company is not
   in the corpus, not that retrieval returned nothing. Existing `unavailable_tickers` /
   `unavailable_years` clauses are **unchanged** in this spec.

## Recon checkpoints (CC confirms live BEFORE implementing — S28 lesson)

- **R1 — `_PLAN_SYSTEM_TEMPLATE` verbatim.** Paste the whole template. I need the exact ticker
  clause and the exact formatting of the year negative example, so the new example matches house
  style and the `{roster}` placeholder is not disturbed.
- **R2 — `build_synthesize_user_msg` + honesty rail.** Paste the signature and the clause-assembly
  block (prompts.py ~145–170). Confirm how `unavailable_tickers` / `unavailable_years` clauses are
  built and joined, so the new clause slots in parallel rather than being bolted on.
- **R3 — Defaulted field vs the emitted schema (BLOCKING). RESOLVED.** Determine whether adding
  `unresolved_companies: list[str] = Field(default_factory=list, ...)` keeps the field in the
  `required` array of the schema **langchain-groq actually emits** — not "under `strict=True`",
  which is a no-op: it is popped and discarded at `chat_models.py:1171` and never reaches Groq.
  **Finding (langchain_groq 1.1.2):** a default DOES remove the field from `required`, and
  nothing backstops it — no strict flag, no `additionalProperties: false`. The `required` array
  is the entire enforcement mechanism. **So the field ships required with no default**, with
  constructors fixed per R4 — a field the model may legally omit is precisely the failure we
  are fixing.
- **R4 — Existing `QueryPlan(...)` constructors.** `grep -rn "QueryPlan(" src/ tests/`. List every
  site. A required new field breaks all of them; report the count before editing.
- **R5 — `AgentState` construction.** `AgentState` has no `total=False`, so `unavailable_companies`
  is required. Confirm (as with C1/R3) that node-written keys are not required in the
  `graph.ainvoke(...)` input and no initializer seeds every key. If one does, seed `[]` there.

---

## Goal

Every company named in the query lands in exactly one of two places: `tickers` (resolved against the
roster) or `unresolved_companies` (not in the corpus). The second list reaches Synthesize as
`unavailable_companies` and is disclosed with **structural** framing. The rail stops depending on
the model happening to mention the dropped company in prose.

---

## Function Signatures / Logic

### `state.py` — `QueryPlan`, new field

```python
unresolved_companies: list[str]
# Every company named in the query that does NOT appear in the roster, verbatim as the user
# wrote it ("Coca-Cola", not "KO"). Partition invariant: a company named in the query appears
# in EITHER `tickers` (resolved) OR here -- never both, never neither.
```

Field description text matters — it is part of the schema the model sees. Keep it explicit about
the partition.

### `state.py` — `AgentState`, beside `unavailable_tickers`

```python
# Company names from the query that could not be resolved to a corpus ticker. Distinct in
# PROVENANCE from unavailable_tickers (which holds SYMBOLS the LLM emitted that failed the
# allowlist gate). Written once by plan_node; read by synthesize_node. No reducer -- replaced.
unavailable_companies: list[str]
```

### `nodes.py` — new gate, beside `validate_tickers`

```python
def validate_companies(
    unresolved: list[str], roster: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    """Split LLM-declared unresolved company names into (confirmed_unavailable, false_positives).

    Asymmetric to validate_tickers/validate_years by NECESSITY (see Gotchas): this gate can only
    catch FALSE POSITIVES -- a name the model called unresolved that is in fact in the roster.
    It cannot catch false negatives, because it cannot know what the query said.

    Matching closes LEGAL-SUFFIX variance and NOTHING else: the roster holds legal names
    ("Apple Inc.") while the model is asked for the user's wording ("Apple"), so a legal name
    that STARTS WITH the emitted name counts as in-roster. Direction is fixed -- the legal name
    is the haystack, never the reverse.

    THREE variance classes stay OPEN, in TWO OPPOSITE failure directions.
    UNDER-match -> false CONFIRM (an in-corpus company denied in the footer while the body cites
    its ticker), because startswith is left-anchored and casefold() normalizes case only:
      - SEMANTIC alias: "Google" does not match "Alphabet Inc.".
      - PUNCTUATION / WHITESPACE: "JP Morgan" does not match "JPMorgan Chase & Co." (diverges at
        index 2). "Amazon" matches "Amazon.com Inc." only incidentally.
    OVER-match -> false SUPPRESS, the more dangerous direction:
      - PREFIX COLLISION: "Alpha" matches "Alphabet Inc.", so a non-roster company is judged
        in-roster and DISCARDED with no disclosure -- the S5b silent drop, via this gate.
        _MIN_COMPANY_PREFIX trims that class's short-needle tail; it does not close the class.
    All three are the deferred v2 alias-table item.
    """
    legal_names = [name.casefold().strip() for name in roster.values()]
    confirmed = [c for c in unresolved if not _matches_roster(c, legal_names)]
    false_pos = [c for c in unresolved if _matches_roster(c, legal_names)]
    return confirmed, false_pos
```

with the needle-side helper and its threshold:

```python
# CUTS the densest tail of prefix over-matching ("V" would match "Visa Inc."); those fall back
# to exact equality. SURVIVES above it: "Alpha" still matches "Alphabet Inc.". A threshold trims
# a tail, it does not close a class.
_MIN_COMPANY_PREFIX = 3


def _matches_roster(emitted: str, legal_names: list[str]) -> bool:
    needle = emitted.casefold().strip()
    if len(needle) < _MIN_COMPANY_PREFIX:
        return needle in legal_names
    return any(name.startswith(needle) for name in legal_names)
```

### `nodes.py` — `plan_node`, after the existing ticker gate

```python
kept, dropped = validate_tickers(plan.tickers, runtime.context.allowed_tickers)
plan.tickers = kept

confirmed, false_pos = validate_companies(
    plan.unresolved_companies, runtime.context.ticker_roster
)
if false_pos:  # model contradicted itself; do NOT promote into tickers (see Gotchas)
    logger.warning("plan listed in-roster companies as unresolved: %s", false_pos)
plan.unresolved_companies = confirmed
```

and add to the return dict:

```python
"unavailable_companies": confirmed,
```

### `prompts.py` — `_PLAN_SYSTEM_TEMPLATE`, ticker clause

Add a worked negative example, matching the year example's existing style (exact wording to be
fitted to R1's paste):

```
Examples (tickers / unresolved_companies):
- "Apple vs Microsoft"        -> tickers: ["AAPL", "MSFT"], unresolved_companies: []
- "Coca-Cola vs Apple"        -> tickers: ["AAPL"], unresolved_companies: ["Coca-Cola"]
    NEVER drop Coca-Cola silently -- it is not in the roster, so it MUST be named in
    unresolved_companies. Omitting it entirely is always wrong.
- "Ford's revenue"            -> tickers: [], unresolved_companies: ["Ford"]

Every company the user names goes in EXACTLY ONE of the two lists. Never invent a ticker for a
company that is not in the roster.
```

### `prompts.py` — Synthesize honesty rail

New clause, parallel to the existing two, with structural framing:

```
The following companies are not covered by this corpus and no filings for them exist here:
{names}. State this plainly as a limitation of coverage. Do NOT describe it as excerpts not
being found or retrieval returning nothing -- that wrongly implies a retry would help.
```

---

## Acceptance Criteria

1. **Live: the S5b case.** `"Compare Coca-Cola's and Apple's operating margin in fiscal 2024."`
   ⇒ `unavailable_companies == ["Coca-Cola"]`, `tickers == ["AAPL"]`. This is the fix's whole point
   and **cannot be satisfied by a mock** (see Gotchas).
2. **Live: symbol form.** The same query written with the symbol (`"KO"`) ⇒ the company is recorded
   as unavailable by one path or the other — either `unavailable_tickers == ["KO"]` (emitted then
   gated) or `unavailable_companies` non-empty. Silence is a fail. *(This separates roster-name
   resolution from the gate, which the original S5b conflated.)*
3. **Live: no false positives.** An all-in-corpus query ⇒ `unresolved_companies == []` and
   `unavailable_companies == []`. The rail must not fire on the happy path.
4. **False-positive gate.** `validate_companies(["Apple"], roster)` ⇒ `confirmed == []`,
   `false_pos == ["Apple"]`; a WARNING is logged; nothing is promoted into `tickers`.
5. **Case/whitespace + legal suffix.** Matching is `casefold()`-based, so `"apple"` and `"APPLE"`
   are both caught as false positives. It additionally treats a roster legal name that
   `startswith` the emitted name as a match (`"Apple"` ⇒ `"Apple Inc."`), one-directionally;
   needles under 3 characters fall back to exact equality so `"V"` does not match `"Visa Inc."`.
6. **Key on the return path.** `plan_node`'s return dict always contains `unavailable_companies`
   (`[]` when there is nothing to report).
7. **Disclosure framing.** With a non-empty `unavailable_companies`, the answer states the company
   is not in the corpus. It must **not** use retrieval-failure phrasing ("no excerpts were
   retrieved", "nothing was found"). Assert on the rendered prompt clause, plus one live read.
8. **Existing rails untouched.** `validate_tickers`, `validate_years`,
   `split_concatenated_years`, and the `unavailable_tickers` / `unavailable_years` clauses are
   byte-identical. No change to retrieval, rerank, or evaluate.
9. **Schema.** R3 resolved and its answer applied; the generated json_schema is dumped in the PR
   notes. All R4 constructor sites updated.
10. **Gates green:** `ruff`, `mypy --strict`, `pytest`. Unit tests cover AC4–AC6 with fakes; AC1–AC3
    and AC7 are live.
11. **Live: bare colloquial name.** `"Apple"` (no legal suffix) never reaches
    `unavailable_companies` end-to-end. The legal-suffix match is the one class this rail
    closes, so it must not ship untested — an untested honesty rail is what produced S5b.
12. **Live: `tickers=[]` terminates gracefully.** Deterministic, one run proves it: the plan the
    prompt now teaches (`"Ford's revenue"` ⇒ `tickers: []`) must not crash. `retrieve_node`
    already short-circuits (`test_retrieve_node.py` AC9); rerank/evaluate/synthesize must
    complete without raising.
13. **OBSERVATION (n≥3), NOT a pass/fail gate: no confident vacuous answer on `tickers=[]`.**
    With `tickers=[]`, `compute_coverage_gaps` yields `needed = {}`, so `coverage_gaps` is empty
    and `evaluate_node`'s `if coverage_gaps:` precedence branch is **skipped entirely** — the
    verdict falls through to the LLM sufficiency call over zero passages. Nothing deterministic
    forces `confidence="low"`; `EVALUATE_SYSTEM_PROMPT` only nudges. A single green run is n=1,
    which is the model behaving once, not a working rail. Record `confidence` /
    `confidence_reason` across ≥3 runs and report the distribution. A deterministic fix
    (empty `needed` forcing low) is a SEPARATE spec — the twin of S16's `years=[]` case.

---

## Gotchas

- **This rail is weaker than its two siblings, by necessity. Say so in the docstring.**
  `validate_tickers` and `validate_years` are *complete* deterministic gates: they see the full
  candidate list and can filter it exhaustively. `validate_companies` cannot — it has no access to
  the query text, so it cannot know a company was named and omitted. It catches **false positives
  only**. Completeness rests on the prompt (D-CR-c). This **reduces** the S5b failure class, it does
  not eliminate it. Do not document it as a hard gate.
- **Mock tests cannot validate this change.** The schema, the strict-decoding behaviour, and the
  worked negative example are all LLM-boundary concerns. Per the project's standing rule, AC1–AC3
  and AC7 require real Groq calls. Budget ~4–6 calls including one failure retry; free tier is
  ~30–45/day.
- **Do not promote false positives into `tickers`.** If the model lists "Apple" as unresolved *and*
  omits AAPL from `tickers`, promoting would silently add a retrieval cell the plan never asked
  for. Filtering only degrades to today's behaviour (a miss), which is honest; promoting invents
  intent. Log and move on.
- **Prompt-cache invalidation.** The Plan system prompt is byte-stable and Groq-caches (S16/D3a).
  Editing the template invalidates that cache exactly once, at the next cold start. Expected, not a
  regression — but do not let it get attributed to something else in latency readings.
- **`entities` stays ungated and unused.** The probe showed it carries the signal, and it stays
  that way for diagnostics — but nothing may read it for rail decisions. Its name/metric mixing is
  the whole reason for this spec.
- **Required TypedDict key.** As with C1: every `plan_node` return path sets
  `unavailable_companies`; R5 confirms nothing upstream reads it and no initializer breaks.

---

## Out of scope / deferred (v2)

- **Year-rail twin.** No scenario has ever produced a non-empty `unavailable_years`. It shares this
  exact soft-guide shape and is suspect by symmetry — but it is a separate concern and a separate
  spec. Ship this first.
- **Alias-table work — the three residual classes this rail leaves open**, in two opposite
  failure directions. *Under-match ⇒ false confirm* (an in-corpus company is denied in the
  footer while the body cites its ticker): **semantic alias** (`"Google"` → `"Alphabet Inc."`,
  `"Facebook"` → `"Meta Platforms Inc."`) and **punctuation / whitespace** (`"JP Morgan"` →
  `"JPMorgan Chase & Co."`). *Over-match ⇒ false suppress*, the more dangerous direction:
  **prefix collision** (`"Alpha"` → `"Alphabet Inc."` discards a non-roster company with no
  disclosure). Also ticker-less subsidiaries.
- Mining `entities` for anything.
- `filing_type` as a retrieval axis (v2 rank #2).
- Rich per-company confidence signalling; `confidence_reason="unavailable"`.
