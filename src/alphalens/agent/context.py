"""AlphaLens v8 -- Agent cold-start assembly + pool lifecycle (S16).

COLD-START SEAM. Everything expensive the agent needs -- the asyncpg pool, the Groq LLM, the
cross-encoder reranker (~80 MB), the Jina query embedder, the synthesis circuit breaker, and
the DB-derived ticker universe -- is assembled ONCE here and packed into an ``AgentContext``.
This is import-safe only because it all lives behind ``async def build_context`` and never at
module scope (S16/D1a): importing ``alphalens.agent.graph`` connects to nothing.

Pool ownership: ``build_context`` returns ``(AgentContext, Pool)`` as a tuple so the *caller*
owns teardown (``await pool.close()`` in a ``finally``), mirroring ``etl/runner.py`` (G7). The
pool is built on the config-driven ``agent_db_url`` endpoint (POOLED default, S16/D-endpoint)
with the S9 ``register_pgvector`` codec on every connection (G1/G2 -- required for the
``list[float] -> $1::vector`` binding the retrieve node depends on).
"""

from __future__ import annotations

import logging

import asyncpg
from asyncpg import Pool
from langchain_groq import ChatGroq
from sentence_transformers import CrossEncoder

from alphalens.agent.circuit_breaker import SynthesisCircuitBreaker  # S14
from alphalens.agent.nodes import AgentContext
from alphalens.config import Settings, get_settings
from alphalens.etl.embeddings import EmbeddingClient  # S8 (jina-v3 query embeddings)
from alphalens.etl.upsert import register_pgvector  # S9 -- REUSE, do NOT reimplement

_log = logging.getLogger(__name__)


async def build_agent_pool(settings: Settings) -> Pool:
    """Create the agent asyncpg pool with the pgvector codec registered on EVERY connection.

    MIRRORS ``etl/runner.py``'s live pool shape (G1): positional DSN, ``min_size=1``,
    ``max_size=5``, ``init=register_pgvector``, prepared statements ON (NO
    ``statement_cache_size=0``). The endpoint is config-driven via ``agent_db_url`` (POOLED
    default; env ``AGENT_DB_URL`` overrides for a scale-flip) -- S16/D-endpoint.

    Both timeout kwargs travel through asyncpg's ``**connect_kwargs`` onto every connection
    the pool opens. ``command_timeout`` is not merely belt-and-braces: ``register_pgvector``
    runs as the ``init=`` callback and issues codec round-trips that NO call site can wrap,
    so the pool-level default is the only thing that can bound them. It is deliberately NOT
    an acquire budget -- asyncpg has no such pool-level knob (``Pool.__init__`` has no
    timeout slot), which is why each agent call site passes its own.
    """
    dsn = (settings.agent_db_url or settings.neon_database_url).get_secret_value()
    pool: Pool = await asyncpg.create_pool(
        dsn,
        min_size=settings.agent_pool_min_size,
        max_size=settings.agent_pool_max_size,
        init=register_pgvector,
        timeout=settings.agent_pool_connect_timeout_seconds,
        command_timeout=settings.agent_command_timeout_seconds,
    )
    return pool


async def load_ticker_universe(
    pool: Pool,
    *,
    acquire_timeout: float,
    query_timeout: float,
) -> tuple[frozenset[str], dict[str, str]]:
    """One cold-start query on ``companies`` -> ``(allowed_tickers, ticker_roster)`` (D3a).

    The frozenset is the hard input-rail (``validate_tickers``); the roster (ticker->name) is
    injected into the Plan system prompt so word->ticker resolution is grounded, not guessed.

    TWO budgets, not one. ``pool.fetch(q, timeout=T)`` would bound only the query -- asyncpg's
    ``Pool.fetch`` calls a bare ``self.acquire()`` and never forwards ``T`` (pool.py:613-634),
    and a ``None`` acquire timeout awaits the connection queue forever (pool.py:891-899). The
    two legs stay separately identifiable because they have opposite fixes: acquire starvation
    means pool sizing, a slow query means SQL or indexes.

    CALLER BEWARE: this runs inside the FastAPI lifespan ABOVE the ``try:`` at ``app.py:69``,
    so a breach here kills startup and has no ``ErrorEvent`` path. That is intended -- loud
    beats silent -- but it is NOT the request-scoped path.
    """
    acquired = False
    try:
        async with pool.acquire(timeout=acquire_timeout) as con:
            acquired = True
            rows = await con.fetch("SELECT ticker, name FROM companies", timeout=query_timeout)
    except TimeoutError:
        # The flag is what keeps the two legs apart: a query breach raised inside the `async
        # with` would otherwise be logged here as an acquire breach.
        leg, budget = ("query", query_timeout) if acquired else ("acquire", acquire_timeout)
        # %g not %.1f: a sub-second budget must not be reported as "0.0s".
        _log.error("ticker universe load timed out on the %s leg after %gs", leg, budget)
        raise
    roster: dict[str, str] = {row["ticker"]: row["name"] for row in rows}
    return frozenset(roster), roster


async def build_context(settings: Settings | None = None) -> tuple[AgentContext, Pool]:
    """Assemble ALL per-run deps ONCE at cold-start; return ``(context, pool)``.

    The pool is returned SEPARATELY so the caller owns teardown (``await pool.close()`` in a
    ``finally``). Everything here is import-safe only because it lives behind this async
    function, never at module scope (D1a).

    Temperature note (S16/D-temp): the shared ``llm`` carries ``groq_temperature`` -- that is
    the SYNTHESIZE-node temperature. Plan and Evaluate re-pin ``temperature=0`` invariantly at
    their own call sites (``nodes.py``) and ignore this value.
    """
    settings = settings or get_settings()

    pool = await build_agent_pool(settings)
    allowed_tickers, ticker_roster = await load_ticker_universe(
        pool,
        acquire_timeout=settings.agent_pool_acquire_timeout_seconds,
        query_timeout=settings.agent_command_timeout_seconds,
    )

    # api_key passed explicitly (G6): pydantic-settings loads .env into Settings, not
    # os.environ, so ChatGroq's GROQ_API_KEY env fallback is not guaranteed at run time.
    llm = ChatGroq(
        model_name=settings.groq_model,  # field name (alias "model"); mypy wants the field name
        api_key=settings.groq_api_key.get_secret_value(),
        temperature=settings.groq_temperature,  # Synthesize temp; Plan/Evaluate re-pin 0 (D-temp)
    )
    reranker = CrossEncoder(settings.reranker_model)  # L3/L14 -- baked in the image at build time
    embedder = EmbeddingClient(settings)  # S8; mirror etl/runner positional construction
    breaker = SynthesisCircuitBreaker(  # S14 -- real ctor (G6), NOT settings=
        failure_threshold=settings.breaker_failure_threshold,
        reset_timeout_seconds=settings.breaker_reset_timeout_seconds,
    )

    ctx = AgentContext(
        llm=llm,
        reranker=reranker,
        pool=pool,
        allowed_tickers=allowed_tickers,
        breaker=breaker,
        embedder=embedder,
        ticker_roster=ticker_roster,  # NEW field (S16/D3a)
        # Plan year-rail bounds: config-derived here so nodes read every rail's gate set off
        # the context rather than reaching for get_settings() mid-node.
        corpus_min_year=settings.corpus_min_year,
        corpus_max_year=settings.corpus_max_year,
    )
    return ctx, pool
