"""Unit tests for src/alphalens/api/app.py (S18) -- AC1, AC2, AC3, AC11, AC12.

Fakes only: zero Groq calls, zero Neon connections. ``build_context`` / ``build_graph`` are
monkeypatched at their ``alphalens.api.app`` binding, so the lifespan runs its real control flow
against a counting spy and a close-counting pool.

TestClient-driven cases are deliberately SYNC defs: ``TestClient`` is synchronous and drives the
ASGI app on its own portal thread, which does not compose with an already-running event loop.
Lifespan-internals cases are async and drive ``lifespan(app)`` directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

import alphalens.api.app as app_mod
from alphalens.api.app import DEFAULT_CORS_ORIGINS, create_app, lifespan

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakePool:
    """asyncpg Pool stand-in: counts closes. The lifespan never queries it."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _SpyBuildContext:
    """Counting spy over the cold-start seam (AC2).

    Records every invocation AND every pool it handed out, so "called exactly once" and
    "exactly one pool created" are two separate assertions rather than one inference.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.pools: list[_FakePool] = []

    async def __call__(self) -> tuple[Any, Any]:
        self.calls += 1
        pool = _FakePool()
        self.pools.append(pool)
        return object(), pool


def _install_spies(monkeypatch: pytest.MonkeyPatch) -> _SpyBuildContext:
    spy = _SpyBuildContext()
    monkeypatch.setattr(app_mod, "build_context", spy)
    monkeypatch.setattr(app_mod, "build_graph", lambda: object())
    return spy


# ── AC1: import-safety ────────────────────────────────────────────────────────


def test_import_with_no_dotenv_and_scrubbed_env(tmp_path: Path) -> None:
    """AC1, verified honestly rather than by proxy: import the module in a FRESH interpreter
    whose environment carries none of the app's variables and whose CWD contains no `.env`.

    A module-level `get_settings()`, DB connect, or model load would raise (ValidationError or
    a connection error) and the subprocess would exit non-zero. The in-process spy tests below
    cannot show this, because this interpreter has already loaded the repo's real `.env`.
    """
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "SYSTEMROOT")}
    probe = "import alphalens.api.app as m; assert not hasattr(m, 'app'); print('IMPORT_OK')"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        cwd=tmp_path,  # no .env discoverable from here
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr}"
    assert "IMPORT_OK" in result.stdout


def test_create_app_acquires_no_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: `create_app()` registers middleware and routes and nothing else -- resources are
    acquired only when the lifespan enters."""
    spy = _install_spies(monkeypatch)
    create_app()
    assert spy.calls == 0


def test_create_app_returns_a_fresh_instance_each_call() -> None:
    """AC1: a factory, not a module-level singleton -- so tests never share app state."""
    assert create_app() is not create_app()


# ── AC2: single cold start ────────────────────────────────────────────────────


def test_build_context_called_once_across_n_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2/D2: N sequential requests to a running app cold-start exactly once and create exactly
    one pool -- asserted with a counting spy, not by inspection."""
    spy = _install_spies(monkeypatch)
    with TestClient(create_app()) as client:
        for _ in range(3):
            assert client.get("/health").status_code == 200
    assert spy.calls == 1
    assert len(spy.pools) == 1


def test_lifespan_stashes_the_three_state_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `app.state` names are a de-facto contract for the v2 Lambda spec; pin them."""
    _install_spies(monkeypatch)
    app = create_app()
    with TestClient(app):
        assert app.state.agent_context is not None
        assert app.state.pool is not None
        assert app.state.graph is not None


# ── AC3: pool teardown ────────────────────────────────────────────────────────


