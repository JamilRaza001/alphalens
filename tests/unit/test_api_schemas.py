"""Unit tests for src/alphalens/api/schemas.py (S18) -- AC8, AC9.

Pure wire-shape tests: no ASGI app, no DB, no Groq. The footer-parity test (AC8) drives the
REAL ``scripts/run_query.py`` harness with fakes and compares its printed footer against
``MetaEvent.from_state`` for the same run, so the two surfaces cannot drift silently -- this is
the exact drift class that produced the ``unavailable=`` / ``unavailable_tickers`` bug
(37e4b41). ``scripts`` is importable as an implicit namespace package from the repo root.

asyncio_mode=auto: async tests run without @pytest.mark.asyncio.
"""

from __future__ import annotations

import ast
import re
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest

from alphalens.agent.state import AgentState, Citation, QueryPlan, TimeRange
from alphalens.api.schemas import CitationOut, GapCell, MetaEvent

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakePool:
    """Minimal asyncpg Pool stand-in: the harness only ever closes it."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _FakeGraph:
    """Compiled-graph stand-in: ``ainvoke`` returns a canned final state, ignoring the intake."""

    def __init__(self, state: AgentState) -> None:
        self._state = state
        self.intakes: list[Any] = []

    async def ainvoke(self, intake: Any, *, context: Any = None) -> AgentState:
        self.intakes.append(intake)
        return self._state


# ── Builders ──────────────────────────────────────────────────────────────────


async def _stream(*tokens: str) -> AsyncGenerator[str, None]:
    for token in tokens:
        yield token


def _plan(tickers: list[str]) -> QueryPlan:
    return QueryPlan(
        tickers=tickers,
        intent="factual",
        time_range=TimeRange(years=[2023]),
        sub_questions=["revenue"],
        entities=[],
        unresolved_companies=[],
    )


def _citation(chunk_id: str = "c1") -> Citation:
    return Citation(
        chunk_id=chunk_id,
        ticker="AAPL",
        filing_type="10-K",
        period_year=2023,
        section="Item 7",
    )


def _base_state() -> dict[str, Any]:
    """A fully-populated final state, deliberately non-empty in every honesty-rail field so the
    parity comparison exercises real values rather than a row of empty lists."""
    return {
        "original_query": "How did AAPL revenue trend?",
        "request_id": "req-1",
        "user_id": None,
        "query_plan": _plan(["AAPL", "MSFT"]),
        "unavailable_tickers": ["TSLA"],
        "unavailable_companies": ["Coca-Cola"],
        "unavailable_years": [20232024],
        "query": "How did AAPL revenue trend?",
        "iteration": 0,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "dropped_for_capacity": [("MSFT", 2022)],
        "confidence": "low",
        "confidence_reason": "coverage",
        "coverage_gaps": [("AAPL", 2024), ("MSFT", 2023)],
        "capacity_drops": [("MSFT", 2022)],
        "citations": [_citation()],
        "answer_stream": _stream("hello ", "world"),
    }


def _state(**overrides: Any) -> AgentState:
    base = _base_state()
    base.update(overrides)
    return cast(AgentState, base)


def _state_without_plan() -> AgentState:
    """The early-exit shape: every key present EXCEPT ``query_plan`` (AC9)."""
    base = _base_state()
    del base["query_plan"]
    return cast(AgentState, base)


# ── Footer parsing ────────────────────────────────────────────────────────────

# The footer is one space-joined line of `key=value` pairs whose values contain spaces
# (`[('AAPL', 2024)]`), so it is split on the boundary BEFORE a `key=`, not on whitespace.
_FOOTER_SPLIT = re.compile(r"\s(?=[a-z_]+=)")

# The one label that differs between the surfaces: the footer prints `reason=` while both the
# state key and the wire field are `confidence_reason` (S18 G4). The label is not the contract.
_FOOTER_TO_META = {"reason": "confidence_reason"}


def _parse_footer(captured: str) -> dict[str, str]:
    line = next(ln for ln in captured.splitlines() if ln.startswith("confidence="))
    return dict(
        cast(tuple[str, str], tuple(part.split("=", 1))) for part in _FOOTER_SPLIT.split(line)
    )


async def _run_harness(monkeypatch: pytest.MonkeyPatch, state: AgentState) -> None:
    """Run the REAL run_query.py harness against fakes -- no Neon, no Groq."""
    import scripts.run_query as harness

    pool = _FakePool()

    async def _fake_build_context() -> tuple[Any, Any]:
        return object(), pool

    monkeypatch.setattr(harness, "build_context", _fake_build_context)
    monkeypatch.setattr(harness, "build_graph", lambda: _FakeGraph(state))

    await harness.run_query("How did AAPL revenue trend?")
    assert pool.close_calls == 1  # the harness still owns teardown (S16 G7)


# ── AC8: footer parity (INTERSECTION) ─────────────────────────────────────────


async def test_footer_and_meta_carry_identical_values_for_the_same_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC8: every field in the INTERSECTION of MetaEvent and the footer agrees, value-for-value,
    for one and the same final state."""
    state = _state()
    await _run_harness(monkeypatch, state)
    footer = _parse_footer(capsys.readouterr().out)

    meta = MetaEvent.from_state(state, request_id="req-parity")

    assert footer["confidence"] == meta.confidence
    assert footer["reason"] == meta.confidence_reason
    assert ast.literal_eval(footer["coverage_gaps"]) == [
        (g.ticker, g.year) for g in meta.coverage_gaps
    ]
    assert ast.literal_eval(footer["capacity_drops"]) == [
        (g.ticker, g.year) for g in meta.capacity_drops
    ]
    assert ast.literal_eval(footer["unavailable_tickers"]) == meta.unavailable_tickers
    assert ast.literal_eval(footer["unavailable_years"]) == meta.unavailable_years
    assert ast.literal_eval(footer["unavailable_companies"]) == meta.unavailable_companies
    assert ast.literal_eval(footer["plan_tickers"]) == meta.plan_tickers


