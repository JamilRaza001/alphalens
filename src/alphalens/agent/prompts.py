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

from alphalens.agent.state import ScoredChunk

# ── Plan node ─────────────────────────────────────────────────────────────────

_PLAN_SYSTEM_TEMPLATE = """\
You are the query-planning module of AlphaLens, an analyst assistant that answers \
questions strictly from SEC 10-K / 10-Q filings.

Decompose the user's question into a structured plan with these fields:
- tickers: the uppercase stock ticker symbols the question is about.
- intent: exactly one of comparative | temporal | factual | qualitative.
- time_range.years: the DISCRETE reporting years explicitly named or clearly implied. \
List only the years actually asked for -- e.g. "2022 vs 2024" -> [2022, 2024]; do NOT \
add intermediate years such as 2023.
- sub_questions: the question broken into atomic, independently-answerable parts.
- entities: salient non-ticker entities (people, products, segments, financial metrics).

The corpus only covers these tickers:
{allowlist}
Prefer tickers from this list when the user names a company; this list only GUIDES you \
-- a downstream hard gate drops any ticker outside the corpus, so never invent tickers to \
satisfy the user.

Return ONLY the structured fields. Do not answer the question."""


def build_plan_system_prompt(allowed_tickers: frozenset[str]) -> str:
    """Fixed instructions with the (static, v1) allowlist baked in.

    Returns the SAME string for a given corpus (allowlist is sorted, so ordering is
    stable) -> Groq prompt-cache hit. If the `companies` seed changes, the allowlist
    regenerates once at the next cold-start (expected). The allowlist here only GUIDES
    the LLM; `validate_tickers` in nodes.py is the hard gate.
    """
    allowlist = ", ".join(sorted(allowed_tickers))
    return _PLAN_SYSTEM_TEMPLATE.format(allowlist=allowlist)


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

Write a clear, concise analyst answer."""


def build_synthesize_user_msg(
    query: str,
    reranked: list[ScoredChunk],
    confidence: str,
    unavailable_tickers: list[str],
) -> str:
    """Assemble the human turn: question, evidence, confidence flag, and the
    unavailable-ticker note (worded distinctly from coverage gaps)."""
    unavailable = ", ".join(unavailable_tickers) if unavailable_tickers else "(none)"
    return (
        f"User question:\n{query}\n\n"
        f"Confidence flag: {confidence}\n"
        f"Unavailable (out-of-corpus) tickers: {unavailable}\n\n"
        f"Retrieved evidence:\n{_render_evidence(reranked)}"
    )
