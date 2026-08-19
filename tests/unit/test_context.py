"""Unit tests for the agent cold-start seam (src/alphalens/agent/context.py).

Scope is the S_agent_pool_timeouts contract: every wait on the agent pool carries a budget,
and a breach names WHICH leg broke. Fakes only -- no Neon connection, no Groq, no reranker.

Why these assertions are worth having: asyncpg leaves two of the three waits unbounded by
default, and both defaults are invisible at the call site (``acquire`` returns None-timeout
silently; ``command_timeout`` simply never fires). A regression here does not raise -- it
hangs. So the tests assert the kwargs ARRIVE, not merely that the code runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from alphalens.agent.context import build_agent_pool, load_ticker_universe
from alphalens.config import Settings, get_settings

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Pool-acquired connection stand-in; records the query budget it was handed."""

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
    """Fake asyncpg Pool exposing the two-budget shape: ``acquire(timeout=)`` -> ``fetch(timeout=)``."""

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


def _settings(**overrides: Any) -> Settings:
    """A real Settings built from live config, with fields overridden per test.

    A real instance (not a MagicMock) so a renamed or deleted field fails here rather than
    silently returning a Mock that satisfies every assertion.
    """
    return get_settings().model_copy(update=overrides)


# ── AC1: create_pool carries the connect and command budgets ──────────────────


async def test_ac1_create_pool_receives_both_timeout_kwargs() -> None:
    """AC1: both budgets reach create_pool, from settings rather than hardcoded."""
    settings = _settings(
        agent_pool_connect_timeout_seconds=17.0,
        agent_command_timeout_seconds=4.0,
    )
    create_pool = AsyncMock(return_value=_FakePool())

    with patch("alphalens.agent.context.asyncpg.create_pool", create_pool):
        await build_agent_pool(settings)

    kwargs = create_pool.await_args.kwargs
    assert kwargs["timeout"] == 17.0
    assert kwargs["command_timeout"] == 4.0


async def test_ac1_pool_shape_survives_the_timeout_addition() -> None:
    """The S16 pool shape (G1) is unchanged: same sizes, same pgvector codec on every conn.

    ``init=register_pgvector`` is the one kwarg that must never be dropped -- without it the
    ``list[float] -> $1::vector`` bind the retrieve node depends on stops working.
    """
    from alphalens.etl.upsert import register_pgvector

    settings = _settings()
    create_pool = AsyncMock(return_value=_FakePool())

    with patch("alphalens.agent.context.asyncpg.create_pool", create_pool):
        await build_agent_pool(settings)

    kwargs = create_pool.await_args.kwargs
    assert kwargs["min_size"] == settings.agent_pool_min_size
    assert kwargs["max_size"] == settings.agent_pool_max_size
    assert kwargs["init"] is register_pgvector


# ── AC2/AC5: load_ticker_universe bounds BOTH legs ────────────────────────────


async def test_ac2_ticker_universe_bounds_acquire_and_query_separately() -> None:
    """AC2/AC5: the cold-start query no longer rides a bare ``pool.fetch``.

    ``pool.fetch(q, timeout=T)`` would bound only the query -- asyncpg's Pool.fetch calls a
    bare ``self.acquire()`` and never forwards T -- so the explicit acquire is the whole point.
    """
    pool = _FakePool([{"ticker": "AAPL", "name": "Apple Inc."}])

    allowed, roster = await load_ticker_universe(
        pool,  # type: ignore[arg-type]
        acquire_timeout=12.0,
        query_timeout=3.0,
    )

    assert pool.acquire_timeouts == [12.0]
    assert pool.fetch_timeouts == [3.0]
    assert allowed == frozenset({"AAPL"})
    assert roster == {"AAPL": "Apple Inc."}


async def test_build_context_passes_the_configured_budgets() -> None:
    """The budgets reaching load_ticker_universe come from config, not from its defaults."""
    cfg = get_settings()
    pool = _FakePool([{"ticker": "AAPL", "name": "Apple Inc."}])

    await load_ticker_universe(
        pool,  # type: ignore[arg-type]
        acquire_timeout=cfg.agent_pool_acquire_timeout_seconds,
        query_timeout=cfg.agent_command_timeout_seconds,
    )

    assert pool.acquire_timeouts == [cfg.agent_pool_acquire_timeout_seconds]
    assert pool.fetch_timeouts == [cfg.agent_command_timeout_seconds]


# ── AC4: a breach names the leg that broke ────────────────────────────────────


async def test_ac4_acquire_breach_names_the_acquire_leg(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC4: acquire starvation and a slow query have opposite fixes, so they must not blur."""
    pool = _FakePool()
    pool.raise_on_acquire = TimeoutError()

    with caplog.at_level("ERROR"), pytest.raises(TimeoutError):
        await load_ticker_universe(
            pool,  # type: ignore[arg-type]
            acquire_timeout=1.5,
            query_timeout=9.0,
        )

    assert "acquire leg" in caplog.text and "1.5s" in caplog.text
    assert "query leg" not in caplog.text


async def test_ac4_query_breach_names_the_query_leg_and_releases(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC4: a breach INSIDE the `async with` is a query breach, and still releases the conn."""
    pool = _FakePool()
    pool.raise_on_fetch = TimeoutError()

    with caplog.at_level("ERROR"), pytest.raises(TimeoutError):
        await load_ticker_universe(
            pool,  # type: ignore[arg-type]
            acquire_timeout=30.0,
            query_timeout=2.5,
        )

    assert "query leg" in caplog.text and "2.5s" in caplog.text
    assert "acquire leg" not in caplog.text  # the `acquired` flag keeps the legs apart
    assert pool.acquired == 1 and pool.released == 1
