# Spec S18 — FastAPI + SSE Local App (`api`)

> Spec **S18** (fastapi_sse_local) · v8 cross-ref: §4 (API row — "FastAPI + Server-Sent Events"), §13.3 Phase 1.C · targets:
> `src/alphalens/api/app.py` (+ `src/alphalens/api/schemas.py`, `scripts/run_api.py`)
> Format: L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).
> Consumes **verbatim**: S16 `build_graph()` / `build_context()`, S12 state, S14 breaker drain semantics.
> **This spec WRAPS; it does not re-implement any node, graph, or streaming logic.**
>
> **Provenance.** Added by the 12 Aug 2026 amendment (v8 §13.4a). The SSE surface was originally scoped
> inside spec 14 (`14_lambda_deployment.md`); the 31 Jul 2026 deferral moved spec 14 to v2 as one block,
> which unintentionally carried the *local* app concern with the *Lambda deployment* concern. S18 splits
> them: the local app is v1 (S19 depends on it), the Lambda/OIDC/IAM surface stays v2.

---

## Decisions applied

1. **D1 — Wrap-only scope.** `app.py` builds a FastAPI app around the existing `build_graph()` +
   `build_context()` seam. **No node logic, no graph topology, no retrieval, no prompt changes.**
   If this spec needs to touch `nodes.py`, `graph.py`, `prompts.py`, or `state.py`, that is a signal to
   stop and re-scope — not to edit them.

2. **D2 — Cold-start seam = FastAPI `lifespan`, not per-request.** `build_context()` runs **once** on app
   startup and its `(AgentContext, Pool)` is stashed on `app.state`; the pool closes in the lifespan's
   teardown. *Why:* `build_context()` opens an asyncpg pool **and** loads the ~80 MB reranker
   (S16 D1a). Doing that per request would add seconds of latency to every query and open a new pool
   per request. This mirrors `run_query.py`'s `try/finally` ownership (S16 G7), lifted from
   script-scope to app-scope.

