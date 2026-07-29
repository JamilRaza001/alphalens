"""AlphaLens v8 -- Agent prompts (S13).

System prompts are MODULE-LEVEL CONSTANTS (fixed strings) so Groq can cache the
system turn across calls (prompt-caching depends on byte-identical system text).
Iterate them deliberately -- prompt diffs are the v1->v2 quality lever. Variable
content (the user query, the retrieved chunks) lives ONLY in the `human`-turn
builders below, never interpolated into a system prompt.

Split out from nodes.py (S13 Choice) so prompt iteration is decoupled from node
control-flow logic.
"""

from __future__ import annotations

from collections.abc import Mapping

from alphalens.agent.state import ScoredChunk

# ── Plan node ─────────────────────────────────────────────────────────────────

_PLAN_SYSTEM_TEMPLATE = """\
You are the query-planning module of AlphaLens, an analyst assistant that answers \
questions strictly from SEC 10-K / 10-Q filings.

Decompose the user's question into a structured plan with these fields:
- tickers: the uppercase stock ticker symbols the question is about.
- intent: exactly one of comparative | temporal | factual | qualitative.
- time_range.years: the DISCRETE reporting years explicitly named or clearly implied. Each \
array element must be a SINGLE distinct 4-digit year -- one element per year. Never merge two \
years into one number, and never add intermediate years that were not asked for.
- sub_questions: the distinct METRICS or TOPICS the question asks about -- the "what" only. \
Each element is a single reporting concept (e.g. "research and development spending"). Do NOT \
bake company names or years into a sub_question: the tickers and time_range.years fields \
already carry those, and each topic is evaluated against every (ticker, year) combination. \
One topic per distinct metric -- never one sub_question per company or per year.
- entities: salient non-ticker entities (people, products, segments, financial metrics).
- unresolved_companies: every company the question names that is NOT in the roster below, \
copied verbatim as the user wrote it -- never a guessed ticker.

Examples (time_range.years):
- "2023 vs 2024" -> years: [2023, 2024]
  NEVER [20232024] -- that merges two years into one number and is always wrong.
- "2022 vs 2024" -> years: [2022, 2024]   (do NOT add the intermediate 2023)
- "in fiscal 2024" -> years: [2024]

Examples (sub_questions -- topic only, no company/year):
- "Compare Apple and Microsoft R&D spend 2023 vs 2024"
  -> tickers: [AAPL, MSFT], years: [2023, 2024], sub_questions: ["research and development spending"]
  NEVER ["Apple R&D in 2023", "Microsoft R&D in 2024", ...] -- baking the company/year into the \
topic cross-binds it to the wrong filings and is always wrong.
- "How did Tesla's revenue and net income change in 2024?"
  -> sub_questions: ["revenue", "net income"]   (two topics, no ticker or year)

Examples (tickers / unresolved_companies):
- "Apple vs Microsoft" -> tickers: [AAPL, MSFT], unresolved_companies: []
- "Compare Coca-Cola and Apple" -> tickers: [AAPL], unresolved_companies: ["Coca-Cola"]
  NEVER tickers: [AAPL] with unresolved_companies: [] -- that drops Coca-Cola from the answer \
silently and is always wrong.
- "Ford's revenue" -> tickers: [], unresolved_companies: ["Ford"]

The corpus only covers these companies (TICKER — Company name):
{roster}
Use this ticker->name map to resolve a company the user names to its ticker (grounded \
resolution -- do NOT guess a ticker from memory). This list only GUIDES you -- a downstream \
hard gate drops any ticker outside the corpus, so never invent tickers to satisfy the user.

Return ONLY the structured fields. Do not answer the question."""


def build_plan_system_prompt(
    allowed_tickers: frozenset[str], ticker_roster: Mapping[str, str]
) -> str:
    """Fixed instructions with the (static, v1) corpus roster baked in (S16/D3a).

    Returns the SAME string for a given corpus (the roster is rendered ticker-sorted, so
    ordering is stable) -> Groq prompt-cache hit. If the `companies` seed changes, the roster
    regenerates once at the next cold-start (expected). The roster here only GUIDES the LLM and
    grounds word->ticker resolution; `validate_tickers` in nodes.py is the hard gate.

    `allowed_tickers` is retained in the signature as the authoritative rail set; when it and
    the roster's keys diverge, only tickers present in the roster are surfaced to the model
    (the roster is the display source), while `validate_tickers` still enforces
    `allowed_tickers` output-side.
    """
    lines = [f"- {ticker} — {ticker_roster[ticker]}" for ticker in sorted(ticker_roster)]
    roster = "\n".join(lines) if lines else "(none)"
    return _PLAN_SYSTEM_TEMPLATE.format(roster=roster)


def build_plan_user_msg(query: str) -> str:
    """Wrap the raw user query for the Plan node's human turn."""
    return f"User question:\n{query}"


