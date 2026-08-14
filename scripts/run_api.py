"""AlphaLens v8 -- local API dev entrypoint (S18).

Runs the FastAPI + SSE app under uvicorn for LOCAL development only (D7): no auth, no OIDC, no
rate limiting, bound to loopback. The production ASGI story -- Lambda Web Adapter, Function URL,
container image -- belongs to the v2 deployment specs, not here.

Usage:
    python scripts/run_api.py            # 127.0.0.1:8000
    API_PORT=8123 python scripts/run_api.py

Then, to verify streaming really streams (S18 G1/G3 -- `EventSource` cannot POST, and any
buffering layer would deliver the tokens in one lump):

    curl -N -X POST http://127.0.0.1:8000/query \\
      -H 'Content-Type: application/json' \\
      -d '{"question": "How did Apple and Microsoft revenue compare in 2023 vs 2024?"}'

Needs live creds (Neon + Groq) in the environment/.env, because the lifespan cold-starts the
real agent. Not run in CI.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    """Serve the app on ``API_HOST``:``API_PORT`` (defaults 127.0.0.1:8000).

    The environment is read HERE and never at import time, so importing this module stays free
    of side effects in the same way ``alphalens.api.app`` is (AC1).

    ``reload=False`` deliberately: the reloader re-imports the app on every file change, and each
    re-import re-runs the lifespan -- meaning a fresh asyncpg pool and another ~80 MB reranker
    load per save. The app is passed as an import string with ``factory=True`` so uvicorn calls
    ``create_app()`` itself rather than receiving a pre-built instance.
    """
    uvicorn.run(
        "alphalens.api.app:create_app",
        factory=True,
        host=os.environ.get("API_HOST", "127.0.0.1"),
        port=int(os.environ.get("API_PORT", "8000")),
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