def test_pool_closed_once_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: closed exactly once, and no Pool object survives lifespan exit."""
    spy = _install_spies(monkeypatch)
    app = create_app()
    with TestClient(app) as client:
        client.get("/health")
    assert spy.pools[0].close_calls == 1
    assert app.state.pool is None


async def test_pool_closed_once_when_the_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: the `finally` holds when something inside the lifespan's scope raises -- the pool is
    still closed exactly once and the state attribute is still cleared."""
    spy = _install_spies(monkeypatch)
    app = create_app()

    with pytest.raises(RuntimeError, match="in-flight failure"):
        async with lifespan(app):
            raise RuntimeError("in-flight failure")

    assert spy.pools[0].close_calls == 1
    assert app.state.pool is None
    assert app.state.agent_context is None
    assert app.state.graph is None


async def test_pool_closed_when_graph_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: `build_graph()` sits INSIDE the try, so a topology failure cannot leak the pool that
    `build_context()` already opened."""
    spy = _SpyBuildContext()
    monkeypatch.setattr(app_mod, "build_context", spy)

    def _boom() -> Any:
        raise RuntimeError("topology failure")

    monkeypatch.setattr(app_mod, "build_graph", _boom)
    app = create_app()

    with pytest.raises(RuntimeError, match="topology failure"):
        async with lifespan(app):
            pytest.fail("lifespan body must not run when build_graph raises")

    assert spy.pools[0].close_calls == 1
    assert app.state.pool is None


# ── AC11: CORS ────────────────────────────────────────────────────────────────
#
# Starlette 1.0.1 behaviour, OBSERVED before these tests were written (S18 execution note):
#   * allowed preflight   -> 200 with `access-control-allow-origin: <origin>`
#   * disallowed preflight-> 400 "Disallowed CORS origin", and it STILL emits
#     `access-control-allow-methods` / `-allow-headers` / `-max-age` -- but NOT
#     `access-control-allow-origin`.
# So the negative assertion is on the ABSENCE OF `access-control-allow-origin` specifically.
# Asserting "no access-control-* headers at all" would fail against this Starlette and would be
# testing the wrong thing: allow-origin is the header that actually grants the browser access.


def test_preflight_from_localhost_3000_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_spies(monkeypatch)
    response = TestClient(create_app()).options(
        "/query",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_preflight_from_unlisted_origin_gets_no_allow_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_spies(monkeypatch)
    response = TestClient(create_app()).options(
        "/query",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_simple_request_from_unlisted_origin_gets_no_allow_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-preflighted request is not blocked server-side, but it must not come back with the
    header that would let the browser hand the body to the page."""
    _install_spies(monkeypatch)
    response = TestClient(create_app()).get("/health", headers={"Origin": "http://evil.test"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_origins_are_a_parameter_and_never_wildcard() -> None:
    """D8 + Q6/Q7: origins arrive as a `create_app` argument -- no config field, no
    `get_settings()` call -- and the default allowlist never contains `"*"`."""
    assert "*" not in DEFAULT_CORS_ORIGINS
    assert DEFAULT_CORS_ORIGINS == ["http://localhost:3000", "http://127.0.0.1:3000"]

    custom = TestClient(create_app(cors_origins=["http://localhost:4321"])).options(
        "/query",
        headers={
            "Origin": "http://localhost:4321",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert custom.headers["access-control-allow-origin"] == "http://localhost:4321"


# ── AC12: health ──────────────────────────────────────────────────────────────


def test_health_is_liveness_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC12: 200 `{"status": "ok"}` WITHOUT touching the DB, the LLM or the reranker.

    Driven through a TestClient built WITHOUT the context manager, so the lifespan never runs:
    the route answers with no context, no pool and no graph in existence, which is the strongest
    available statement that it reads none of them.
    """
    spy = _install_spies(monkeypatch)
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert spy.calls == 0


def test_health_answers_after_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Liveness must not depend on lifespan state that shutdown has already cleared."""
    _install_spies(monkeypatch)
    app = create_app()
    client = TestClient(app)
    with client:
        pass  # startup + shutdown
    assert client.get("/health").json() == {"status": "ok"}
    assert cast(Any, app.state).pool is None
