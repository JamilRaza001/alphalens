"""S16/D4 -- live end-to-end regression net for the agent graph.

One real run of the compiled graph against LIVE Neon + Groq, asserting STRUCTURAL invariants
only -- never content. The drive query is a >=2-ticker x >=2-year comparative (AC13) so
per-cell fan-out + RRF + the ``chunks->filings`` join are actually exercised.

Carries the ``live`` marker and is EXCLUDED from the default ``pytest`` run
(``addopts = -m "not live"``); needs live Neon + Groq creds + network. Run with ``-m live``.

The six assertions:
  A1  the drained-and-joined answer text is non-empty after stripping,
  A2  at least one citation is returned,
  A3  every ``citation.ticker`` is in ``ctx.allowed_tickers``,
  A4  every ``query_plan.tickers`` entry is in ``ctx.allowed_tickers``,
  A5  ``confidence`` is one of {"low", "high"} and ``confidence_reason`` is one of
      {"coverage", "llm", "none"} -- the D1/D3 labels; there is no ok/degraded field,
  A6  PAIR CLOSURE: every planned (ticker, year) cell is ACCOUNTED FOR -- cited, or named in
      coverage_gaps / capacity_drops / dropped_for_capacity, or ruled out by
      unavailable_tickers / unavailable_years.

A6 is the one that earns its keep. A1-A5 can all pass while the graph silently loses a cell
between the plan and the answer; A6 says every cell the plan asked for either produced
evidence or was explicitly declared missing by a named rail. What it deliberately does NOT
pin: any exact citation count, any figure or word in the answer text, any latency budget,
per-ticker citation balance (covered purely by ``test_s31_shape_msft_survives``), or
``filing_type`` membership -- that last would be a NEW runtime constraint, not a restatement
of the model.
"""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from alphalens.agent.context import build_context
from alphalens.agent.graph import build_graph
from alphalens.agent.state import AgentState

# AC13 shape: >=2 tickers x >=2 years, so the per-cell fan-out is real.
LIVE_QUERY = "Compare Apple's and Microsoft's R&D spend in fiscal 2023 vs 2024."


@pytest.mark.live
async def test_agent_end_to_end_live() -> None:
    """Structural invariants of one live end-to-end run. See module docstring for the contract."""
    ctx, pool = await build_context()
    try:
        graph = build_graph()
        # AgentState is an all-required TypedDict; the remaining keys are filled by the nodes.
        # cast keeps mypy --strict happy with the partial intake literal (only anchors + seeds).
        intake = cast(
            AgentState,
            {
                "original_query": LIVE_QUERY,
                "query": LIVE_QUERY,  # == original_query in v1 (L6)
                "request_id": str(uuid4()),
                "user_id": None,
                "iteration": 0,
            },
        )

        # langgraph types ainvoke's return as `dict[str, Any] | Any`; the compiled graph's
        # output schema IS AgentState, so name it as such at this one boundary (api/app.py:169).
        # answer_stream stays UN-DRAINED here (G3) -- the breaker gate / degraded fallback is
        # evaluated lazily by the `async for` below, with the pool still OPEN (G4).
        final_state = cast(AgentState, await graph.ainvoke(intake, context=ctx))

        stream = final_state["answer_stream"]
        assert stream is not None, "AC14/A1: synthesize_node must set answer_stream"
        answer = "".join([token async for token in stream])  # D2 drain, pool still open (G4)

        # ── A1: the answer carries text ──────────────────────────────────────
        assert answer.strip(), "AC14/A1: streamed answer must be non-empty after stripping"

        # ── A2: at least one citation ────────────────────────────────────────
        citations = final_state["citations"]
        assert len(citations) >= 1, "AC14/A2: expected >=1 citation, got 0"

        # ── A3: every cited ticker is inside the corpus allowlist ────────────
        cited_tickers = {c.ticker for c in citations}
        stray_cited = sorted(cited_tickers - ctx.allowed_tickers)
        assert not stray_cited, f"AC14/A3: citation tickers outside allowed_tickers: {stray_cited}"

        # ── A4: every PLANNED ticker is inside the corpus allowlist ──────────
        # query_plan is the sole home of the resolved tickers (D2). An early exit can leave it
        # unset, so read it defensively (run_query.py:67) rather than KeyError-ing on the way out.
        plan = final_state.get("query_plan")
        assert plan is not None, "AC14/A4: plan_node must set query_plan"
        stray_planned = sorted(t for t in plan.tickers if t not in ctx.allowed_tickers)
        assert not stray_planned, (
            f"AC14/A4: planned tickers outside allowed_tickers: {stray_planned}"
        )

        # ── A5: the D1/D3 honesty labels are in their declared domains ───────
        confidence = final_state["confidence"]
        confidence_reason = final_state["confidence_reason"]
        assert confidence in {"low", "high"}, f"AC14/A5: unexpected confidence {confidence!r}"
        assert confidence_reason in {"coverage", "llm", "none"}, (
            f"AC14/A5: unexpected confidence_reason {confidence_reason!r}"
        )

        # ── A6: pair closure -- every planned cell is accounted for ──────────
        cited_pairs = {(c.ticker, c.period_year) for c in citations}
        declared_missing = (
            set(final_state["coverage_gaps"])
            | set(final_state["capacity_drops"])
            | set(final_state["dropped_for_capacity"])
        )
        unavailable_tickers = set(final_state["unavailable_tickers"])
        unavailable_years = set(final_state["unavailable_years"])

        unaccounted = sorted(
            (ticker, year)
            for ticker in plan.tickers
            for year in plan.time_range.years
            if (ticker, year) not in cited_pairs
            and (ticker, year) not in declared_missing
            and ticker not in unavailable_tickers
            and year not in unavailable_years
        )
        assert not unaccounted, (
            "AC14/A6: planned (ticker, year) pairs neither cited nor declared missing by any "
            f"rail: {unaccounted} "
            f"(cited={sorted(cited_pairs)} coverage_gaps={final_state['coverage_gaps']} "
            f"capacity_drops={final_state['capacity_drops']} "
            f"dropped_for_capacity={final_state['dropped_for_capacity']} "
            f"unavailable_tickers={final_state['unavailable_tickers']} "
            f"unavailable_years={final_state['unavailable_years']})"
        )
    finally:
        await pool.close()  # test owns teardown -- mirror run_query.py (G7)
