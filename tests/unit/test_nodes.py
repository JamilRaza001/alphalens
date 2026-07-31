"""Unit tests for src/alphalens/agent/nodes.py (S13).

Every node is exercised in isolation with a fake ``AgentContext`` (fake llm, fake
reranker) and a hand-built ``AgentState`` -- no live Groq / DB / graph. Retrieve's
per-cell fan-out is covered separately in ``test_retrieve_node.py`` (S15). No
end-to-end/integration test until S16 graph wiring lands (per spec Gotcha).

asyncio_mode=auto: async tests run without @pytest.mark.asyncio.
Runtime construction: langgraph 1.2.1's Runtime is a dataclass with all fields
defaulted, so ``Runtime(context=ctx)`` is the minimal standalone shim.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from typing import Any, cast

import pytest
from asyncpg import Pool
from langchain_groq import ChatGroq
from langgraph.runtime import Runtime
from sentence_transformers import CrossEncoder

from alphalens.agent.circuit_breaker import SynthesisCircuitBreaker
from alphalens.agent.nodes import (
    LOW_CONFIDENCE_CAVEAT,
    AgentContext,
    compute_coverage_gaps,
    evaluate_node,
    plan_node,
    plan_to_cells,
    rerank_node,
    split_concatenated_years,
    synthesize_node,
    validate_companies,
    validate_tickers,
    validate_years,
)
from alphalens.agent.state import (
    AgentState,
    EvalVerdict,
    QueryPlan,
    RetrievedChunk,
    ScoredChunk,
    TimeRange,
)
from alphalens.config import get_settings
from alphalens.etl.embeddings import EmbeddingClient

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeStructured:
    """Stand-in for the Runnable returned by ``with_structured_output``.

    ``ainvoke`` returns a preset boundary object and records that it was called, so
    tests can assert the Evaluate LLM branch was (or was NOT) taken.
    """

    def __init__(self, result: Any, calls: list[Any]) -> None:
        self._result = result
        self._calls = calls

    async def ainvoke(self, messages: Any) -> Any:
        self._calls.append(messages)
        return self._result


class _FakeChunk:
    """Minimal AIMessageChunk stand-in for astream (only ``.content`` is read)."""

    def __init__(self, content: Any) -> None:
        self.content = content


class _FakeLLM:
    """Fake ChatGroq: structured extraction + a streaming generator with a blank delta."""

    def __init__(self, structured_result: Any = None) -> None:
        self._structured_result = structured_result
        self.structured_calls: list[Any] = []
        self.with_structured_output_kwargs: dict[str, Any] | None = None

    def model_copy(self, *, update: dict[str, Any] | None = None) -> _FakeLLM:
        # S16/D-temp: plan_node/evaluate_node call model_copy(update={"temperature": 0.0})
        # before with_structured_output. Return self so structured-call recording survives.
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeStructured:
        self.with_structured_output_kwargs = kwargs
        return _FakeStructured(self._structured_result, self.structured_calls)

    async def astream(self, messages: Any) -> AsyncIterator[_FakeChunk]:
        # Include an empty delta to prove stream_synthesis filters it out.
        for content in ["Hello", "", " world", None]:
            yield _FakeChunk(content)


class _FakeReranker:
    """Fake CrossEncoder: returns descending scores in input order (i-th -> len-i)."""

    def __init__(self) -> None:
        self.predict_calls: list[Any] = []

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.predict_calls.append(pairs)
        return [float(len(pairs) - i) for i in range(len(pairs))]


# ── Builders ──────────────────────────────────────────────────────────────────

_ALLOWED = frozenset({"AAPL", "MSFT", "GOOGL"})

# The REAL production roster shape: values are LEGAL names, mirroring scripts/seed_companies.py.
# The company rail is meaningless against a ticker-valued roster, so it is tested against this.
_SEED_ROSTER: Mapping[str, str] = {
    "AAPL": "Apple Inc.",
    "MSFT": "Microsoft Corporation",
    "GOOGL": "Alphabet Inc.",
    "AMZN": "Amazon.com Inc.",
    "NVDA": "NVIDIA Corporation",
    "META": "Meta Platforms Inc.",
    "TSLA": "Tesla Inc.",
    "BRK-B": "Berkshire Hathaway Inc.",
    "JPM": "JPMorgan Chase & Co.",
    "V": "Visa Inc.",
}


def _ctx(
    *,
    llm: Any = None,
    reranker: Any = None,
    allowed: frozenset[str] = _ALLOWED,
    roster: Mapping[str, str] | None = None,
    breaker: SynthesisCircuitBreaker | None = None,
    embedder: Any = None,
    corpus_min_year: int = 2021,
    corpus_max_year: int = 2026,
) -> AgentContext:
    # Fakes stand in for the real deps; cast to satisfy AgentContext's typed fields.
    return AgentContext(
        llm=cast(ChatGroq, llm if llm is not None else _FakeLLM()),
        reranker=cast(CrossEncoder, reranker if reranker is not None else _FakeReranker()),
        pool=cast(Pool, object()),  # never touched by these nodes (retrieve tested separately)
        allowed_tickers=allowed,
        # Fresh breaker -> CLOSED, so synthesize passes the real LLM stream through untouched.
        breaker=breaker
        if breaker is not None
        else SynthesisCircuitBreaker(failure_threshold=3, reset_timeout_seconds=30.0),
        # retrieve_node is exercised in test_retrieve_node.py; a stand-in suffices here.
        embedder=cast(EmbeddingClient, embedder if embedder is not None else object()),
        # S16: roster keys mirror the allowlist. Values are TICKERS here, not legal names --
        # company-rail tests pass an explicit legal-name roster instead (see _SEED_ROSTER).
        ticker_roster=roster if roster is not None else {t: t for t in allowed},
        # Plan year-rail bounds: injected here (not read from env) so the rail's tests are
        # independent of the deployed corpus window.
        corpus_min_year=corpus_min_year,
        corpus_max_year=corpus_max_year,
    )


def _runtime(ctx: AgentContext) -> Runtime[AgentContext]:
    return Runtime(context=ctx)


def _chunk(chunk_id: str, ticker: str, year: int, text: str = "evidence") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=text,
        section="Item 7",
        ticker=ticker,
        period_year=year,
        filing_type="10-K",
    )


def _plan(
    tickers: list[str], years: list[int], unresolved_companies: list[str] | None = None
) -> QueryPlan:
    return QueryPlan(
        tickers=tickers,
        intent="factual",
        time_range=TimeRange(years=years),
        sub_questions=["q"],
        entities=[],
        unresolved_companies=unresolved_companies or [],
    )


def _state(**overrides: Any) -> AgentState:
    base: dict[str, Any] = {
        "original_query": "How did AAPL revenue trend?",
        "request_id": "req-1",
        "user_id": None,
        "query_plan": _plan(["AAPL"], [2023]),
        "unavailable_tickers": [],
        "unavailable_companies": [],
        "unavailable_years": [],
        "query": "How did AAPL revenue trend?",
        "iteration": 0,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "dropped_for_capacity": [],
        "confidence": "high",
        "confidence_reason": "none",
        "coverage_gaps": [],
        "capacity_drops": [],
        "citations": [],
        "answer_stream": None,
    }
    base.update(overrides)
    return cast(AgentState, base)


# ── Pure helpers ──────────────────────────────────────────────────────────────


def test_validate_tickers_split() -> None:
    kept, dropped = validate_tickers(["AAPL", "TSLA", "MSFT"], _ALLOWED)
    assert kept == ["AAPL", "MSFT"]
    assert dropped == ["TSLA"]


def test_validate_companies_confirms_out_of_corpus() -> None:
    # The S5b case: a genuinely absent company must survive the gate and reach the disclosure.
    confirmed, false_pos = validate_companies(["Coca-Cola"], _SEED_ROSTER)
    assert confirmed == ["Coca-Cola"]
    assert false_pos == []


def test_validate_companies_catches_legal_suffix_variance() -> None:
    # The one class this gate closes: the model writes the user's wording, the roster holds the
    # legal name. "Apple" must NOT be reported as out-of-corpus.
    confirmed, false_pos = validate_companies(["Apple"], {"AAPL": "Apple Inc."})
    assert confirmed == []
    assert false_pos == ["Apple"]


def test_validate_companies_is_casefolded() -> None:
    confirmed, false_pos = validate_companies(["apple", "APPLE"], _SEED_ROSTER)
    assert confirmed == []
    assert false_pos == ["apple", "APPLE"]


def test_validate_companies_short_needle_guard() -> None:
    # _MIN_COMPANY_PREFIX: "V" must not prefix-match "Visa Inc.". Below the threshold the
    # comparison degrades to exact equality, so the name flows through as confirmed.
    confirmed, false_pos = validate_companies(["V"], _SEED_ROSTER)
    assert confirmed == ["V"]
    assert false_pos == []


def test_validate_companies_residual_semantic_alias() -> None:
    # RESIDUAL (deferred to the v2 alias table), asserted as-is -- NOT a fix.
    # Under-match -> false CONFIRM: GOOGL is in the corpus, but "Google" does not match
    # "Alphabet Inc.", so the footer would deny a company the body can cite.
    confirmed, false_pos = validate_companies(["Google"], _SEED_ROSTER)
    assert confirmed == ["Google"]
    assert false_pos == []


def test_validate_companies_residual_punctuation_whitespace() -> None:
    # RESIDUAL (deferred to the v2 alias table), asserted as-is -- NOT a fix.
    # Under-match -> false CONFIRM: casefold() normalizes case only, and startswith is
    # left-anchored, so "jp morgan" diverges from "jpmorgan chase & co." at index 2.
    confirmed, false_pos = validate_companies(["JP Morgan"], _SEED_ROSTER)
    assert confirmed == ["JP Morgan"]
    assert false_pos == []


def test_validate_companies_residual_prefix_over_match() -> None:
    # RESIDUAL (deferred to the v2 alias table), asserted as-is -- NOT a fix.
    # Over-match -> false SUPPRESS, the OPPOSITE direction and the more dangerous one: a
    # non-roster company named "Alpha" prefix-matches "Alphabet Inc.", so it is discarded and
    # the user never sees the disclosure. That is the S5b silent drop, via this very gate.
    confirmed, false_pos = validate_companies(["Alpha"], _SEED_ROSTER)
    assert confirmed == []
    assert false_pos == ["Alpha"]


def test_split_concatenated_years_repairs_pair() -> None:
    # The exact live failure: "2023 vs 2024" decoded as one integer.
    assert split_concatenated_years([20232024]) == [2023, 2024]


def test_split_concatenated_years_repairs_triple() -> None:
    assert split_concatenated_years([202320242025]) == [2023, 2024, 2025]


def test_split_concatenated_years_passes_clean_years() -> None:
    # Already-correct output must survive the repair untouched.
    assert split_concatenated_years([2023, 2024]) == [2023, 2024]


def test_split_concatenated_years_leaves_unrepairable() -> None:
    # 5 digits -- not a multiple of 4, so not a concatenation. Left for validate_years to drop.
    assert split_concatenated_years([12345]) == [12345]
    kept, dropped = validate_years([12345], min_year=2021, max_year=2026)
    assert kept == []
    assert dropped == [12345]


def test_split_concatenated_years_dedups_order_preserving() -> None:
    # The repair can CREATE duplicates; a dup year would multiply the retrieval fan-out.
    assert split_concatenated_years([2023, 20232024]) == [2023, 2024]


def test_validate_years_split() -> None:
    # 20232024 is the real observed failure: the LLM concatenating "2023 vs 2024" into one int.
    # 1999 is plausibly-shaped but outside the corpus window -- both are dropped.
    kept, dropped = validate_years([20232024, 2023, 1999], min_year=2021, max_year=2026)
    assert kept == [2023]
    assert dropped == [20232024, 1999]


def test_validate_years_boundaries_inclusive() -> None:
    kept, dropped = validate_years([2021, 2026, 2020, 2027], min_year=2021, max_year=2026)
    assert kept == [2021, 2026]  # bounds are inclusive
    assert dropped == [2020, 2027]


def test_compute_coverage_gaps_missing_cell() -> None:
    plan = _plan(["AAPL", "MSFT"], [2023])
    reranked = [ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)]
    assert compute_coverage_gaps(plan, reranked) == [("MSFT", 2023)]


def test_compute_coverage_gaps_discrete_years() -> None:
    # [2022, 2024] must NEVER report 2023 as a gap (discrete years, not a range).
    plan = _plan(["AAPL"], [2022, 2024])
    reranked = [
        ScoredChunk(chunk=_chunk("c1", "AAPL", 2022), rerank_score=1.0),
        ScoredChunk(chunk=_chunk("c2", "AAPL", 2024), rerank_score=1.0),
    ]
    assert compute_coverage_gaps(plan, reranked) == []


def test_compute_coverage_gaps_full_coverage_empty() -> None:
    plan = _plan(["AAPL"], [2023])
    reranked = [ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)]
    assert compute_coverage_gaps(plan, reranked) == []


# ── Plan node ─────────────────────────────────────────────────────────────────


async def test_plan_node_drops_unavailable() -> None:
    # LLM proposes an out-of-corpus ticker (TSLA) -> input-rail drops it.
    llm = _FakeLLM(structured_result=_plan(["AAPL", "TSLA"], [2023]))
    ctx = _ctx(llm=llm)
    out = await plan_node(_state(), _runtime(ctx))

    # partial update only
    assert set(out.keys()) == {
        "query_plan",
        "unavailable_tickers",
        "unavailable_companies",
        "unavailable_years",
    }
    assert out["query_plan"].tickers == ["AAPL"]  # dropped TSLA
    assert all(t in _ALLOWED for t in out["query_plan"].tickers)
    assert out["unavailable_tickers"] == ["TSLA"]
    # D3/D4: strict json_schema constrained decoding requested.
    assert llm.with_structured_output_kwargs == {"method": "json_schema", "strict": True}


async def test_plan_node_surfaces_unresolved_companies() -> None:
    # The path that actually matters: a company the model could not resolve reaches the state
    # key Synthesize reads, by NAME ("Coca-Cola") -- not as a ticker, and not silently dropped.
    llm = _FakeLLM(structured_result=_plan(["AAPL"], [2024], ["Coca-Cola"]))
    out = await plan_node(_state(), _runtime(_ctx(llm=llm, roster=_SEED_ROSTER)))

    assert out["unavailable_companies"] == ["Coca-Cola"]
    assert out["query_plan"].unresolved_companies == ["Coca-Cola"]
    assert out["query_plan"].tickers == ["AAPL"]  # the resolvable half is untouched


async def test_plan_node_always_returns_unavailable_companies_key() -> None:
    # Required TypedDict key: present on EVERY return path, [] when there is nothing to report.
    llm = _FakeLLM(structured_result=_plan(["AAPL"], [2024]))
    out = await plan_node(_state(), _runtime(_ctx(llm=llm, roster=_SEED_ROSTER)))

    assert out["unavailable_companies"] == []


async def test_plan_node_discards_false_positive_companies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The model contradicts itself: it lists an IN-roster company as unresolved. Filter and
    # log -- never promote into tickers, which would invent a retrieval cell nobody asked for.
    llm = _FakeLLM(structured_result=_plan([], [2024], ["Apple"]))
    with caplog.at_level(logging.WARNING, logger="alphalens.agent.nodes"):
        out = await plan_node(_state(), _runtime(_ctx(llm=llm, roster=_SEED_ROSTER)))

    assert out["unavailable_companies"] == []  # discarded, not disclosed
    assert out["query_plan"].tickers == []  # and NOT promoted
    assert "Apple" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_plan_node_drops_malformed_year() -> None:
    # Retargeted: 20232024 is now REPAIRED (see test_plan_node_repairs_concatenated_year), so
    # the drop-and-flag path is exercised with genuinely unrepairable garbage -- 5 digits, not
    # a multiple of 4, so the repair leaves it for the rail to drop and surface.
    llm = _FakeLLM(structured_result=_plan(["AAPL"], [12345]))
    out = await plan_node(_state(), _runtime(_ctx(llm=llm)))

    assert out["query_plan"].time_range.years == []  # dropped
    assert out["unavailable_years"] == [12345]  # and surfaced, not swallowed


async def test_plan_node_repairs_concatenated_year() -> None:
    # The live bug, end to end: the model emits [20232024]; the repair must turn that into two
    # real years that reach retrieval, with nothing flagged -- i.e. the user gets a real answer
    # instead of an honest "no coverage".
    llm = _FakeLLM(structured_result=_plan(["AAPL"], [20232024]))
    out = await plan_node(_state(), _runtime(_ctx(llm=llm)))

    assert out["query_plan"].time_range.years == [2023, 2024]  # repaired, in bounds
    assert out["unavailable_years"] == []  # nothing dropped -- the query now works
    # ...and both years actually reach cell-building (the fan-out retrieval consumes).
    assert plan_to_cells(out["query_plan"]) == [("AAPL", 2023, "q"), ("AAPL", 2024, "q")]


async def test_plan_node_keeps_valid_years() -> None:
    # The correctly-decoded form of the same query passes through untouched.
    llm = _FakeLLM(structured_result=_plan(["AAPL"], [2023, 2024]))
    out = await plan_node(_state(), _runtime(_ctx(llm=llm)))

    assert out["query_plan"].time_range.years == [2023, 2024]
    assert out["unavailable_years"] == []


# ── Retrieve node: real per-cell fan-out lives in test_retrieve_node.py (S15) ──
# The former stub (NotImplementedError) is gone -- retrieve_node is implemented in S15.


# ── Rerank node ───────────────────────────────────────────────────────────────


async def test_rerank_node_sorts_and_caps() -> None:
    # S17: a single-pair pool overflowing the cap is capped at max_context_chunks and still
    # ordered best-first. (Per-pair floor selection has its own suite: test_selection_floor.py.)
    reranker = _FakeReranker()
    ctx = _ctx(reranker=reranker)
    cap = get_settings().max_context_chunks
    n = cap + 3
    chunks = [_chunk(f"c{i:02d}", "AAPL", 2023) for i in range(n)]
    out = await rerank_node(_state(retrieved_chunks=chunks), _runtime(ctx))

    scored = out["reranked_chunks"]
    assert len(scored) == cap  # capped at settings.max_context_chunks
    # descending by rerank_score
    assert scored == sorted(scored, key=lambda s: s.rerank_score, reverse=True)
    # highest score first -> the fake gives chunk[0] the top score
    assert scored[0].chunk.chunk_id == "c00"
    assert out["dropped_for_capacity"] == []  # one pair, never a coverage drop
    assert reranker.predict_calls  # went through the reranker (off-thread)


async def test_rerank_node_empty_input() -> None:
    out = await rerank_node(_state(retrieved_chunks=[]), _runtime(_ctx()))
    assert out == {"reranked_chunks": [], "dropped_for_capacity": []}


# ── Evaluate node (three branches + precedence) ───────────────────────────────


async def test_evaluate_node_coverage_precedence() -> None:
    # A missing (MSFT, 2023) cell forces low/coverage WITHOUT any LLM call.
    llm = _FakeLLM(structured_result=EvalVerdict(reasoning="unused", sufficient=True))
    ctx = _ctx(llm=llm)
    state = _state(
        query_plan=_plan(["AAPL", "MSFT"], [2023]),
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
    )
    out = await evaluate_node(state, _runtime(ctx))

    assert out == {
        "confidence": "low",
        "confidence_reason": "coverage",
        "coverage_gaps": [("MSFT", 2023)],
        "capacity_drops": [],
    }
    assert llm.structured_calls == []  # LLM sufficiency NOT invoked (precedence)


async def test_evaluate_node_llm_insufficient() -> None:
    llm = _FakeLLM(structured_result=EvalVerdict(reasoning="too thin", sufficient=False))
    ctx = _ctx(llm=llm)
    state = _state(
        query_plan=_plan(["AAPL"], [2023]),
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
    )
    out = await evaluate_node(state, _runtime(ctx))

    assert out == {
        "confidence": "low",
        "confidence_reason": "llm",
        "coverage_gaps": [],
        "capacity_drops": [],
    }
    assert len(llm.structured_calls) == 1  # LLM WAS consulted (no gaps)


async def test_evaluate_node_high() -> None:
    llm = _FakeLLM(structured_result=EvalVerdict(reasoning="complete", sufficient=True))
    ctx = _ctx(llm=llm)
    state = _state(
        query_plan=_plan(["AAPL"], [2023]),
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
    )
    out = await evaluate_node(state, _runtime(ctx))

    assert out == {
        "confidence": "high",
        "confidence_reason": "none",
        "coverage_gaps": [],
        "capacity_drops": [],
    }


# ── Evaluate node: capacity-drop vs coverage-gap split (S_C1) ──────────────────


async def test_evaluate_node_capacity_only_no_coverage() -> None:
    # AC1 (S4 regression guard): every raw miss is a capacity drop, so there is NO true
    # coverage gap -> Evaluate must NOT take the coverage-precedence path. It proceeds to the
    # LLM branch and returns the LLM's verdict; capacity_drops holds the trimmed pairs.
    llm = _FakeLLM(structured_result=EvalVerdict(reasoning="enough", sufficient=True))
    ctx = _ctx(llm=llm)
    state = _state(
        query_plan=_plan(["AAPL", "MSFT"], [2023]),
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
        dropped_for_capacity=[("MSFT", 2023)],  # the only miss was budget-trimmed
    )
    out = await evaluate_node(state, _runtime(ctx))

    assert out == {
        "confidence": "high",
        "confidence_reason": "none",
        "coverage_gaps": [],
        "capacity_drops": [("MSFT", 2023)],
    }
    assert out["confidence_reason"] != "coverage"  # NEVER the coverage path
    assert len(llm.structured_calls) == 1  # LLM WAS consulted (capacity is not a hard gap)


async def test_evaluate_node_true_gap() -> None:
    # AC2: a miss NOT in dropped_for_capacity is a real evidence gap -> low/coverage, LLM skipped.
    llm = _FakeLLM(structured_result=EvalVerdict(reasoning="unused", sufficient=True))
    ctx = _ctx(llm=llm)
    state = _state(
        query_plan=_plan(["AAPL", "MSFT"], [2023]),
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
        dropped_for_capacity=[],  # nothing was trimmed -> the miss is genuine
    )
    out = await evaluate_node(state, _runtime(ctx))

    assert out == {
        "confidence": "low",
        "confidence_reason": "coverage",
        "coverage_gaps": [("MSFT", 2023)],
        "capacity_drops": [],
    }
    assert llm.structured_calls == []  # precedence -> LLM sufficiency NOT invoked


async def test_evaluate_node_mixed_gap_and_capacity() -> None:
    # AC3: one true gap + one capacity drop. coverage precedence still fires on the true gap;
    # the two lists are disjoint and together partition raw_gaps.
    llm = _FakeLLM(structured_result=EvalVerdict(reasoning="unused", sufficient=True))
    ctx = _ctx(llm=llm)
    # needed = {AAPL,MSFT,GOOGL} x {2023}; present = {AAPL}; raw_gaps = {MSFT, GOOGL}.
    state = _state(
        query_plan=_plan(["AAPL", "MSFT", "GOOGL"], [2023]),
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
        dropped_for_capacity=[("GOOGL", 2023)],  # GOOGL trimmed; MSFT genuinely missing
    )
    out = await evaluate_node(state, _runtime(ctx))

    assert out == {
        "confidence": "low",
        "confidence_reason": "coverage",
        "coverage_gaps": [("MSFT", 2023)],
        "capacity_drops": [("GOOGL", 2023)],
    }
    assert llm.structured_calls == []  # true gap present -> precedence, LLM skipped
    # disjoint, and together partition the raw misses.
    coverage = set(out["coverage_gaps"])
    capacity = set(out["capacity_drops"])
    assert coverage.isdisjoint(capacity)
    assert coverage | capacity == {("MSFT", 2023), ("GOOGL", 2023)}


async def test_evaluate_node_no_gap_capacity_empty() -> None:
    # AC4: no raw misses at all -> LLM branch exactly as pre-fix; capacity_drops present + empty.
    llm = _FakeLLM(structured_result=EvalVerdict(reasoning="complete", sufficient=True))
    ctx = _ctx(llm=llm)
    state = _state(
        query_plan=_plan(["AAPL"], [2023]),
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
    )
    out = await evaluate_node(state, _runtime(ctx))

    assert out == {
        "confidence": "high",
        "confidence_reason": "none",
        "coverage_gaps": [],
        "capacity_drops": [],
    }
    assert len(llm.structured_calls) == 1


# ── Synthesize node ───────────────────────────────────────────────────────────


async def test_synthesize_node_citations_and_stream() -> None:
    ctx = _ctx(llm=_FakeLLM())
    reranked = [
        ScoredChunk(chunk=_chunk("c1", "AAPL", 2023, "rev up"), rerank_score=2.0),
        ScoredChunk(chunk=_chunk("c2", "AAPL", 2022, "rev flat"), rerank_score=1.0),
    ]
    state = _state(reranked_chunks=reranked, confidence="high")
    out = await synthesize_node(state, _runtime(ctx))

    # One Citation per reranked chunk, fields copied 1:1.
    citations = out["citations"]
    assert len(citations) == 2
    assert citations[0].chunk_id == "c1"
    assert citations[0].ticker == "AAPL"
    assert citations[0].filing_type == "10-K"
    assert citations[0].period_year == 2023
    assert citations[0].section == "Item 7"

    # answer_stream is an async generator; draining skips the empty/None deltas.
    stream = out["answer_stream"]
    assert isinstance(stream, AsyncGenerator)
    pieces = [p async for p in stream]
    assert pieces == ["Hello", " world"]


async def test_synthesize_node_low_confidence_emits_caveat_first() -> None:
    """S_CR Phase 4: the caveat is code-emitted, so it is the FIRST token, every run."""
    ctx = _ctx(llm=_FakeLLM())
    state = _state(
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
        confidence="low",
    )
    out = await synthesize_node(state, _runtime(ctx))

    pieces = [p async for p in out["answer_stream"]]
    assert pieces[0] == LOW_CONFIDENCE_CAVEAT
    # The LLM stream is delegated to untouched after the caveat.
    assert pieces[1:] == ["Hello", " world"]


async def test_synthesize_node_high_confidence_adds_nothing() -> None:
    """confidence="high" -> the wrapper is not applied at all; stream is byte-identical."""
    ctx = _ctx(llm=_FakeLLM())
    state = _state(
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
        confidence="high",
    )
    out = await synthesize_node(state, _runtime(ctx))

    text = "".join([p async for p in out["answer_stream"]])
    assert LOW_CONFIDENCE_CAVEAT not in text
    assert "confidence in this answer is low" not in text
    assert text == "Hello world"
