"""AlphaLens v8 -- Agent graph wiring (S16).

PURE TOPOLOGY. This module wires the five already-built, decoupled S13/S15 nodes
(Plan -> Retrieve -> Rerank -> Evaluate -> Synthesize, L19) into one linear, single-pass
LangGraph ``StateGraph`` and compiles it WITHOUT a checkpointer (v1 is stateless single-pass).

Importing this module has ZERO side effects: no DB connection, no model load, no ``await``,
no resource construction (S16/D1a). All per-run dependencies are assembled separately in
``context.py`` (the cold-start seam) and injected at invoke time via
``graph.ainvoke(intake, context=ctx)`` -- never at module scope.

Linear-graph lock (D3b): no conditional edges, no Refine loop, no early-``END`` short-circuit.
Total-unavailable / empty queries flow straight through; each node's empty-input handling
(retrieve -> ``[]``, rerank -> ``[]``, evaluate -> low/coverage, synthesize -> honesty rail)
produces an honest low-confidence answer without any extra hop.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from alphalens.agent.nodes import (
    AgentContext,
    evaluate_node,
    plan_node,
    rerank_node,
    retrieve_node,
    synthesize_node,
)
from alphalens.agent.state import AgentState


def build_graph() -> CompiledStateGraph[AgentState, AgentContext, AgentState, AgentState]:
    """Wire the 5 nodes into a linear single-pass graph and compile WITHOUT a checkpointer.

    Pure topology: no DB, no await, no resource construction -- importing this module is
    side-effect-free. ``context_schema=AgentContext`` (LangGraph 1.x -- NOT the deprecated
    ``config_schema``) declares the DI shape; deps are injected later at
    ``graph.ainvoke(..., context=ctx)``.
    """
    builder = StateGraph(AgentState, context_schema=AgentContext)

    builder.add_node("plan", plan_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("rerank", rerank_node)
    builder.add_node("evaluate", evaluate_node)
    builder.add_node("synthesize", synthesize_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "retrieve")
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "evaluate")
    builder.add_edge("evaluate", "synthesize")
    builder.add_edge("synthesize", END)

    return builder.compile()  # no checkpointer -> v1 stateless single-pass
