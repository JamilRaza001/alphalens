"""Unit tests for the S15 Retrieve node (src/alphalens/agent/nodes.py).

Covers the S15 acceptance criteria with FAKES only -- no live Groq, DB, or graph:
  * a fake EmbeddingClient (jina-v3 / nomic / counting) for the query-embedding path,
  * a fake asyncpg Pool whose ``fetch`` returns canned Records (dicts) and records calls.

The real per-cell RRF fusion runs in SQL (untestable without a DB); ``rrf_fuse`` is the
Python-testable reference for that formula (AC6). asyncio_mode=auto: no @pytest.mark.asyncio.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from asyncpg import Pool

from alphalens.agent.nodes import (
    AgentContext,
    hybrid_search_cell,
    merge_dedup,
    plan_to_cells,
    retrieve_node,
    rrf_fuse,
)
from alphalens.agent.state import (
    AgentState,
    QueryPlan,
    RetrievedChunk,
    TimeRange,
)
from alphalens.config import get_settings
from alphalens.etl.embeddings import EmbeddingClient, EmbeddingModelVersion, EmbeddingResult

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeEmbedder:
    """Fake EmbeddingClient: returns one preset vector, records every embed_query call."""

    def __init__(
        self,
        *,
        model_version: EmbeddingModelVersion = "jina-v3",
        vector: list[float] | None = None,
    ) -> None:
        self._model_version: EmbeddingModelVersion = model_version
        self._vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.calls: list[str] = []

    async def embed_query(self, text: str) -> EmbeddingResult:
        self.calls.append(text)
        return EmbeddingResult(
            vectors=[self._vector],
            model_version=self._model_version,
            tokens_used=0,
        )


class _FakeConn:
    """Fake pool-acquired connection: ``fetch`` returns canned rows and records (sql, args).

    Recording lives on the owning _FakePool so ``fetch_calls`` stays the assertion surface it
    was before retrieval moved behind an explicit ``acquire``.
    """

    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def fetch(self, sql: str, *args: Any, timeout: float | None = None) -> list[Any]:
        self._pool.fetch_calls.append((sql, args))
        self._pool.fetch_timeouts.append(timeout)
        if self._pool.raise_on_fetch is not None:
            raise self._pool.raise_on_fetch
        return list(self._pool.rows)


class _AcqCtx:
    """``pool.acquire(timeout=...)`` stand-in -- an async CM, mirroring test_runner.py:152."""

    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        if self._pool.raise_on_acquire is not None:
            raise self._pool.raise_on_acquire
        self._pool.acquired += 1
        return _FakeConn(self._pool)

    async def __aexit__(self, *_: Any) -> None:
        self._pool.released += 1


class _FakePool:
    """Fake asyncpg Pool exposing the two-budget shape: ``acquire(timeout=)`` -> ``fetch(timeout=)``.

    Records both budgets so tests can assert each leg is bounded, and can be told to breach
    either leg so the per-leg logging is testable without a real pool.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_timeouts: list[float | None] = []
        self.acquire_timeouts: list[float | None] = []
        self.acquired = 0
        self.released = 0
        self.raise_on_acquire: BaseException | None = None
        self.raise_on_fetch: BaseException | None = None

    def acquire(self, *, timeout: float | None = None) -> _AcqCtx:
        self.acquire_timeouts.append(timeout)
        return _AcqCtx(self)


# ── Builders ──────────────────────────────────────────────────────────────────


def _plan(tickers: list[str], years: list[int], sub_questions: list[str]) -> QueryPlan:
    return QueryPlan(
        tickers=tickers,
        intent="factual",
        time_range=TimeRange(years=years),
        sub_questions=sub_questions,
        entities=[],
        unresolved_companies=[],
    )


def _ctx(*, embedder: Any, pool: Any) -> AgentContext:
    # Only embedder + pool are exercised by retrieve_node; the rest are inert stand-ins.
    return AgentContext(
        llm=cast(Any, object()),
        reranker=cast(Any, object()),
        pool=cast(Pool, pool),
        allowed_tickers=frozenset({"AAPL", "MSFT"}),
        breaker=cast(Any, object()),
        embedder=cast(EmbeddingClient, embedder),
        ticker_roster={},  # unused by retrieve_node
        corpus_min_year=2021,  # unused by retrieve_node (the year-rail runs in plan_node)
        corpus_max_year=2026,
    )


def _runtime(ctx: AgentContext) -> Any:
    from langgraph.runtime import Runtime

    return Runtime(context=ctx)


def _state(plan: QueryPlan) -> AgentState:
    base: dict[str, Any] = {
        "original_query": "q",
        "request_id": "req-1",
        "user_id": None,
        "query_plan": plan,
        "unavailable_tickers": [],
        "unavailable_companies": [],
        "unavailable_years": [],
        "query": "q",
        "iteration": 0,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "confidence": "high",
        "confidence_reason": "none",
        "coverage_gaps": [],
        "citations": [],
        "answer_stream": None,
    }
    return cast(AgentState, base)


