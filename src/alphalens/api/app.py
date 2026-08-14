"""AlphaLens v8 -- FastAPI + SSE local app (S18).

WRAPPER ONLY (D1). This module puts the existing v1 agent behind a local HTTP surface. Every
retrieval, reranking, evaluation and honesty decision continues to happen exactly where it
already happens -- nothing here re-implements a node, the graph, or the streaming seam. If a
change here seems to require editing ``nodes.py`` / ``graph.py`` / ``prompts.py`` / ``state.py``,
that is a signal to stop and re-scope.

Import-safety (AC1): importing this module performs no DB connection, no model load and no
``await``. ``create_app()`` is likewise side-effect-free -- it registers middleware and routes
and nothing else. In particular it does NOT call ``get_settings()``: that would validate the
entire environment at construction time and raise with no ``.env`` present (S18 Q6/Q7), which is
why CORS origins arrive as a parameter rather than from config.

Resources are acquired exactly once, when the lifespan enters (D2).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from alphalens.agent.context import build_context
from alphalens.agent.graph import build_graph

logger = logging.getLogger(__name__)

# D8: explicit allowlist, never `["*"]`. Next.js dev serves on :3000 while this app binds to
# 127.0.0.1:8000 -- different origins, so the browser blocks the request without CORS.
DEFAULT_CORS_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """D2 cold-start seam: build the context + graph ONCE, close the pool on shutdown.

    The app-scope twin of run_query.py's try/finally (S16 G7). ``build_context()`` opens an
    asyncpg pool AND loads the ~80 MB cross-encoder, so doing this per request would add seconds
    to every query and open a pool per request.

    ``app.state`` attribute names -- ``agent_context`` / ``pool`` / ``graph`` -- are a de-facto
    contract: the v2 Lambda spec and any future middleware read the same three names. Renaming
    them is a breaking change, not a refactor.
    """
    ctx, pool = await build_context()
    try:
        app.state.agent_context = ctx
        app.state.pool = pool
        # build_graph() is inside the try so that a topology failure still closes the pool.
        app.state.graph = build_graph()
        logger.info("agent cold start complete: context, pool and graph ready")
        yield
    finally:
        await pool.close()
        # AC3: no Pool object survives lifespan exit -- the attribute is cleared, not merely
        # closed, so a post-shutdown reader gets None rather than a dead pool.
        app.state.pool = None
        app.state.agent_context = None
        app.state.graph = None
        logger.info("agent shutdown complete: pool closed")


def create_app(cors_origins: list[str] | None = None) -> FastAPI:
    """App factory. Registers the lifespan, CORS (D8) and the routes.

    A factory rather than a module-level ``app = FastAPI()`` so importing this module has no
    side effects and tests can build a fresh instance per case.

    ``cors_origins`` is a PARAMETER, not a config read (S18 Q6/Q7): ``CORSMiddleware`` must be
    registered at construction time, and reaching for ``get_settings()`` here would validate the
    whole environment inside ``create_app()`` -- raising ``ValidationError`` with no ``.env``
    present and breaking AC1.

    No compression middleware is registered, deliberately (G3): gzip buffers the response and
    would silently defeat token-by-token streaming.
    """
    app = FastAPI(title="AlphaLens API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins) if cors_origins is not None else DEFAULT_CORS_ORIGINS,
        allow_credentials=False,  # D7: no auth, no cookies -- nothing to send credentials for
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness only (AC12) -- touches no DB, no LLM and no reranker.

        A health check that queries Neon turns a DB blip into a restart loop, and readiness
        semantics only matter once something is orchestrating the process (a v2 concern). This
        route deliberately does not read ``app.state``, so it answers before the lifespan has
        run and after it has torn down.
        """
        return {"status": "ok"}

    return app
