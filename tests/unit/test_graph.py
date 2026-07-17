"""S16 -- pure topology test for the compiled agent graph.

Uses NO DB / LLM / reranker / pool: proves ``graph.py`` is import-safe and topology-only
(S16/D1a, AC1-AC3). Resource assembly + live behavior are covered by ``context.py`` and the
live harness, not here.
"""

from __future__ import annotations

from langgraph.graph.state import CompiledStateGraph

from alphalens.agent.graph import build_graph

_BUSINESS_NODES = {"plan", "retrieve", "rerank", "evaluate", "synthesize"}
_EXPECTED_EDGES = {
    ("__start__", "plan"),
    ("plan", "retrieve"),
    ("retrieve", "rerank"),
    ("rerank", "evaluate"),
    ("evaluate", "synthesize"),
    ("synthesize", "__end__"),
}


def test_build_graph_topology() -> None:
    """The 5 nodes and the single linear START->...->END chain are present; no conditionals."""
    graph = build_graph()
    assert isinstance(graph, CompiledStateGraph)

    drawable = graph.get_graph()
    business = set(drawable.nodes) - {"__start__", "__end__"}
    assert business == _BUSINESS_NODES

    edges = {(e.source, e.target) for e in drawable.edges}
    assert edges == _EXPECTED_EDGES

    # AC2: strictly linear -- no conditional (branching) edges anywhere.
    assert not any(getattr(e, "conditional", False) for e in drawable.edges)


def test_graph_compiled_without_checkpointer() -> None:
    """AC3/D2: v1 is a stateless single-pass graph -- compiled without a checkpointer."""
    assert build_graph().checkpointer is None


def test_build_graph_is_pure_and_repeatable() -> None:
    """AC1: build_graph takes no resources and can be called repeatedly with no side effects
    (no DB/LLM/pool needed to construct the topology)."""
    first = build_graph()
    second = build_graph()
    assert first is not second
    assert set(first.get_graph().nodes) == set(second.get_graph().nodes)