def _row(
    *,
    chunk_id: str = "c1",
    section: str | None = "Item 7",
    ticker: str = "AAPL",
    period_year: int = 2023,
    filing_type: str = "10-K",
    metadata: Any = "{}",
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "text": "evidence",
        "section": section,
        "ticker": ticker,
        "period_year": period_year,
        "filing_type": filing_type,
        "metadata": metadata,
    }


# ── AC1: jina-v3 enforced (nomic ⇒ raise, DB untouched) ───────────────────────


async def test_ac1_nomic_query_embedding_raises_before_any_db() -> None:
    embedder = _FakeEmbedder(model_version="nomic-embed-text-v1.5")
    pool = _FakePool()
    ctx = _ctx(embedder=embedder, pool=pool)
    plan = _plan(["AAPL"], [2023], ["revenue trend"])

    with pytest.raises(RuntimeError, match="mismatched vector space"):
        await retrieve_node(_state(plan), _runtime(ctx))

    assert pool.fetch_calls == []  # no search issued in the wrong vector space


# ── AC5: embed each UNIQUE sub_question exactly once ──────────────────────────


async def test_ac5_embed_once_per_distinct_subquestion() -> None:
    embedder = _FakeEmbedder()
    pool = _FakePool()
    ctx = _ctx(embedder=embedder, pool=pool)
    # 2 tickers x 1 year x 2 sub-questions -> "q1" and "q2" each appear in 2 cells.
    plan = _plan(["AAPL", "MSFT"], [2023], ["q1", "q2"])

    await retrieve_node(_state(plan), _runtime(ctx))

    assert sorted(embedder.calls) == ["q1", "q2"]  # once per DISTINCT sub-question, not per cell


# ── AC4: fan-out shape + one hybrid query per cell ────────────────────────────


def test_ac4_plan_to_cells_full_product() -> None:
    plan = _plan(["A", "B"], [2023], ["q1", "q2"])
    assert plan_to_cells(plan) == [
        ("A", 2023, "q1"),
        ("A", 2023, "q2"),
        ("B", 2023, "q1"),
        ("B", 2023, "q2"),
    ]


async def test_ac4_one_fetch_per_cell() -> None:
    embedder = _FakeEmbedder()
    pool = _FakePool()
    ctx = _ctx(embedder=embedder, pool=pool)
    plan = _plan(["AAPL", "MSFT"], [2023], ["q1", "q2"])  # 4 cells

    await retrieve_node(_state(plan), _runtime(ctx))

    assert len(pool.fetch_calls) == 4  # hybrid_search_cell fired once per cell


# ── AC9: empty inputs ⇒ [] with the DB (and embedder) untouched ───────────────


@pytest.mark.parametrize(
    ("tickers", "years", "subqs"),
    [
        ([], [2023], ["q"]),
        (["AAPL"], [], ["q"]),
        (["AAPL"], [2023], []),
    ],
)
async def test_ac9_empty_dimension_returns_empty_no_db(
    tickers: list[str], years: list[int], subqs: list[str]
) -> None:
    plan = _plan(tickers, years, subqs)
    assert plan_to_cells(plan) == []

    embedder = _FakeEmbedder()
    pool = _FakePool()
    ctx = _ctx(embedder=embedder, pool=pool)

    out = await retrieve_node(_state(plan), _runtime(ctx))

    assert out == {"retrieved_chunks": []}
    assert pool.fetch_calls == []  # short-circuit before any query
    assert embedder.calls == []  # ... and before any embedding


# ── AC7: merge_dedup keeps first per chunk_id, order-stable ───────────────────


def test_ac7_merge_dedup_keeps_first_occurrence_order_stable() -> None:
    def rc(cid: str) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=cid,
            text="t",
            section=None,
            ticker="AAPL",
            period_year=2023,
            filing_type="10-K",
        )

    cell_a = [rc("c1"), rc("c2")]
    cell_b = [rc("c2"), rc("c3")]  # c2 overlaps cell_a
    merged = merge_dedup([cell_a, cell_b])

    assert [c.chunk_id for c in merged] == ["c1", "c2", "c3"]  # once each, first-seen order


# ── AC6: RRF reference -- dual-list outranks single-list; single still scored ──


def test_ac6_rrf_dual_list_outranks_single_and_single_still_scored() -> None:
    # "b" appears in both retrievers; "a"/"c" only in vector; "d" only in lexical.
    vec_ranked = ["a", "b", "c"]
    lex_ranked = ["b", "d"]
    fused = rrf_fuse(vec_ranked, lex_ranked, c=60)
    scores = dict(fused)

    assert fused[0][0] == "b"  # present in BOTH lists -> highest fused score
    assert scores["b"] > scores["a"]  # dual-list beats a single-list top hit
    assert scores["d"] > 0.0  # single-list (lexical-only) chunk is still scored
    assert set(scores) == {"a", "b", "c", "d"}  # nothing dropped (FULL OUTER semantics)


# ── AC3: hybrid_search_cell maps Records -> enriched RetrievedChunk ────────────


