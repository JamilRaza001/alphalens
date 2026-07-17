\# Spec S16 — Graph Wiring (\`graph\`)

\> Spec \*\*S16\*\* (graph\_wiring) · v8 cross-ref: §5.2 (Agent Loop), §7 (Node Responsibilities) · targets:

\> \`src/alphalens/agent/graph.py\` + \`src/alphalens/agent/context.py\` + \`scripts/run\_query.py\`

\> (+ small \`config.py\` / \`nodes.py\` / \`prompts.py\` piggybacks below).

\> Format: L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).

\> Consumes \*\*verbatim\*\*: S12 state (\`state.py\`), S13 nodes (\`nodes.py\` + \`prompts.py\`), S14 breaker, S15 retrieve.

\> \*\*This spec WIRES; it does not re-implement any node logic.\*\*

\> \*\*First real end-to-end live run lands here\*\* — jina-v3 \`$1::vector\` binding, SQL RRF (\`FULL OUTER JOIN\`, c=60),

\> and the breaker drain are all exercised against live Neon + Groq for the first time.

\---

\## Decisions applied (locked Sessions 30–31)

1\. \*\*D1(a) — Two-module split.\*\*

\`graph.py\` = \*\*pure topology\*\* (\`build\_graph() -> CompiledStateGraph\`): no I/O, no \`await\`, no resource

construction → importing it has \*\*zero side effects\*\*. \`context.py\` = \`async def build\_context()\` =

cold-start resource assembly \*\*and pool-lifecycle owner\*\*. \*Why:\* module-level resources would make

\`import\` = DB connect + 80 MB reranker load; the split turns cold-start into one named, testable seam.

2\. \*\*D1(b) — Agent pool = DIRECT endpoint + \`init=register\_pgvector\`\*\*, \`min\_size=1\`, \`max\_size=5\`,

\*\*prepared statements ON\*\* (no \`statement\_cache\_size=0\`). Endpoint is \*\*config-driven\*\* (\`agent\_db\_url\`)

so a future scale-flip to pooled + \`statement\_cache\_size=0\` is an \*\*env change, zero code change\*\*.

\*Why:\* AlphaLens is ~30–45 queries/day at \`max\_size=5\` — asyncpg's own pool on the direct endpoint

beats a PgBouncer path (whose prepared-statement trap + pgvector-OID indirection buy nothing here).

The ETL runner already runs this exact shape live → \*\*mirror it\*\* (see Gotcha G1 — verify, don't assume).

3\. \*\*D2 — Streaming = \`ainvoke()\` → drain \`final\_state\["answer\_stream"\]\` in the caller.\*\*

Native \`astream(stream\_mode="messages")\` \*\*rejected\*\* (breaker OPEN ⇒ no LLM call ⇒ message-mode yields

nothing on the degraded path). \`stream\_mode="custom"\` \*\*also rejected\*\* (couples nodes to the stream

writer, breaking S14's DI; plus a known async \`get\_stream\_writer\` gotcha on langgraph 1.0.x). The breaker

gate fires \*\*lazily at drain-time\*\* (S14). In v1 the \*\*run harness is the caller\*\*; the real SSE endpoint

lands in the deployment/observability spec. Node-progress (\`stream\_mode="updates"\`) is \*\*deferred\*\* there too.

4\. \*\*D3(a) — \`allowed\_tickers\` DB-derived\*\* at cold-start: one \`SELECT ticker, name FROM companies\` →

builds \*\*both\*\* \`allowed\_tickers: frozenset\[str\]\` (the hard input-rail) \*\*and\*\* a \`ticker\_roster\`

(ticker→name) injected into the Plan system prompt for \*\*grounded\*\* word→ticker resolution (not the LLM's

parametric guess). \*Why:\* the universe is DB-defined and can change; grounding removes a hallucination surface.

5\. \*\*D3(b) — Input-rail enforced inside the Plan node, output-side\*\* (on the LLM's resolved tickers), not in a

separate guard node. \*Why:\* the rail's input (resolved tickers) only exists after Plan; a linear graph needs

no extra hop for one membership check. \*\*Reconciliation with the linear-graph lock:\*\* v1 stays strictly

linear (no conditional edges). Partial-unavailable queries → \*\*drop-and-note\*\* (keep valid tickers, surface

the dropped ones). Total-unavailable queries → flow through with empty tickers and produce an \*\*honest

low-confidence "no coverage" answer\*\* via each node's empty-handling (retrieve→\`\[\]\`, rerank→\`\[\]\`,

evaluate→\`low\`/\`coverage\`, synthesize→honesty rail). A conditional early-\`END\` short-circuit is a \*\*v-next\*\*

enhancement, intentionally \*\*not\*\* added here.

6\. \*\*D4 — Run harness = \`scripts/run\_query.py\` (primary)\*\* + a \*\*live-marked\*\* integration test (secondary).

The test is authored \*\*from the observed shape of the first live run\*\*, not blind; it asserts \*\*structural

invariants only\*\* (answer non-empty, citations present, tickers ⊆ \`allowed\_tickers\`, status ∈ {ok,degraded}),

is \`@pytest.mark.live\` and \*\*excluded from default CI\*\*. \*Why:\* the first run is exploratory (Synthesize is

free-form, non-deterministic); the script gives human-inspected proof, the test is a later regression net.

\---

\## Piggyback additions (applied alongside S16)

\- \*\*\`config.py\`\*\* — \`agent\_db\_url: SecretStr\` (defaults to \`neon\_direct\_url\` via validator; env \`AGENT\_DB\_URL\`

overrides for the scale-flip), \`agent\_pool\_min\_size: int = 1\`, \`agent\_pool\_max\_size: int = 5\`.

\- \*\*\`nodes.py\`\*\* — extend \`AgentContext\` with \`ticker\_roster: Mapping\[str, str\]\` (NEW, S16/D3a). This is the

4th cumulative extension after S14 (\`breaker\`) and S15 (\`embedder\`); the dataclass now holds

\`llm, reranker, pool, allowed\_tickers, breaker, embedder, ticker\_roster\`.

\- \*\*\`prompts.py\`\*\* — extend \`build\_plan\_system\_prompt(allowed\_tickers, ticker\_roster)\` to bake the ticker→name

map into the (still byte-stable, cache-friendly) system prompt.

\- \*\*\`nodes.py\` / \`plan\_node\`\*\* — pass \`runtime.context.ticker\_roster\` into \`build\_plan\_system\_prompt\`.

(The hard \`validate\_tickers\` gate is unchanged — D3b keeps it output-side on the resolved plan.)

\---

\## Goal

Wire the five already-built, decoupled nodes (Plan → Retrieve → Rerank → Evaluate → Synthesize) into one

\*\*linear, single-pass LangGraph 1.0 \`StateGraph\`\*\* — no conditional edges, no Refine loop, no checkpointer —

and assemble the \`AgentContext\` those nodes depend on \*\*once at cold-start\*\*. Topology (\`graph.py\`) is kept

strictly separate from resource assembly (\`context.py\`) so that importing the graph is side-effect-free and

the cold-start path is a single named seam that owns the asyncpg pool's lifecycle. A run harness

(\`scripts/run\_query.py\`) exercises the whole pipeline end-to-end against live Neon + Groq — the first time the

jina-v3 vector binding, the SQL RRF fusion, the \`chunks→filings\` join, and the breaker's lazy drain run

outside fake pools. This spec produces the runnable v1 agent; the SSE HTTP surface and UX progress events are

deferred to the deployment/observability spec.

\---

\## Function Signatures

\`\`\`python

\# ── src/alphalens/agent/graph.py ── PURE TOPOLOGY · import-safe · no I/O, no await ──

from langgraph.graph import START, END, StateGraph

from langgraph.graph.state import CompiledStateGraph # G5: confirm exact import path on installed 1.0.x

from alphalens.agent.state import AgentState

from alphalens.agent.nodes import (

AgentContext,

plan\_node, retrieve\_node, rerank\_node, evaluate\_node, synthesize\_node, # G5: confirm plan/rerank/evaluate names

)

def build\_graph() -> CompiledStateGraph:

"""Wire the 5 nodes into a linear single-pass graph and compile WITHOUT a checkpointer.

Pure topology: no DB, no await, no resource construction — importing this module is side-effect-free.

\`context\_schema=AgentContext\` (LangGraph 1.0 — NOT the deprecated \`config\_schema\`) declares the DI shape;

deps are injected later at \`graph.ainvoke(..., context=ctx)\`."""

builder = StateGraph(AgentState, context\_schema=AgentContext)

builder.add\_node("plan", plan\_node)

builder.add\_node("retrieve", retrieve\_node)

builder.add\_node("rerank", rerank\_node)

builder.add\_node("evaluate", evaluate\_node)

builder.add\_node("synthesize", synthesize\_node)

builder.add\_edge(START, "plan")

builder.add\_edge("plan", "retrieve")

builder.add\_edge("retrieve", "rerank")

builder.add\_edge("rerank", "evaluate")

builder.add\_edge("evaluate", "synthesize")

builder.add\_edge("synthesize", END)

return builder.compile() # no checkpointer → v1 stateless single-pass

\`\`\`

\`\`\`python

\# ── src/alphalens/agent/context.py ── COLD-START ASSEMBLY + POOL LIFECYCLE OWNER ──

from collections.abc import Mapping

import asyncpg

from asyncpg import Pool

from langchain\_groq import ChatGroq

from sentence\_transformers import CrossEncoder

from alphalens.config import Settings, get\_settings

from alphalens.agent.nodes import AgentContext

from alphalens.agent.circuit\_breaker import SynthesisCircuitBreaker # S14

from alphalens.etl.embeddings import EmbeddingClient # S8 (jina-v3 query embeddings)

from alphalens.etl.upsert import register\_pgvector # S9 — REUSE, do NOT reimplement

async def build\_agent\_pool(settings: Settings) -> Pool:

"""Create the agent asyncpg pool on the DIRECT Neon endpoint with the pgvector codec registered on

EVERY new connection via \`init\` (D1b). MIRRORS etl/runner.py pool construction — verify first (G1)."""

return await asyncpg.create\_pool(

dsn=settings.agent\_db\_url.get\_secret\_value(),

min\_size=settings.agent\_pool\_min\_size, # 1

max\_size=settings.agent\_pool\_max\_size, # 5

init=register\_pgvector, # list\[float\] -> $1::vector (G2)

)

async def load\_ticker\_universe(pool: Pool) -> tuple\[frozenset\[str\], Mapping\[str, str\]\]:

"""One cold-start query on \`companies\` → (allowed\_tickers, ticker\_roster) (D3a).

frozenset = the hard input-rail; roster (ticker→name) = injected into the Plan prompt for grounding."""

rows = await pool.fetch("SELECT ticker, name FROM companies")

roster: dict\[str, str\] = {r\["ticker"\]: r\["name"\] for r in rows}

return frozenset(roster), roster

async def build\_context(settings: Settings | None = None) -> tuple\[AgentContext, Pool\]:

"""Assemble ALL per-run deps ONCE at cold-start; return (context, pool).

Pool is returned SEPARATELY so the caller owns teardown (\`await pool.close()\`). Everything here is

import-safe only because it lives behind this async function, never at module scope (D1a)."""

settings = settings or get\_settings()

pool = await build\_agent\_pool(settings)

allowed\_tickers, ticker\_roster = await load\_ticker\_universe(pool)

llm = ChatGroq(model=settings.groq\_model) # per-call temp=0 already set inside Plan/Evaluate (S13)

reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2") # L3/L14, ~80 MB — confirm S13/S14 load path

embedder = EmbeddingClient(settings=settings) # G6: confirm S8 constructor deps

breaker = SynthesisCircuitBreaker(settings=settings) # G6: confirm S14 constructor deps

ctx = AgentContext(

llm=llm, reranker=reranker, pool=pool,

allowed\_tickers=allowed\_tickers, breaker=breaker,

embedder=embedder, ticker\_roster=ticker\_roster, # NEW field (S16/D3a)

)

return ctx, pool

\`\`\`

\`\`\`python

\# ── scripts/run\_query.py ── D4 live end-to-end harness · v1 stand-in for the SSE caller ──

import argparse

import asyncio

import time

from uuid import uuid4

from alphalens.agent.context import build\_context

from alphalens.agent.graph import build\_graph

from alphalens.agent.state import AgentState

async def run\_query(question: str, \*, user\_id: str | None = None) -> None:

"""Cold-start → build graph → ainvoke → DRAIN answer\_stream (D2) → print answer + citations + timing.

The FIRST real live run: jina-v3 binding, SQL RRF, breaker drain all fire here."""

ctx, pool = await build\_context()

try:

graph = build\_graph()

intake: AgentState = { # G4: exact required intake keys

"original\_query": question,

"query": question, # == original\_query in v1 (L6)

"request\_id": str(uuid4()),

"user\_id": user\_id,

"iteration": 0,

}

t0 = time.monotonic()

final\_state = await graph.ainvoke(intake, context=ctx) # graph runs; answer\_stream stays UN-DRAINED (G3)

print("\\n--- ANSWER ---")

async for token in final\_state\["answer\_stream"\]: # D2 drain: breaker gate/fallback evaluated HERE

print(token, end="", flush=True)

print("\\n\\n--- CITATIONS ---")

for c in final\_state\["citations"\]:

print(f" \[{c.ticker} {c.filing\_type} {c.period\_year}\] {c.section} ({c.chunk\_id})")

print(

f"\\nconfidence={final\_state\['confidence'\]} reason={final\_state\['confidence\_reason'\]} "

f"unavailable={final\_state\['unavailable\_tickers'\]} "

f"latency={time.monotonic() - t0:.2f}s"

)

finally:

await pool.close() # script owns teardown — mirror etl/runner (G7)

def main() -> None:

ap = argparse.ArgumentParser(description="AlphaLens live agent query harness (S16).")

ap.add\_argument("question")

ap.add\_argument("--user-id", default=None)

args = ap.parse\_args()

asyncio.run(run\_query(args.question, user\_id=args.user\_id))

if \_\_name\_\_ == "\_\_main\_\_":

main()

\`\`\`

\`\`\`python

\# ── tests/agent/test\_graph.py ── D1a topology test · PURE, no resources ──

def test\_build\_graph\_topology() -> None:

"""Compiles with no checkpointer; the 5 nodes and the linear START→…→END edges are present.

Uses NO DB / LLM / reranker — proves graph.py is import-safe and topology-only."""

\# ── tests/integration/test\_agent\_live.py ── D4 live regression net · authored FROM observed shape ──

import pytest

@pytest.mark.live # excluded from default CI; needs live Neon + Groq creds + network

async def test\_agent\_end\_to\_end\_live() -> None:

"""Structural invariants only (NOT content): answer non-empty, ≥1 citation, every citation.ticker and

every plan ticker ∈ allowed\_tickers, status ∈ {ok, degraded}. Written after the first run\_query.py run."""

\`\`\`

\---

\## Acceptance Criteria

1\. \*\*Import-safety (D1a).\*\* \`import alphalens.agent.graph\` performs \*\*no\*\* DB connection, no model load, no

\`await\` — verifiable by importing with no \`.env\`/network present and asserting it does not raise or connect.

2\. \*\*Topology (D1a).\*\* \`build\_graph()\` returns a compiled graph whose nodes are exactly

\`{plan, retrieve, rerank, evaluate, synthesize}\` and whose edges form the single linear chain

\`START → plan → retrieve → rerank → evaluate → synthesize → END\`. No conditional edges.

3\. \*\*No checkpointer (D1/D2).\*\* The graph is compiled \*\*without\*\* a checkpointer (v1 stateless single-pass).

4\. \*\*LangGraph 1.0 API.\*\* Construction uses \`StateGraph(AgentState, context\_schema=AgentContext)\` — the

deprecated \`config\_schema\` does \*\*not\*\* appear anywhere. Deps reach nodes only via \`ainvoke(..., context=ctx)\`.

5\. \*\*Pool construction (D1b).\*\* \`build\_agent\_pool\` creates the pool on \`settings.agent\_db\_url\` (direct) with

\`init=register\_pgvector\`, \`min\_size=1\`, \`max\_size=5\`, and \*\*no\*\* \`statement\_cache\_size=0\`. \`agent\_db\_url\`

defaults to \`neon\_direct\_url\` and is env-overridable.

6\. \*\*\`register\_pgvector\` reuse (D1b).\*\* The pool \`init\` is the \*\*S9\*\* \`register\_pgvector\` imported from

\`alphalens.etl.upsert\` — not a reimplementation.

7\. \*\*Cold-start seam (D1a).\*\* \`build\_context()\` returns \`(AgentContext, Pool)\`; the pool is returned separately

and the caller (harness) closes it in a \`finally\`. All six S13/S14/S15 context fields plus the new

\`ticker\_roster\` are populated.

8\. \*\*Ticker universe (D3a).\*\* \`load\_ticker\_universe\` issues exactly one \`SELECT ticker, name FROM companies\`

and returns a \`frozenset\` of tickers plus a ticker→name \`Mapping\`; the frozenset feeds \`allowed\_tickers\`

and the roster is injected into the Plan system prompt.

9\. \*\*Grounded resolution (D3a/D3b).\*\* \`build\_plan\_system\_prompt\` now receives the roster; \`plan\_node\` passes

\`runtime.context.ticker\_roster\`. The hard \`validate\_tickers\` gate still runs output-side on the resolved

plan (unchanged from S13).

10\. \*\*Streaming (D2).\*\* The harness calls \`graph.ainvoke(intake, context=ctx)\` and then drains

\`final\_state\["answer\_stream"\]\` with \`async for\`. No \`astream\` / \`stream\_mode\` is used anywhere in S16.

11\. \*\*Un-drained invariant (D2).\*\* After \`ainvoke\` returns, \`answer\_stream\` has produced \*\*no\*\* tokens yet

(drain begins only in the harness loop) — verifiable in the live run by observing the first token prints

after the "--- ANSWER ---" banner, not during graph execution.

12\. \*\*Harness output (D4).\*\* \`run\_query.py\` prints the streamed answer, the citations list, and a footer with

\`confidence\`, \`confidence\_reason\`, \`unavailable\_tickers\`, and latency; it exits 0 on success and closes the

pool on every path.

13\. \*\*Live multi-cell proof (D4).\*\* The documented first run uses a ≥2-ticker × ≥2-year query (e.g. Apple vs

Microsoft, 2023 vs 2024) so per-cell fan-out + RRF + join are actually exercised, and returns a cited answer.

14\. \*\*Live test shape (D4).\*\* \`test\_agent\_end\_to\_end\_live\` is \`@pytest.mark.live\`, excluded from the default

\`pytest\` run, and asserts only the structural invariants in its docstring — authored after the first live run.

15\. \*\*Gates.\*\* \`ruff\` clean, \`mypy --strict\` clean (incl. the new \`AgentContext.ticker\_roster\` field and the

\`build\_context\` return tuple); the pure topology test passes in default CI with no external resources.

\---

\## Gotchas (live-verify checkpoints — S28 discipline)

\- \*\*G1 — Verify the ETL runner's ACTUAL endpoint before locking the agent default.\*\* Doc tension: S4 gotcha

(L12) says "app code uses the \*\*pooled\*\* URL," but D1b locks the agent pool to the \*\*direct\*\* endpoint.

\`etl/runner.py\` (≈ line 133) is \*\*ground truth\*\* — read its exact \`create\_pool\` args (endpoint, \`init\`,

\`statement\_cache\_size\`, sizes) and \*\*mirror them\*\*. If the runner is pooled, that is the signal to reconcile

D1b, not to assume. \*\*Do this as S16 step 1.\*\* Assume nothing (S28 lesson).

\- \*\*G2 — \`register\_pgvector\` only surfaces on a LIVE run.\*\* It runs per-connection via the \`init\` callback;

fake-pool unit tests \*\*never exercise it\*\*. The \`list\[float\] → $1::vector\` binding (and therefore the whole

RRF query) is first proven in \`run\_query.py\`, not in the topology test.

\- \*\*G3 — \`ainvoke\` must leave \`answer\_stream\` un-drained.\*\* Confirm no eager consumption during graph

execution. No checkpointer → nothing serializes state → nothing should touch the generator before the harness

drains it. Tokens appearing during graph execution ⇒ the seam is wrong.

\- \*\*G4 — Drain in the SAME event loop / request scope where \`ctx\` + \`pool\` are alive.\*\* The lazy stream's

closures hold \`reranked\_chunks\` + \`llm\` + messages; the pool must be open when drain runs. The harness does

\`ainvoke\` + drain inside one coroutine → safe. The future Lambda handler must do the same (one invocation).

\- \*\*G5 — Confirm the installed LangGraph 1.0.x API surface.\*\* Exact \`CompiledStateGraph\` import path, the

\`StateGraph(state, context\_schema=...)\` signature, \`ainvoke(..., context=ctx)\`, and the node function names

in \`nodes.py\` (\`retrieve\_node\`/\`synthesize\_node\` confirmed; verify \`plan\_node\`/\`rerank\_node\`/\`evaluate\_node\`).

\- \*\*G6 — Confirm constructor deps for \`EmbeddingClient\` (S8) and \`SynthesisCircuitBreaker\` (S14)\*\* before

wiring cold-start; the \`settings=\` shapes above are assumptions to verify.

\- \*\*G7 — Mirror the ETL runner's teardown pattern\*\* (\`async with create\_pool(...)\` vs \`create\_pool(...)\` +

\`finally: await pool.close()\`). Do not invent a new lifecycle style.

\- \*\*D3b/linear reconciliation (deliberate v1 choice).\*\* Total-unavailable queries are handled by graceful

empty-flow + honest synthesis, \*\*not\*\* an early \`END\`. Keep each node's empty-input handling intact — that is

what prevents a silent mis-answer without adding a conditional edge.
