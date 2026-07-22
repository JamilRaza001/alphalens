"""AlphaLens v8 -- live agent query harness (S16/D4).

The v1 stand-in for the SSE caller: cold-start the agent, build the graph, run one query
end-to-end against LIVE Neon + Groq, drain the streamed answer, and print answer + citations
+ a timing/confidence footer. This is the FIRST real end-to-end run -- the jina-v3
``$1::vector`` binding, the SQL RRF fusion, the ``chunks->filings`` join, and the breaker's
lazy drain all fire here for the first time outside fake pools.

Usage:
    python scripts/run_query.py "How did Apple and Microsoft revenue compare in 2023 vs 2024?"

Needs live creds (Neon + Groq) in the environment/.env. Not run in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import cast
from uuid import uuid4

from alphalens.agent.context import build_context
from alphalens.agent.graph import build_graph
from alphalens.agent.state import AgentState


async def run_query(question: str, *, user_id: str | None = None) -> None:
    """Cold-start -> build graph -> ainvoke -> DRAIN answer_stream (D2) -> print results.

    ``ainvoke`` leaves ``answer_stream`` UN-DRAINED (G3); the breaker gate / degraded fallback
    is evaluated lazily only when the ``async for`` below pulls the first token (S14). The pool
    stays open across the drain (G4) and is closed in the ``finally``.
    """
    ctx, pool = await build_context()
    try:
        graph = build_graph()
        # AgentState is an all-required TypedDict; the remaining keys are filled by the nodes.
        # cast keeps mypy --strict happy with the partial intake literal (only anchors + seeds).
        intake = cast(
            AgentState,
            {
                "original_query": question,
                "query": question,  # == original_query in v1 (L6)
                "request_id": str(uuid4()),
                "user_id": user_id,
                "iteration": 0,
            },
        )

        t0 = time.monotonic()
        final_state = await graph.ainvoke(intake, context=ctx)  # answer_stream stays UN-DRAINED

        print("\n--- ANSWER ---")
        stream = final_state["answer_stream"]
        assert stream is not None, "synthesize_node must set answer_stream"
        async for token in stream:  # D2 drain: breaker gate/fallback evaluated HERE
            print(token, end="", flush=True)

        print("\n\n--- CITATIONS ---")
        for c in final_state["citations"]:
            print(f"  [{c.ticker} {c.filing_type} {c.period_year}] {c.section} ({c.chunk_id})")

        print(
            f"\nconfidence={final_state['confidence']} "
            f"reason={final_state['confidence_reason']} "
            f"coverage_gaps={final_state['coverage_gaps']} "
            f"capacity_drops={final_state['capacity_drops']} "
            f"unavailable={final_state['unavailable_tickers']} "
            f"unavailable_years={final_state['unavailable_years']} "
            f"latency={time.monotonic() - t0:.2f}s"
        )
    finally:
        await pool.close()  # script owns teardown -- mirror etl/runner (G7)


def main() -> None:
    ap = argparse.ArgumentParser(description="AlphaLens live agent query harness (S16).")
    ap.add_argument("question")
    ap.add_argument("--user-id", default=None)
    args = ap.parse_args()
    asyncio.run(run_query(args.question, user_id=args.user_id))


if __name__ == "__main__":
    main()
