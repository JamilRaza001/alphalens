"""S16/D4 -- live end-to-end regression net for the agent graph.

SCAFFOLD ONLY. This test is authored FROM the observed shape of the first ``run_query.py``
live run (D4 / AC14), not blind -- the first run is exploratory (Synthesize is free-form and
non-deterministic). It is ``@pytest.mark.live``, EXCLUDED from the default ``pytest`` run
(``addopts = -m "not live"``), and needs live Neon + Groq creds + network.

Once the first run is observed, replace the stub below with STRUCTURAL invariants only (never
content):
  - the streamed answer is non-empty,
  - at least one citation is returned,
  - every ``citation.ticker`` AND every ``query_plan.ticker`` is in ``allowed_tickers``,
  - status/confidence is one of the expected values (ok/degraded ~ high/low).
Drive it with a >=2-ticker x >=2-year query (AC13) so per-cell fan-out + RRF + join are
actually exercised.
"""

from __future__ import annotations

import pytest


@pytest.mark.live
async def test_agent_end_to_end_live() -> None:
    """Structural invariants of one live end-to-end run (authored after the first run_query.py
    run). See module docstring for the contract."""
    pytest.skip("TODO(S16/D4/AC14): author structural assertions from the first live run")
