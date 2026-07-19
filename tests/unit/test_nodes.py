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

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast

from asyncpg import Pool
from langchain_groq import ChatGroq
from langgraph.runtime import Runtime
from sentence_transformers import CrossEncoder

from alphalens.agent.circuit_breaker import SynthesisCircuitBreaker
from alphalens.agent.nodes import (
    AgentContext,
    compute_coverage_gaps,
    evaluate_node,
    plan_node,
    plan_to_cells,
    rerank_node,
    split_concatenated_years,
    synthesize_node,
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


def _ctx(
    *,
    llm: Any = None,
    reranker: Any = None,
    allowed: frozenset[str] = _ALLOWED,
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
        ticker_roster={t: t for t in allowed},  # S16: roster keys mirror the allowlist
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


def _plan(tickers: list[str], years: list[int]) -> QueryPlan:
    return QueryPlan(
        tickers=tickers,
        intent="factual",
        time_range=TimeRange(years=years),
        sub_questions=["q"],
        entities=[],
    )


def _state(**overrides: Any) -> AgentState:
    base: dict[str, Any] = {
        "original_query": "How did AAPL revenue trend?",
        "request_id": "req-1",
        "user_id": None,
        "query_plan": _plan(["AAPL"], [2023]),
        "unavailable_tickers": [],
        "unavailable_years": [],
        "query": "How did AAPL revenue trend?",
        "iteration": 0,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "dropped_for_capacity": [],
        "confidence": "high",
        "confidence_reason": "none",
        "coverage_gaps": [],
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
    assert set(out.keys()) == {"query_plan", "unavailable_tickers", "unavailable_years"}
    assert out["query_plan"].tickers == ["AAPL"]  # dropped TSLA
    assert all(t in _ALLOWED for t in out["query_plan"].tickers)
    assert out["unavailable_tickers"] == ["TSLA"]
    # D3/D4: strict json_schema constrained decoding requested.
    assert llm.with_structured_output_kwargs == {"method": "json_schema", "strict": True}


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

    assert out == {"confidence": "low", "confidence_reason": "llm", "coverage_gaps": []}
    assert len(llm.structured_calls) == 1  # LLM WAS consulted (no gaps)


async def test_evaluate_node_high() -> None:
    llm = _FakeLLM(structured_result=EvalVerdict(reasoning="complete", sufficient=True))
    ctx = _ctx(llm=llm)
    state = _state(
        query_plan=_plan(["AAPL"], [2023]),
        reranked_chunks=[ScoredChunk(chunk=_chunk("c1", "AAPL", 2023), rerank_score=1.0)],
    )
    out = await evaluate_node(state, _runtime(ctx))

    assert out == {"confidence": "high", "confidence_reason": "none", "coverage_gaps": []}


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
