\# S\_agent\_pool\_timeouts — Bounded Waits on the Agent Pool

\*\*Format:\*\* L21 lightweight (Goal + Signatures + AC + Gotchas).

\*\*Scope:\*\* agent pool only. ETL pools are out of scope (serial, one connection

at a time, proven at 180 filings) — separate concern, separate spec.

\---

\## Goal

Every wait on the agent asyncpg pool is bounded, and a breach is

distinguishable in the server log. Today two of three waits are unbounded, so

a stall produces silence rather than diagnostics: a live run hung ~5 minutes,

burned a Groq call, and emitted no application log line after Plan.

\---

\## Recon findings this spec rests on (asyncpg 0.31.0, live-verified)

| Wait | Knob | Live default | Status |

|---|---|---|---|

| Connection establishment (TCP/TLS/auth + \`init\`) | \`timeout\` via \`\*\*connect\_kwargs\` | 60 | bounded, loose |

| Acquire (wait for a free connection) | \`Pool.acquire(timeout=)\` | \`None\` = forever | \*\*UNBOUNDED\*\* |

| Query execution | \`command\_timeout\` | \`None\` = forever | \*\*UNBOUNDED\*\* |

\*\*Load-bearing detail:\*\* \`pool.fetch(q, timeout=T)\` bounds ONLY the query leg.

\`pool.py:613-634\` calls a bare \`self.acquire()\` and does not forward \`T\`.

\`pool.py:891-899\`: when \`timeout is None\`, acquire awaits \`self.\_queue.get()\`

with no \`wait\_for\`. \`Pool.\_\_init\_\_\` has no timeout slot, so there is no

pool-level default to inherit — \*\*every acquire call site must pass its own.\*\*

Measured retrieval cost (zero Groq, seeded random unit vector, live config

\`jina\_dimensions=768\`, densest cell JPM/2023 @ 1,934 chunks): steady state

~0.21 s/cell; worst first-query ~1.08 s; pool build 1.4–2.3 s.

\`asyncio.TimeoutError is TimeoutError\` on 3.12 and subclasses \`Exception\`, so

S18's \`except Exception\` catches it — no \`BaseException\` change needed.

\---

\## Decisions

\- \*\*D1 — Two-budget explicit acquire.\*\* At agent pool call sites, replace

\`pool.fetch(...)\` with an explicit \`async with pool.acquire(timeout=A) as con:\`

followed by \`con.fetch(..., timeout=Q)\`. A single \`asyncio.timeout()\` wrap was

\*\*rejected\*\*: it bounds the hang but collapses acquire-exhaustion and

slow-query into one indistinguishable failure, and those have opposite fixes

(pool sizing vs SQL/index).

\- \*\*D2 — \`command\_timeout\` on \`create\_pool\` as well.\*\* Defence in depth: it

covers \`register\_pgvector\` codec round-trips and any future call site that

forgets D1.

\- \*\*D3 — Tighten connect \`timeout\` to 30\*\* from asyncpg's 60.

\- \*\*D4 — Three config fields, \`\_seconds\` suffix\*\* (precedent:

\`breaker\_reset\_timeout\_seconds\`). No aliases needed —

\`case\_sensitive=False\` maps \`AGENT\_\*\` env vars to snake\_case fields.

\- \*\*D5 — Diagnosability is in scope, the wire contract is not.\*\* Breaches are

logged at ERROR with which leg and which cell. No new exception type, no

\`ErrorEvent\` shape change, no S18 file touched.

\## Config (\`config.py\`, agent block, matching live style)

\`\`\`python

agent\_pool\_connect\_timeout\_seconds: float = 30.0

agent\_pool\_acquire\_timeout\_seconds: float = 30.0

agent\_command\_timeout\_seconds: float = 10.0

\`\`\`

\`10.0\` is ~50× measured steady state and ~9× worst observed first query, while

still failing loudly. It must also clear cold-start \`load\_ticker\_universe\` and

codec round-trips.

\## Call sites (all three must be covered)

| # | Site | Change |

|---|---|---|

| 1 | \`context.py:39\` \`create\_pool\` | add \`timeout=\`, \`command\_timeout=\` |

| 2 | \`context.py:54\` \`load\_ticker\_universe\` | D1 two-budget |

| 3 | \`nodes.py:386\` \`hybrid\_search\_cell\` | D1 two-budget |

\---

\## Acceptance Criteria

1\. \`create\_pool\` passes both \`timeout\` and \`command\_timeout\` from settings.

2\. Sites 2 and 3 use explicit \`pool.acquire(timeout=...)\`; no bare

\`pool.fetch(...)\` remains on the agent path (grep-verifiable).

3\. Three config fields exist with the defaults above and are env-overridable.

4\. An acquire breach and a query breach are distinguishable in the log: the

message states which leg timed out; site 3 also logs the cell identity.

5\. Unit tests assert the timeout kwargs reach \`create\_pool\`, \`acquire\`, and

\`fetch\` (fakes; no live DB).

6\. \*\*G1 live gate, zero Groq:\*\* with \`AGENT\_POOL\_MAX\_SIZE=1\` and

\`AGENT\_POOL\_ACQUIRE\_TIMEOUT\_SECONDS=1\`, hold the single connection and

attempt a concurrent cell — a \`TimeoutError\` is raised within ~1 s and the

log names the acquire leg. Throwaway script, not committed.

7\. Gates green: ruff, ruff format, mypy --strict, pytest (285/7/1 baseline

must not regress).

8\. No file under \`src/alphalens/api/\` is modified (S18 D1 wrap-only preserved).

\---

\## Open question — CLOSED by recon

\*\*OQ1 — how do timeout values reach \`nodes.py:386\`?\*\* \`hybrid\_search\_cell\`

receives a bare \`Pool\` plus eight keyword scalars and reads no module state, so it

sees \*\*neither \`Settings\` nor \`AgentContext\`\*\*. Its caller \`retrieve\_node\` sees

both: \`nodes.py:434\` already calls \`cfg = get\_settings()\` (\`@lru\_cache(maxsize=1)\`,

so a dict lookup) and \`nodes.py:462-465\` already threads four config values down as

explicit kwargs.

\*\*LOCKED — shape A, explicit keyword-only parameters\*\*, matching that live

precedent. \`retrieve\_node\` passes the budgets beside the existing \`cfg.retrieval\_\*\`

kwargs. No \`AgentContext\` change, so none of its 6 construction sites move and the

LangGraph \`context\_schema=AgentContext\` binding at \`graph.py:42\` is untouched.

\*\*Correction:\*\* \*\*two\*\* floats reach site 3, not three.

\`agent\_pool\_connect\_timeout\_seconds\` is a \`create\_pool\` kwarg (site 1) and never

reaches \`nodes.py\` at all.

\---

\## Gotchas

1\. \`command\_timeout\` on the pool also applies to \`load\_ticker\_universe\` and to

\`register\_pgvector\` — do not set it below cold-start cost.

2\. \`async with pool.acquire(...)\` must release on every path; the fan-out at

\`nodes.py:453-469\` means one leaked connection starves the rest.

3\. \`context.py:54\` runs inside the FastAPI \`lifespan\` ABOVE the \`try:\` at

\`app.py:69\` — a breach there kills startup and has no \`ErrorEvent\` path.

That is acceptable (loud beats silent) but must not be mistaken for the

request-scoped path.

4\. Measurements were taken against a warm Neon endpoint. A cold autosuspend

resume is NOT represented; \`connect\_timeout\` at 30 must absorb it.