async def test_ac3_hybrid_search_cell_maps_and_enriches_rows() -> None:
    rows = [
        _row(chunk_id="c1", section="Item 1A", metadata='{"k": "v"}'),
        _row(chunk_id="c2", section=None, metadata={"already": "dict"}),  # tolerate dict
        _row(chunk_id="c3", section="Item 7", metadata=None),  # tolerate NULL
    ]
    pool = _FakePool(rows)

    result = await hybrid_search_cell(
        cast(Pool, pool),
        query_vector=[0.1, 0.2, 0.3],
        sub_question="revenue",
        ticker="AAPL",
        year=2023,
        k_vector=20,
        k_lexical=20,
        rrf_c=60,
        n_per_cell=10,
        acquire_timeout=30.0,
        query_timeout=10.0,
    )

    assert [c.chunk_id for c in result] == ["c1", "c2", "c3"]  # DB (RRF) order preserved
    # Enriched from the chunks->filings join.
    assert all(
        c.ticker == "AAPL" and c.period_year == 2023 and c.filing_type == "10-K" for c in result
    )
    assert result[0].section == "Item 1A"
    assert result[1].section is None  # section is nullable
    assert result[0].metadata == {"k": "v"}  # JSON string decoded
    assert result[1].metadata == {"already": "dict"}  # dict passed through
    assert result[2].metadata == {}  # NULL -> {}

    # Parameterized only: the SQL text carries no interpolated ticker/year/subquestion.
    sql, args = pool.fetch_calls[0]
    assert "AAPL" not in sql and "2023" not in sql and "revenue" not in sql
    assert args[1] == "AAPL" and args[2] == 2023 and args[4] == "revenue"


# ── AC8: RETRIEVAL_N_PER_CELL env override flows into the LIMIT ($8) bind ──────


async def test_ac8_n_per_cell_env_override_flows_into_limit_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETRIEVAL_N_PER_CELL", "3")
    get_settings.cache_clear()
    try:
        embedder = _FakeEmbedder()
        pool = _FakePool()
        ctx = _ctx(embedder=embedder, pool=pool)
        plan = _plan(["AAPL"], [2023], ["q"])  # single cell -> single fetch

        await retrieve_node(_state(plan), _runtime(ctx))

        _sql, args = pool.fetch_calls[0]
        assert args[7] == 3  # $8 == n_per_cell, overridden from the default 10
    finally:
        get_settings.cache_clear()  # restore the shared settings singleton for other tests


# ── S_agent_pool_timeouts AC2/AC4/AC5: both legs bounded, and distinguishable ──


async def test_both_timeout_budgets_reach_the_pool_from_config() -> None:
    """AC5: retrieve_node threads BOTH budgets down; neither leg is left unbounded.

    Asserted against live config rather than literals, so a default change cannot leave this
    test passing while the wiring silently reverts to None.
    """
    cfg = get_settings()
    embedder = _FakeEmbedder()
    pool = _FakePool()
    ctx = _ctx(embedder=embedder, pool=pool)

    await retrieve_node(_state(_plan(["AAPL"], [2023], ["q"])), _runtime(ctx))

    assert pool.acquire_timeouts == [cfg.agent_pool_acquire_timeout_seconds]
    assert pool.fetch_timeouts == [cfg.agent_command_timeout_seconds]


async def test_acquire_breach_names_the_acquire_leg_and_the_cell(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC4: an acquire breach is attributed to acquire -- not conflated with a slow query."""
    pool = _FakePool()
    pool.raise_on_acquire = TimeoutError()

    with caplog.at_level("ERROR"), pytest.raises(TimeoutError):
        await hybrid_search_cell(
            cast(Pool, pool),
            query_vector=[0.1],
            sub_question="revenue",
            ticker="AAPL",
            year=2023,
            k_vector=20,
            k_lexical=20,
            rrf_c=60,
            n_per_cell=10,
            acquire_timeout=1.5,
            query_timeout=9.0,
        )

    assert "acquire leg" in caplog.text and "1.5s" in caplog.text
    assert "AAPL/2023" in caplog.text  # cell identity: which cell stalled
    assert "query leg" not in caplog.text


async def test_query_breach_names_the_query_leg_and_releases_the_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC4 + Gotcha 2: the query leg is named, and the connection is still released.

    The release matters more than the label here: retrieve_node fans out against a max_size=5
    pool, so one connection leaked on the error path starves every remaining cell.
    """
    pool = _FakePool()
    pool.raise_on_fetch = TimeoutError()

    with caplog.at_level("ERROR"), pytest.raises(TimeoutError):
        await hybrid_search_cell(
            cast(Pool, pool),
            query_vector=[0.1],
            sub_question="revenue",
            ticker="MSFT",
            year=2024,
            k_vector=20,
            k_lexical=20,
            rrf_c=60,
            n_per_cell=10,
            acquire_timeout=30.0,
            query_timeout=2.5,
        )

    assert "query leg" in caplog.text and "2.5s" in caplog.text
    assert "MSFT/2024" in caplog.text
    assert "acquire leg" not in caplog.text  # the `acquired` flag keeps the legs apart
    assert pool.acquired == 1 and pool.released == 1  # released despite the re-raise