3. **D3 — Streaming inherits S16 D2 verbatim: `ainvoke()` → drain `final_state["answer_stream"]`.**
   Native `astream(stream_mode="messages")` and `stream_mode="custom"` remain **rejected** for the
   reasons S16 locked (breaker OPEN ⇒ no LLM call ⇒ message-mode yields nothing on the degraded path;
   `custom` couples nodes to a stream writer and breaks S14's DI). The breaker gate still fires
   **lazily at drain time** — inside the SSE generator, not during `ainvoke`.

4. **D4 — Two-phase metadata (LOCKED by Jamil, 12 Aug 2026).** The client receives metadata in **two**
   events, not one: a `meta` event **before** any answer token, and a `citations` event **after** the
   last one.
   **Known limitation, accepted:** because of D3, `meta` is emitted when `ainvoke` *returns* — i.e. after
   the whole graph has run — **not** when the Plan node finishes. The user therefore sees
   `unavailable_companies` before the prose starts, but not at ~2s. Emitting it at plan-time would
   require breaking S16 D2; that is **out of scope here and not scheduled**.
   **Amended 13 Aug 2026 (Q1):** degraded-ness is NOT part of the two-phase metadata. It is not
   knowable at `meta` time — S14's breaker gate fires lazily on the first `__anext__` of the answer
   stream (`circuit_breaker.py:125`), which under D3 happens strictly after `meta` is emitted. A
   breaker that reads CLOSED at `meta` time can still hard-fail at connect and serve the degraded
   fallback, so any `meta`-time flag would be a claim the run has not yet earned. It moves to
   `DoneEvent.degraded`, read from the breaker **after** the drain completes.

5. **D5 — `POST /query`, SSE response body; browser `EventSource` is NOT usable.** The native
   `EventSource` API is GET-only and cannot send a request body. Putting a free-text financial question
   in a query string is wrong (length limits, logging of user input in access logs, encoding noise).
   `POST` + SSE body is the standard modern shape. **S19 consequence, pinned here so it is not
   discovered late:** the frontend must consume this with `fetch()` + `ReadableStream`, or a
   fetch-based SSE client — **not** `new EventSource(...)`.

6. **D6 — `sse-starlette`'s `EventSourceResponse`, not hand-rolled `StreamingResponse`.** *Why:* it
   handles the parts that are easy to get subtly wrong — SSE framing (`event:` / `data:` / blank-line
   terminator), periodic keepalive comments so idle proxies don't kill the connection, and client
   disconnect detection so a closed tab doesn't leave a generator draining a Groq stream. Rolling this
   by hand is ~100 lines of protocol edge cases for zero gain.
   **Amended 13 Aug 2026 (Q8): every event's `data:` is JSON-encoded — `token` included, as
   `{"text": "..."}`.** Raw text in `data:` is not newline-safe: SSE splits the payload on `\n` and
   the client rejoins the parts, so a token that is exactly `"\n"`, or one that ends in a newline,
   does not round-trip. This is not hypothetical — `degraded_stream` (`nodes.py:724`) emits
   newline-heavy chunks and `LOW_CONFIDENCE_CAVEAT` ends in `"\n\n"`. One encoding for all five
   event types also means the S19 client has exactly one parse path.

7. **D7 — No auth, no OIDC, no rate limiting.** Local dev only, bound to `127.0.0.1`. The Vercel OIDC
   middleware (L11), Function URL `auth=NONE`, and IAM belong to spec 14 (v2). **Do not add a stub
   auth layer "for later"** — an unused, untested auth path is worse than none.

8. **D8 — CORS: explicit allowlist, no wildcard.** Next.js dev runs on `http://localhost:3000`, the API
   on `http://127.0.0.1:8000` — different origins, so the browser blocks the request without CORS.
   Origins arrive as the `create_app(cors_origins=...)` **parameter**, defaulting to the two
   localhost forms. **Amended 13 Aug 2026 (Q6/Q7):** the original text sourced them from a config
   field `api_cors_origins`, which does not exist in `Settings` — and adding one would put a
   `get_settings()` call inside `create_app()`, validating the entire environment (and raising
   `ValidationError` with no `.env`) at construction time, in direct conflict with AC1. No new config
   field is added. `allow_origins=["*"]` is **not** used, so the shape stays honest when this
   eventually sits behind a real domain.

---

## Goal

Put the existing v1 agent behind a local HTTP surface that streams. Today the only way to run a query is
`scripts/run_query.py`, a CLI that prints to a terminal — which no browser can consume, so S19 (the local
Next.js frontend) has nothing to talk to. S18 adds a thin FastAPI application that owns the cold-start
resources for the process lifetime, accepts a question over `POST /query`, and returns a Server-Sent
Events stream carrying: the honesty-rail metadata first, then the answer token-by-token as the model
produces it, then the citation list. The application is a **wrapper** — every retrieval, reranking,
evaluation, and honesty decision continues to happen exactly where it already happens. Deliberately
excluded: authentication, deployment, containerisation, and observability wiring.

---

## Function Signatures

```python
# ── src/alphalens/api/schemas.py ── wire types only · no logic ──
from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """Request body for POST /query."""

    question: str = Field(min_length=1, max_length=2000)
    user_id: str | None = None


class GapCell(BaseModel):
    """Wire form of a live `tuple[str, int]` (ticker, year) cell.

    `AgentState.coverage_gaps` / `.capacity_drops` are `list[tuple[str, int]]` (state.py:199,
    :204). JSON has no tuple, and a positional 2-element array forces every consumer to
    remember which slot is which — so the pair is named on the wire."""

    ticker: str
    year: int


class MetaEvent(BaseModel):
    """Payload of the `meta` SSE event — emitted ONCE, before the first answer token.

    Mirrors the run_query.py footer keys (37e4b41) so the CLI and the HTTP surface never
    drift into reporting different things about the same run — see AC8 for the exact
    intersection contract and its two documented exceptions.

    `capacity_drops` is a write-only v1 state key: `evaluate_node` writes it and NO node
    reads it (state.py:204). It is carried here for footer parity only. The key that IS
    consumed — `dropped_for_capacity` (state.py:191, read by `synthesize_node`'s honesty
    rail) — is deliberately NOT on the wire; see Out of Scope."""

    request_id: str
    confidence: str                    # "low" | "high"
    confidence_reason: str             # "coverage" | "llm" | "none"  (live Literal, state.py:198 — no "capacity" member)
    coverage_gaps: list[GapCell]
    capacity_drops: list[GapCell]
    unavailable_tickers: list[str]
    unavailable_years: list[int]
    unavailable_companies: list[str]
    plan_tickers: list[str]            # `[]` when query_plan is unset (see AC9)


class CitationOut(BaseModel):
    """One entry of the `citations` SSE event — emitted ONCE, after the last token."""

    chunk_id: str
    ticker: str
    filing_type: str
    period_year: int
    section: str | None


class DoneEvent(BaseModel):
    """Payload of the terminal `done` event."""

    latency_s: float
    token_count: int
    degraded: bool        # breaker OPEN — resolved AFTER drain, not at meta time (Q1)


class ErrorEvent(BaseModel):
    """Payload of the `error` event. Emitted IN-BAND (see G2) — the HTTP status is
    already 200 by the time the stream is live, so failures cannot use a status code."""

    message: str          # operator-safe summary; NEVER a raw traceback or DB URL
    phase: str            # "graph" | "stream"


# ── src/alphalens/api/app.py ── FastAPI wrapper · owns cold-start for the process ──
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """D2 cold-start seam. Builds the graph + context ONCE, stashes them on `app.state`,
    and closes the pool on shutdown — the app-scope twin of run_query.py's try/finally."""


def create_app(cors_origins: list[str] | None = None) -> FastAPI:
    """App factory. Registers the lifespan, CORS (D8), and the two routes.

    Factory rather than a module-level `app = FastAPI()` so importing this module has no
    side effects and tests can build a fresh instance with a stubbed context.

    `cors_origins` is a PARAMETER, not a config read: `CORSMiddleware` must be registered at
    construction time, and reaching for `get_settings()` here would validate the whole
    environment inside `create_app()` — raising `ValidationError` with no `.env` present and
    breaking AC1. Defaults to `["http://localhost:3000", "http://127.0.0.1:3000"]`."""


async def query_stream(app: FastAPI, req: QueryRequest) -> AsyncIterator[dict[str, str]]:
    """The SSE generator: run the graph, emit `meta`, drain the answer stream emitting one
    `token` event per chunk, then `citations`, then `done`.

    Yields sse-starlette event dicts ({"event": ..., "data": ...}). Any exception is caught
    and converted into a terminal `error` event — this generator never raises to the caller."""


# ── scripts/run_api.py ── dev entrypoint ──
def main() -> None:
    """uvicorn runner bound to 127.0.0.1:8000 (D7). Not an ASGI production server config —
    that lands in the v2 Lambda spec."""
```

---

## Acceptance Criteria

1. **Import-safety.** `import alphalens.api.app` performs no DB connection, no model load, and no
   `await` — verifiable by importing with no `.env` and no network present. `create_app()` is likewise
   side-effect-free; resources are acquired only when the lifespan enters.

2. **Single cold start (D2).** Across N sequential requests to a running app, `build_context()` is
   called exactly **once** and exactly **one** asyncpg pool is created — asserted with a counting
   spy over the lifespan, not by inspection.

3. **Pool teardown.** The pool is closed exactly once on app shutdown, including when a request is
   in flight and when a request raised. No `Pool` object survives lifespan exit.

4. **Event order is invariant.** For any successful request the event sequence is exactly:
   `meta` → `token`\* → `citations` → `done`. `meta` never appears after a `token`; `done` is always
   last; `citations` is emitted even when the list is empty.

5. **Un-drained invariant preserved (S16 AC11).** No `token` event is emitted before `ainvoke` returns.
   The drain — and therefore S14's lazy breaker gate — happens inside the SSE generator.

6. **Degraded path streams.** With the breaker forced OPEN, the request still returns 200 with a
   well-formed event sequence, the degraded preamble arrives as `token` events, and the terminal
   event carries **`DoneEvent.degraded is True`** — asserted there, **not** on `meta`, which cannot
   know it (D4 amendment / Q1). **No LLM call is made** — pinned by a fake whose `astream` raises
   `AssertionError` if touched. That fake already exists and is reusable: `_NoStreamLLM`
   (`tests/unit/test_circuit_breaker.py:363`, same guard shape as S_CR Phase 4). Force OPEN with the
   established pattern — `SynthesisCircuitBreaker(failure_threshold=1, …)` then
   `_record_hard_failure()`.

7. **In-band errors (G2).** An exception raised during `ainvoke` yields a single `error` event with
   `phase="graph"` and no `token` events; an exception raised mid-drain yields the tokens emitted so far
   followed by an `error` event with `phase="stream"`. In both cases HTTP status is 200 and the
   generator does not propagate. `ErrorEvent.message` contains no traceback, no DSN, and no DB URL.

8. **Footer parity (INTERSECTION, amended 13 Aug 2026).** The **intersection** of `MetaEvent`'s
   fields and `run_query.py`'s footer keys carries **identical values for the same run**. Two
   documented exceptions, both directional and both intentional:
   - `request_id` — `MetaEvent`-only; the footer (`run_query.py:70-80`) does not print it.
   - `latency` — footer-only; on the wire it is `DoneEvent.latency_s`, because latency is not known
     until the drain finishes.

   The original wording ("every key in `MetaEvent` is present in the footer") was false at HEAD: the
   footer prints 9 keys and lacks `request_id`. Note also that the footer's label is `reason=` while
   both the state key and the wire field are `confidence_reason` — the label is not part of the
   contract (G4). A test pins the intersection so the two surfaces cannot drift; this is the exact
   drift class that produced the `unavailable=` / `unavailable_tickers` bug (`37e4b41`).

9. **`plan_tickers` normalisation.** When `query_plan` is unset (early-exit path), `MetaEvent.plan_tickers`
   is `[]`, **not** `None`. This deliberately diverges from `run_query.py`, which prints `None` — the
   known-but-unactioned behaviour flagged on 12 Aug. A wire contract that changes type on an edge case
   forces every consumer into a null check; the CLI can keep printing `None`.

10. **Client disconnect is clean.** If the client closes the connection mid-stream, the generator stops,
    the underlying answer stream is closed, and no exception is logged at ERROR level. The pool is
    unaffected and the next request succeeds.

11. **CORS (D8).** A preflight `OPTIONS /query` from `http://localhost:3000` succeeds; one from an
    unlisted origin does not receive permissive CORS headers. `allow_origins` never contains `"*"`.

12. **Health.** `GET /health` returns 200 with `{"status": "ok"}` **without touching the DB, the LLM, or
    the reranker.** *Why liveness-only:* a health check that queries Neon turns a DB blip into a
    restart loop, and readiness semantics only matter once something is orchestrating the process —
    a v2 concern.

13. **Validation.** An empty `question` returns 422 (Pydantic), **before** the SSE stream opens — so the
    client gets a real status code, not an in-band `error` event. Confirms that request validation and
    stream errors are handled in different places.

14. **Gates.** `ruff` clean, `mypy --strict` clean, full `pytest` green. All new tests use fakes —
    **zero Groq calls, zero Neon connections** in the default suite. Any live check is
    `@pytest.mark.live` and excluded by default.

---

## Gotchas (live-verify checkpoints — S28 discipline)

- **G1 — `EventSource` will not work; confirm the S19 client shape early.** D5 makes `/query` a POST.
  The browser's built-in `EventSource` cannot POST, so a frontend written against it will fail with a
  confusing CORS-shaped error rather than an obvious "wrong method". **Verify during S18, not S19:**
  hit the endpoint with `curl -N -X POST` and confirm events arrive incrementally, then note in the
  S19 spec that the client is `fetch` + `ReadableStream`.

- **G2 — Once the stream opens, HTTP status is spent.** SSE sends `200 OK` and the headers the moment
  the first byte flushes. Anything that fails after that point **cannot** become a 500 — the client
  would see a truncated but nominally successful stream. This is why AC7 mandates in-band `error`
  events, and why AC13 puts validation *before* the stream. The rule: **fail before you flush, or fail
  in-band.**

- **G3 — Buffering will silently defeat streaming.** Tokens can arrive at the browser in one lump if
  anything between the generator and the client buffers — a reverse proxy, gzip middleware, or a client
  reading with `.text()` instead of a stream reader. **Do not add compression middleware to this app.**
  Verify with `curl -N`: tokens must appear progressively. If they arrive all at once, the bug is
  buffering, not the generator.

- **G4 — Confirm the live `AgentState` keys before writing `MetaEvent`.** `MetaEvent` above is written
  from the 12 Aug footer (`37e4b41`), which is doc-sourced. Read `state.py` and `run_query.py` at HEAD
  and reconcile field-by-field **before** implementing. If a key was renamed, the spec is stale and the
  code is ground truth. (`confidence_reason` is a known trap: the footer label is `reason=` while the
  state key is `confidence_reason` — the wire contract uses the **state key**.)

- **G5 — Groq free tier has no concurrency budget.** Nothing here serialises requests, so two browser
  tabs issuing queries at once can trip a rate limit that `run_query.py` never hit, surfacing as a
  breaker trip rather than an obvious 429. Acceptable for local single-user dev; **flag, do not fix.**
  A concurrency limiter is a deployment concern (v2).

---

## Out of Scope / Deferred

- **Auth / Vercel OIDC / PyJWT middleware** (L11) → spec 14, **v2**.
- **Dockerfile, ECR, Lambda Web Adapter, Function URL** → spec 13 + 14, **v2**.
- **Opik / Sentry / CloudWatch wiring** → spec 16, **v2**.
- **The Next.js client itself** → **S19**. S18 ends at the wire protocol.
- **Progress events per node** (`plan_started`, `retrieve_done`, …) — requires breaking S16 D2 (see D4's
  known limitation). Not scheduled.
- **Persisting queries to the `queries` table** — the table exists (S2) but no spec writes to it; adding
  that here would smuggle a second concern into a wrapper spec.
- **Concurrency limiting / request queueing** (G5) → v2.
- **Citation marker format normalisation** — the CJK `【7】` instability is a known v2 item and belongs to
  whichever spec owns marker parsing, not to the transport.
- **`dropped_for_capacity` on the wire** (Q4). `MetaEvent` carries `capacity_drops` for footer parity
  and nothing else. Exposing `dropped_for_capacity` (`state.py:191`) too would put two
  near-identically-named capacity lists in front of S19 with no stated difference between them, and
  the footer does not print it. Revisit when a consumer actually needs the distinction between
  "trimmed by budget" and "flagged as budget-caused coverage miss".

---

## Amendment (13 Aug 2026 — post-recon)

**G4 fired.** The recon gate this spec set for itself found the wire contract stale: **4 of the 10
original `MetaEvent` fields did not match live `AgentState`**. The spec was authored from the 12 Aug
`run_query.py` footer (`37e4b41`) and the v8 design doc rather than from `src/alphalens/agent/state.py`
at HEAD, so the footer's *printed shape* was mistaken for the state's *actual types*. Live code is
ground truth; the spec has been corrected, not the code.

| Field | Spec said | Live at HEAD | Resolution |
|---|---|---|---|
| `status` | `str`, `"ok" \| "degraded"` | **No such key** — and no degraded flag under any name. Degradation lives in `ctx.breaker.state` and is evaluated lazily at drain (`circuit_breaker.py:125`), which under D3 is strictly after `meta` | **Removed** from `MetaEvent`; moved to `DoneEvent.degraded: bool`, read after the drain (Q1) |
| `confidence_reason` | `"coverage" \| "capacity" \| "llm" \| "none"` | `Literal["coverage", "llm", "none"]` (`state.py:198`) — **no `"capacity"` member**; the fourth value was invented | Comment corrected to the live 3 (Q2) |
| `coverage_gaps` | `list[str]` | `list[tuple[str, int]]` (`state.py:199`) | `list[GapCell]` — `{"ticker": …, "year": …}` (Q3) |
| `capacity_drops` | `list[str]` | `list[tuple[str, int]]` (`state.py:204`) | `list[GapCell]`; key retained for footer parity, `dropped_for_capacity` stays off the wire (Q3/Q4) |

Three further corrections came out of the same pass:

- **AC8 was unsatisfiable as written.** "Every key in `MetaEvent` is present in the footer" is false
  at HEAD — the footer prints 9 keys and has no `request_id`, while `MetaEvent` has no `latency`.
  Restated as an intersection contract with two documented exceptions.
- **D8 referenced a config field that does not exist.** There is no `api_cors_origins` in `Settings`,
  and adding one would have forced a `get_settings()` call into `create_app()`, breaking AC1's
  import-safety guarantee. CORS origins are now a `create_app` parameter (Q6/Q7).
- **Raw-text `data:` payloads were not newline-safe.** All five event types are now JSON-encoded,
  `token` as `{"text": "..."}` (Q8).

Confirmed unchanged by recon and needing no amendment: `build_context()` and `build_graph()`
signatures (S16), the `Citation` field set (`state.py:136`, identical to `CitationOut`), the
`run_query.py` intake keys, and the `reason=` / `confidence_reason` label trap G4 already called out.
`fastapi`, `uvicorn[standard]`, and `sse-starlette` are all already declared and installed — S18 adds
no dependency.
