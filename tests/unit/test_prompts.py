"""Unit tests for src/alphalens/agent/prompts.py.

Pure string assembly -- no Groq, no DB, no nodes. Covers the Synthesize honesty-rail
disclosure lines, whose wording is the contract Synthesize reads.
"""

from __future__ import annotations

from alphalens.agent.prompts import build_plan_system_prompt, build_synthesize_user_msg

_ROSTER = {"AAPL": "Apple Inc.", "MSFT": "Microsoft Corporation"}


def _msg(
    *, unavailable_tickers: list[str] | None = None, unavailable_companies: list[str] | None = None
) -> str:
    return build_synthesize_user_msg(
        query="q",
        reranked=[],
        unavailable_tickers=unavailable_tickers or [],
        unavailable_years=[],
        dropped_for_capacity=[],
        unavailable_companies=unavailable_companies or [],
    )


def test_synthesize_msg_companies_line_none_when_empty() -> None:
    # Unconditional labelled line, like tickers/years -- NOT the conditional capacity block.
    assert "Unavailable (out-of-corpus) companies: (none)" in _msg()


def test_synthesize_msg_renders_company_names() -> None:
    # The NAME reaches the model, not a ticker -- the user asked about "Coca-Cola", not "KO".
    msg = _msg(unavailable_companies=["Coca-Cola", "Ford"])
    assert "Unavailable (out-of-corpus) companies: Coca-Cola, Ford" in msg


def test_synthesize_msg_companies_line_distinct_from_tickers_line() -> None:
    # Two provenances, two lines: SYMBOLS that failed the allowlist vs NAMES never resolved.
    msg = _msg(unavailable_tickers=["KO"], unavailable_companies=["Coca-Cola"])
    assert "Unavailable (out-of-corpus) tickers: KO" in msg
    assert "Unavailable (out-of-corpus) companies: Coca-Cola" in msg


def test_synthesize_msg_carries_no_confidence_flag() -> None:
    # S_CR Phase 4: the confidence flag left the prompt entirely -- the caveat is now emitted
    # deterministically in nodes.py. A reappearing flag line means the rail regressed to a
    # prompt directive, which Phase 3 measured binding on only 1 of 3 fixed-input runs.
    assert "Confidence flag" not in _msg()


def test_plan_system_prompt_renders_roster_and_example_block() -> None:
    # The template is .format()-ed: prove {roster} still substitutes after the edit, and that
    # the worked negative example (the load-bearing half of this rail) survives verbatim.
    prompt = build_plan_system_prompt(frozenset(_ROSTER), _ROSTER)
    assert "{roster}" not in prompt
    assert "- AAPL — Apple Inc." in prompt
    assert "Examples (tickers / unresolved_companies):" in prompt
    assert "NEVER tickers: [AAPL] with unresolved_companies: []" in prompt