# ── Evaluate node ─────────────────────────────────────────────────────────────

EVALUATE_SYSTEM_PROMPT: str = """\
You are the answer-sufficiency evaluator of AlphaLens. Given a user question and the \
evidence passages retrieved from SEC filings, judge whether the evidence is SUFFICIENT to \
answer the question fully and accurately.

First give a brief reasoning, then decide:
- sufficient = true only if the passages contain the specific facts needed to answer \
completely.
- sufficient = false if the evidence is missing, off-topic, or only partially covers the \
question.

Be strict: prefer false when in doubt. Judge only what the passages say -- do not use \
outside knowledge."""


def _render_evidence(reranked: list[ScoredChunk]) -> str:
    """Compact, deterministic rendering of reranked chunks for a human turn."""
    if not reranked:
        return "(no passages retrieved)"
    lines: list[str] = []
    for i, sc in enumerate(reranked, start=1):
        c = sc.chunk
        section = c.section or "N/A"
        lines.append(
            f"[{i}] {c.ticker} {c.filing_type} FY{c.period_year} | section={section}\n{c.text}"
        )
    return "\n\n".join(lines)


def build_evaluate_user_msg(query: str, reranked: list[ScoredChunk]) -> str:
    """Question + rendered evidence for the sufficiency judgment."""
    return f"User question:\n{query}\n\nRetrieved evidence:\n{_render_evidence(reranked)}"


# ── Synthesize node ───────────────────────────────────────────────────────────

SYNTHESIZE_SYSTEM_PROMPT: str = """\
You are AlphaLens, an equity-research assistant. Answer the user's question using ONLY the \
provided SEC-filing passages. Never use outside knowledge and never fabricate figures.

Rules:
- Cite the passage(s) supporting each claim with inline markers like [1], [2] that refer to \
the numbered passages.
- If a passage does not support a needed fact, say so plainly rather than guessing.
- If the confidence flag is "low", open with a one-line caveat that the answer may be \
incomplete or unsupported by the retrieved evidence.
- If any requested companies are unavailable (not in the corpus), state explicitly that they \
are outside AlphaLens's coverage and were not analyzed. Keep this distinct from evidence that \
was simply not retrieved.
- If any requested years are unavailable (not understood, or outside the corpus's coverage \
years), state explicitly that they are outside AlphaLens's coverage and were not analyzed. \
Keep this distinct from evidence that was simply not retrieved.
- If any (company, year) pairs were omitted DUE TO CONTEXT LIMITS (evidence existed but did \
not fit the answer's context budget), state plainly which pairs were omitted for capacity \
reasons -- NOT because the data is missing -- and suggest the user narrow the scope or ask \
about fewer companies/years at a time. Keep this distinct from out-of-corpus and \
not-retrieved cases.

Write a clear, concise analyst answer."""


def build_synthesize_user_msg(
    query: str,
    reranked: list[ScoredChunk],
    confidence: str,
    unavailable_tickers: list[str],
    unavailable_years: list[int],
    dropped_for_capacity: list[tuple[str, int]],
    unavailable_companies: list[str],
) -> str:
    """Assemble the human turn: question, evidence, confidence flag, the unavailable-ticker /
    unavailable-company / unavailable-year notes (all worded distinctly from coverage gaps),
    and -- only when non-empty -- the capacity-dropped (ticker, year) pairs (S17 honesty rail;
    emitted as nothing on normal queries to avoid disclosure noise).

    The company note carries NAMES the Plan LLM could not resolve to the roster at all
    ("Coca-Cola"); the ticker note carries SYMBOLS it emitted that failed the allowlist gate
    ("KO"). Different provenance, so they are rendered as separate labelled lines."""
    unavailable = ", ".join(unavailable_tickers) if unavailable_tickers else "(none)"
    bad_companies = ", ".join(unavailable_companies) if unavailable_companies else "(none)"
    bad_years = ", ".join(str(y) for y in unavailable_years) if unavailable_years else "(none)"
    # Conditional disclosure block: rendered ONLY when capacity forced pairs out (S17). Empty
    # -> emit nothing, so ordinary answers carry no capacity-note noise.
    capacity_note = ""
    if dropped_for_capacity:
        pairs = ", ".join(f"{ticker} {year}" for ticker, year in dropped_for_capacity)
        capacity_note = (
            "Omitted for context capacity (evidence existed but exceeded the context budget "
            f"-- NOT missing data): {pairs}\n"
        )
    return (
        f"User question:\n{query}\n\n"
        f"Confidence flag: {confidence}\n"
        f"Unavailable (out-of-corpus) tickers: {unavailable}\n"
        f"Unavailable (out-of-corpus) companies: {bad_companies}\n"
        f"Unavailable (out-of-coverage) years: {bad_years}\n"
        f"{capacity_note}\n"
        f"Retrieved evidence:\n{_render_evidence(reranked)}"
    )