async def test_footer_and_meta_key_sets_differ_only_by_the_documented_exceptions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC8: the two key sets are identical once `latency` (footer-only, rides on
    DoneEvent.latency_s) and `request_id` (meta-only) are removed. A new state key surfaced on
    only one of the two surfaces fails HERE, not in the browser."""
    await _run_harness(monkeypatch, _state())
    footer = _parse_footer(capsys.readouterr().out)

    assert "latency" in footer, "footer must still print latency"
    assert "request_id" not in footer, "footer must still NOT print request_id"

    mapped = {_FOOTER_TO_META.get(k, k) for k in footer if k != "latency"}
    assert mapped == set(MetaEvent.model_fields) - {"request_id"}


# ── AC9: plan_tickers normalisation ───────────────────────────────────────────


def test_plan_tickers_is_empty_list_when_query_plan_unset() -> None:
    """AC9: `[]`, never `None` -- a wire contract that changes type on an edge case forces every
    consumer into a null check."""
    meta = MetaEvent.from_state(_state_without_plan(), request_id="req-1")
    assert meta.plan_tickers == []


def test_plan_tickers_passes_through_when_query_plan_is_set() -> None:
    meta = MetaEvent.from_state(_state(), request_id="req-1")
    assert meta.plan_tickers == ["AAPL", "MSFT"]


async def test_wire_diverges_from_cli_on_the_unset_plan_edge_case(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC9: the divergence is DELIBERATE and pinned -- the CLI keeps printing `None`, the wire
    normalises to `[]`. If someone 'fixes' the CLI to match, this test says so."""
    state = _state_without_plan()
    await _run_harness(monkeypatch, state)
    footer = _parse_footer(capsys.readouterr().out)

    assert footer["plan_tickers"] == "None"
    assert MetaEvent.from_state(state, request_id="req-1").plan_tickers == []


# ── Pure converters (Q3 wire shape) ───────────────────────────────────────────


def test_gap_cell_serialises_as_a_named_object_not_a_positional_array() -> None:
    """Q3: `{"ticker": ..., "year": ...}` -- never `["AAPL", 2024]`."""
    assert GapCell.from_pair(("AAPL", 2024)).model_dump() == {"ticker": "AAPL", "year": 2024}


def test_meta_gap_lists_convert_every_live_tuple() -> None:
    meta = MetaEvent.from_state(_state(), request_id="req-1")
    assert [(g.ticker, g.year) for g in meta.coverage_gaps] == [("AAPL", 2024), ("MSFT", 2023)]
    assert [(g.ticker, g.year) for g in meta.capacity_drops] == [("MSFT", 2022)]


def test_citation_out_is_field_identical_to_the_live_citation() -> None:
    citation = _citation("c-42")
    out = CitationOut.from_citation(citation)
    assert out.model_dump() == {
        "chunk_id": "c-42",
        "ticker": "AAPL",
        "filing_type": "10-K",
        "period_year": 2023,
        "section": "Item 7",
    }
    assert set(CitationOut.model_fields) == set(Citation.model_fields)


def test_meta_never_carries_dropped_for_capacity() -> None:
    """Out of Scope (Q4): `capacity_drops` is on the wire for footer parity; the key that
    synthesize_node actually reads stays off it."""
    assert "dropped_for_capacity" not in MetaEvent.model_fields
