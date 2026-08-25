# S19 — Local Frontend (Next.js + SSE)

**Format:** L21 lightweight (Goal + Signatures + AC + Gotchas).

**Scope:** `frontend/` only. Zero files under `src/alphalens/` are modified —
S18's wire contract is consumed as-is, never amended. If a change here seems to
require editing `app.py` or `schemas.py`, that is a signal to stop and re-scope.

---

## Goal

A local browser client that submits a question to the S18 SSE endpoint and
renders the response as it arrives: honesty-rail metadata first, then the answer
token-by-token, then the citation list. This closes the v1 shipping track —
after S19 the pipeline is reachable by something other than `curl`.

The client is a **consumer**. Every retrieval, honesty and confidence decision
already happened server-side; this spec adds no interpretation of them, only
display.

---

## Recon findings this spec rests on (HEAD `145a5f5`, live-verified)

Wire contract read from `src/alphalens/api/schemas.py` and `app.py` at HEAD —
not from the S18 spec doc, which carries three amendments and is doc-sourced.

| Event | Payload shape | Cardinality |
|---|---|---|
| `meta` | object (`MetaEvent`) | exactly once, before any token |
| `token` | `{"text": "..."}` | zero or more |
| `citations` | **bare JSON array** of `CitationOut` | exactly once, may be `[]` |
| `done` | object (`DoneEvent`) | success only |
| `error` | object (`ErrorEvent`) | terminal — no `done` follows |

- Every `data:` is JSON-encoded (S18 D6/Q8) — one parse path, but **not one
  shape**: `citations` is an array, the other four are objects. Branch on the
  `event:` name, never on payload structure.
- `POST /query`, body `{question, user_id?}`; `question` is
  `min_length=1, max_length=2000`. A violation is a **FastAPI 422 before the
  stream opens** (S18 AC13) — a status code, not an `error` event. Two distinct
  failure paths.
- `request_id` is minted server-side (`uuid4()`, S18 Q12) and reaches the client
  only inside `meta`. The client does not send one and must not invent one.
- CORS at `app.py`: `allow_origins` defaults to
  `["http://localhost:3000", "http://127.0.0.1:3000"]`,
  `allow_headers=["Content-Type"]`, `allow_credentials=False`.
- API binds `127.0.0.1:8000` (`API_HOST` / `API_PORT`, `scripts/run_api.py`).
- Toolchain live: node `v20.20.2`, npm `10.8.2`. **pnpm absent.**
- `.gitignore` already carries `frontend/node_modules/`, `frontend/.next/`,
  `frontend/.vercel/` — the directory name and location are pre-committed.
- **`meta` is emitted AFTER `ainvoke()` returns** (`app.py`, the `yield _sse("meta", ...)`
  sits below the `await`). The first byte of application data therefore arrives
  ~10s in. This is S18's known AC5 limitation, deferred to v2 — S19 must
  accommodate it, not fix it.

---

## Decisions

- **D1 — npm, not pnpm.** npm 10.8.2 is installed; pnpm is not. A second package
  manager buys nothing for a single developer with no workspace, and adds a
  global install to the setup path.

- **D2 — `frontend/` at repo root, same repo.** Not a decision so much as
  ratification: `.gitignore` already names the path.

- **D3 — App Router, one client component tree.** The entire UI is `"use client"`.
  Server Components, server actions and route handlers have no role: the API is a
  separate process and the stream is consumed in the browser. **No Next.js version
  is asserted in this spec** — it is read live at scaffold time (`npm view next version`).

- **D4 — Manual SSE parser over `fetch` + `ReadableStream`.** `EventSource` is
  GET-only and cannot carry the POST body (S18 D5/G1). Three load-bearing details:
  `TextDecoder` is constructed once and decoded with `{stream: true}` (a multi-byte
  UTF-8 character can straddle two network chunks — and this corpus emits CJK
  citation brackets); the buffer is accumulated across chunks and a frame closed
  only on a blank line; line endings are normalised before splitting (see G1).

- **D5 — Honest pending state, no fake progress.** During the ~10s pre-`meta`
  window the client knows nothing about pipeline position, so it renders a single
  indeterminate "Running query…" state. A staged progress indicator
  ("Planning… Retrieving…") would be **fabricated**, and fabricating certainty is
  the exact failure class the honesty rail exists to prevent. Per-node progress
  events require breaking S16 D2 and are v2.

- **D6 — The honesty rail renders above the answer, never collapsed.**
  `confidence`, `confidence_reason`, `unavailable_tickers`, `unavailable_years`,
  `unavailable_companies`, `coverage_gaps` and `capacity_drops` are displayed
  whenever non-empty (and `confidence` always). Hiding them behind a disclosure
  toggle would let the UI silently undo the rail the agent spent four specs
  building. `coverage_gaps` and `capacity_drops` are labelled **distinctly** —
  C1 split them precisely because "no evidence" and "trimmed by budget" have
  different meanings.

- **D7 — `AbortController` with a visible Stop control.** S18's AC10
  (clean mid-stream disconnect) is verified server-side but has no client
  exercising it. An `AbortError` is a user action, not a failure: it must not
  render as an error.

- **D8 — No state library, no component kit.** `useState` + `useRef` is
  sufficient for one request at a time. Tailwind as scaffolded by
  `create-next-app`; no shadcn, no zustand, no react-query.

- **D9 (OQ1, LOCKED) — `react-markdown` + `remark-gfm`.** The model emits
  markdown and the answer is the product. `remark-gfm` is required — GFM tables
  are not in the base markdown spec, so `react-markdown` alone renders them as
  literal pipes. **`rehype-raw` is banned**: `react-markdown` does not render raw
  HTML by default, which is why the XSS surface is currently zero; adding that
  plugin opens it. Accepted cost: the full answer string is re-parsed on every
  token, and mid-stream the buffer routinely holds unterminated syntax (an open
  `**`, a half-written table row), which flickers. Visual only, not a crash.
  **Do not pre-optimise** — measure the flicker on a real run first.

- **D10 (OQ2, LOCKED) — citation markers are not made interactive.** The
  in-answer `【7】` markers stay inert text. Their format is a known-unstable v2
  item (CJK bracket instability), and writing a parser against an unstable format
  guarantees a broken parser. The `citations` event already carries clean
  structured data; it renders as a separate list below the answer.

---

## Out of scope

- Chat history, multi-turn, persistence. One question, one answer. (The `queries`
  table exists from S2, but no spec writes to it and S19 does not become the first.)
- Deployment, Vercel, OIDC → **v2** (spec 17b).
- Per-node progress events → v2 (requires breaking S16 D2).
- Clickable citation markers → v2, with whichever spec owns marker parsing.
- Retry / resume on a dropped stream. A truncated answer is reported, not silently
  re-fetched.

---

## File layout

```
frontend/
  app/
    layout.tsx           # shell, font, globals import
    page.tsx             # "use client" — the only stateful component
    globals.css
  components/
    MetaPanel.tsx        # honesty rail (D6)
    AnswerView.tsx       # react-markdown + remark-gfm (D9)
    CitationList.tsx
  lib/
    types.ts             # wire types, mirroring schemas.py
    sse.ts               # parser + streamQuery generator
  .env.local.example     # NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
```

---

## Signatures

```ts
// ── lib/types.ts ── wire types only · no logic ──
// Mirrors src/alphalens/api/schemas.py at HEAD 145a5f5. If a field changes
// server-side, it changes HERE — never patched at a call site.

export interface GapCell { ticker: string; year: number }

export interface MetaEvent {
  request_id: string
  confidence: string            // "low" | "high" — typed `str` server-side
  confidence_reason: string     // "coverage" | "llm" | "none"
  coverage_gaps: GapCell[]
  capacity_drops: GapCell[]
  unavailable_tickers: string[]
  unavailable_years: number[]
  unavailable_companies: string[]
  plan_tickers: string[]        // [] when query_plan unset (S18 AC9), never null
}

export interface CitationOut {
  chunk_id: string
  ticker: string
  filing_type: string
  period_year: number
  section: string | null
}

export interface DoneEvent { latency_s: number; token_count: number; breaker_open: boolean }
export interface ErrorEvent { message: string; phase: string; breaker_open: boolean }

// Discriminated on `event` — the payload SHAPE differs (citations is an array).
export type StreamEvent =
  | { event: "meta";      data: MetaEvent }
  | { event: "token";     data: { text: string } }
  | { event: "citations"; data: CitationOut[] }
  | { event: "done";      data: DoneEvent }
  | { event: "error";     data: ErrorEvent }
```

```ts
// ── lib/sse.ts ──

/** Non-2xx BEFORE the stream opens (the 422 path, S18 AC13). Distinct from an
 *  in-band `error` event, which arrives on an already-200 response. */
export class HttpError extends Error {
  constructor(readonly status: number, readonly body: string) { super(...) }
}

/** Split a decoded buffer into complete SSE frames, returning the remainder.
 *  Normalises CRLF/CR to LF before splitting (G1). Pure — no I/O, unit-testable. */
export function splitFrames(buffer: string): { frames: string[]; rest: string }

/** Parse one raw frame into a StreamEvent, or null for frames that carry no
 *  application payload (comment/ping lines, unknown event names) (G2). */
export function parseFrame(frame: string): StreamEvent | null

/** POST the question and yield events as they arrive.
 *  Throws HttpError before the first yield if the response is not ok.
 *  Propagates AbortError on signal — the caller distinguishes it (D7). */
export async function* streamQuery(
  question: string,
  opts: { baseUrl: string; signal: AbortSignal; userId?: string | null },
): AsyncGenerator<StreamEvent, void, unknown>
```

---

## Acceptance Criteria

1. `frontend/` scaffolded with npm (D1); `npm run build` succeeds and
   `npx tsc --noEmit` is clean.
2. `POST /query` is issued with `Content-Type: application/json` and **no other
   custom header** (G3). The body carries `question` and nothing the server does
   not accept.
3. Tokens render **progressively** — visible growth during the stream, not one
   lump at the end. Verified by eye against a real run, cross-checked with the
   `curl -N` behaviour S18 already established.
4. The five event types are each handled: `meta` populates the rail, `token`
   appends, `citations` replaces the list, `done` ends the run, `error` ends the
   run and displays `message` + `phase`. An `error` is terminal — no `done` is
   expected or awaited after it.
5. `citations` parses as an **array**, including the empty case (`[]` renders as
   an explicit "no citations" state, not as a missing section).
6. A 422 (empty or >2000-char question) is surfaced as a validation message and is
   **visibly distinct** from an in-band `error` event.
7. During the pre-`meta` window an indeterminate pending state is shown, with no
   stage names or percentage (D5).
8. `MetaPanel` renders every rail field per D6; `coverage_gaps` and
   `capacity_drops` carry distinct labels.
9. Stop aborts the request; the partial answer stays on screen, is **labelled as
   incomplete**, and no error is rendered (D7).
10. The answer renders through `react-markdown` with `remark-gfm`. `rehype-raw` is
    absent from the dependency tree (D9).
11. `splitFrames` and `parseFrame` have unit tests covering: a frame split across
    two chunks, CRLF line endings, a comment/ping frame, an empty `data`, and a
    `token` whose text contains a newline.
12. `frontend/.env.local` is git-ignored — verified with
    `git check-ignore -v frontend/.env.local`. If it is not matched, an explicit
    line is added to `.gitignore` in the same commit.
13. No file under `src/alphalens/` is modified. Python gates
    (ruff, ruff format, mypy --strict, pytest 294/7/1) are untouched and still green.
14. **Live gate, one run, zero new Groq calls beyond the single query:** with
    `scripts/run_api.py` running, submit a question from the browser and observe
    the full happy path end-to-end — pending → rail → progressive tokens →
    citations → done. Screenshot or log the `done` payload.

---

## Gotchas (live-verify checkpoints — S28 discipline)

- **G1 — Confirm the SSE line separator before writing the split.** The SSE spec
  permits `\n`, `\r\n` and `\r`, and a server library may use any of them. A parser
  that splits on `"\n\n"` against a `\r\n` stream never closes a frame and the UI
  silently shows nothing. **Verify first**: `curl -N -X POST ... | cat -A` (or pipe
  through `xxd`) and read the actual bytes — do not infer from library docs.
  Regardless of the answer, `splitFrames` normalises `\r\n` and `\r` to `\n` before
  splitting, so it is correct either way.

- **G2 — Expect frames that are not events.** `sse-starlette` sends periodic
  keep-alive comment lines (`: ping ...`) on an idle connection by default, and the
  ~10s dead air is exactly when an idle connection exists. A comment line has no
  `event:` and no `data:`; feeding it to `JSON.parse` throws. `parseFrame` returns
  `null` for any frame without a `data:` field or with an unrecognised event name.
  **Verify**: leave a run connected through the dead air and inspect the raw bytes
  for `:`-prefixed lines. If none appear, the guard costs nothing.

- **G3 — CORS allows exactly one non-safelisted header.** `allow_headers=["Content-Type"]`.
  Adding any custom header (`X-Request-Id`, `Authorization`, a trace header) makes
  the preflight fail, and the browser reports it as an opaque CORS error rather than
  "header not allowed". If a header is ever genuinely needed, the server allowlist
  changes first.

- **G4 — `TextDecoder` must be reused, not re-constructed per chunk.** A fresh
  decoder per chunk cannot carry a partial multi-byte sequence and will emit
  replacement characters mid-word. One decoder, `decode(chunk, { stream: true })`.

- **G5 — Do not start the request from `useEffect`.** React StrictMode
  double-invokes effects in dev, which would fire two graph runs and burn two Groq
  calls per submit. The request starts from the submit handler.

- **G6 — An aborted read throws.** Cancelling via `AbortController` rejects the
  in-flight read with `AbortError`. It must be caught and classified as a user
  action (D7), not funnelled into the error branch, or Stop will look like a crash.

- **G7 — Reading with `.text()` or `.json()` defeats streaming entirely.** Both
  buffer the whole body. The response is consumed through
  `response.body.getReader()` and nothing else. If tokens arrive in one lump, the
  bug is on this line before it is anywhere else.

- **G8 — The origin must match the server allowlist exactly.** `localhost:3000`
  and `127.0.0.1:3000` are both allowed but are different origins; if the API base
  URL and the page origin are mixed inconsistently the request is still cross-origin
  and still needs the preflight to pass. Keep both on the same form.

- **G9 — Do not tune the markdown re-parse before observing it.** D9 accepts
  per-token re-parsing. Throttling or memoising it is an optimisation, and this
  project has a standing rule about measuring before fixing. Ship it, watch one
  real run, then decide.

---

## Amendment 1 (19 Aug 2026) — G2 corrected against installed source

**G2 as written above is wrong on one load-bearing point and is superseded by
this section. The original text is left in place unedited: it records what was
believed at authoring time, and the correction is what recon found.**

G2 states that pings arrive "on an idle connection" and that "the ~10s dead air
is exactly when an idle connection exists". Both clauses are false.

Recon (installed `sse-starlette` 3.4.4, source read — not docs):

| Finding | Location |
|---|---|
| `DEFAULT_PING_INTERVAL = 15` | `sse.py:259` |
| `ping is None` → default applies; `app.py:265` passes no `ping` | `sse.py:316` |
| `_ping` started unconditionally as a task-group member | `sse.py:488` |
| `while self.active: await anyio.sleep(self._ping_interval)` | `sse.py:440-441` |
| Emitted as `ServerSentEvent(comment=f"ping - {…}")` | `sse.py:445-447` |
| A comment renders as `f": {chunk}{sep}"` — no `event:`, no `data:` | `event.py:34-36` |
| Sent under `_send_lock` as its own ASGI body message | `sse.py:452-460` |

**The correction:** the ping loop is a fixed cadence measured from response
start and is **never reset by outgoing data**. It is not an idle-connection
mechanism. Consequences:

- The graph takes ~10s, so the first ping lands at t≈15s — **inside the token
  flow, interleaved between `token` frames**, not only in the dead air.
- A run whose graph exceeds 15s puts a ping in the dead air as well.
- Because it is sent under `_send_lock` as its own body message, a ping always
  arrives as a whole frame between frames and can never splice inside one.

**Status change:** G2's guard moves from *cheap insurance* to **hard
requirement**. Any run over ~15s wall-clock emits at least one frame with no
`data:` line, wherever the tokens happen to be. A parser that does not return
`null` for it will throw on `JSON.parse` mid-answer.

**G1 confirmed, also promoted.** `DEFAULT_SEPARATOR = "\r\n"` (`sse.py:260`),
`self.sep = sep or self.DEFAULT_SEPARATOR` (`sse.py:285`), and `app.py:265`
passes no `sep=`. Frames on this wire terminate `\r\n\r\n`. Splitting on
`"\n\n"` finds no match and never closes a frame — the UI would sit silent
forever. G1's normalisation is required, not defensive.

**Q4 — no other frame shapes reach this wire.** `ServerSentEvent.encode` can
emit `id:` (`event.py:38-40`) and `retry:` (`event.py:53-56`), but `app.py:99`
returns `{"event": …, "data": …}` only, so `ensure_bytes` (`event.py:93-95`)
constructs `ServerSentEvent(**data)` with `id` / `retry` / `comment` left
`None`. The ping comment is the sole non-spec frame.

**Carried, not in the original spec:** `event.py:49-51` splits multi-line data
across multiple `data:` lines. Payloads here are compact JSON so one `data:`
line per frame is expected in practice, but `parseFrame` joins multiple `data:`
lines with `\n` regardless — two lines of code for spec correctness.
`sse.py:310-313` already sets `Cache-Control: no-store` and
`X-Accel-Buffering: no`, so no client-side buffering workaround is needed.

**AC11 extended.** Two cases are added to the required unit tests, both earned
by this recon rather than reasoned from the spec:

15. A real ping comment frame (`: ping - <timestamp>`) → `parseFrame` returns
    `null`.
16. A CRLF frame split across two chunks **at the `\r` / `\n` boundary** — the
    specific input on which a naive normaliser breaks.

**Live-verification caveat.** The graph takes ~10s and the ping fires at 15s, so
a short run may complete before any ping is emitted. Absence of a ping in a
capture is **not** evidence that the guard was exercised. Unless a ping frame is
actually visible in the raw bytes, the outcome is recorded as *"G2 not observed
on the wire; covered by unit test only"* — never as *"G2 wire-confirmed"*.

---

## Amendment 2 (25 Aug 2026) — three claims corrected after the live runs

**Same convention as Amendment 1: everything above is left unedited.** It records
what was believed at authoring time; this section records what measurement found.

### 2.1 — The multi-`data:` join is protocol identity, not spec correctness

Amendment 1's "Carried, not in the original spec" paragraph calls `parseFrame`'s
joining of multiple `data:` lines with `\n` "two lines of code for spec
correctness". **The claim is wrong. The code is right.**

Every `data:` on this wire is `json.dumps` output (S18 D6/Q8), and `json.dumps`
escapes newlines as `\n` *inside* the string. A payload that would force
`event.py:49-51` to split across multiple `data:` lines therefore never forms.
And had one formed, the rejoined string would carry a literal newline inside a
JSON scalar — which `JSON.parse` rejects — so the join could not have rescued it.

The join is retained because it is what SSE *is*: a multi-line `data:` field
means one payload separated by newlines, and a parser that keeps only the last
line is not an SSE parser. It buys protocol conformance, not correctness against
this server. **Unreachable in v1 either way.**

Same class, recorded not fixed: `parseFrame` returns `null` silently on a raw
CRLF frame. That is contract-guarded (`splitFrames` normalises first), not
code-guarded.

### 2.2 — AC3's instrument is "by eye", and that is the root cause of a withdrawn PASS

AC3 above requires tokens to render progressively, "**Verified by eye** against a
real run". **That instrument is insufficient, and it produced a false PASS that
survived two sessions before being withdrawn.**

What happened: the result was accepted from a leading question ("did tokens
arrive progressively or in a lump?") put to the operator, while the real
instrument — the DevTools EventStream timestamp column — sat open and unread.

**The instrument for AC3 is the arrival timestamp of the first and last `token`
frame. Not the eye, and not a question put to the operator.**

Measured 25 Aug 2026 at `7b8887e`: `meta` and the deterministic caveat token
(`_prepend_caveat`, K1) share t=0; the first synthesis token lands t+1.3 s — that
gap is Groq TTFT, not client latency, and reproduced at t+2.1 s on an earlier
run; the remaining 312 `token` events plus `citations` plus `done` all fall
inside one 0.1 s bucket. **Token spread = 0.7 s for 312 events.**

At that spread, progressive and batched rendering are indistinguishable by any
instrument available to this project. `page.tsx` contains no batching, and S18's
own G3 (server-side progressive delivery) passed on a different run — so the
underlying behaviour is not in doubt; only the client-side twin is unmeasurable.
**AC3's status on this corpus is NOT FALSIFIED / UNVERIFIABLE, not PASS.** It
becomes measurable when a synthesis runs long enough to spread its tokens — i.e.
after the v2 iXBRL re-ingest gives the model actual figures to write about.

**Knock-on: AC9's retention clause inherits the same 0.7 s window** and is
likewise unexercisable here. Two runs were spent missing it; the window is under
human reaction time once row-recognition is included. That is a corpus property,
not a discipline failure — **no further run should be spent on it before v2.**

### 2.3 — AC12's instrument reads the wrong exit code

AC12 verifies that the env example is not swallowed by `.gitignore`, using
`git check-ignore -v`. **The exit-code semantics are inverted from what the AC
assumes.**

Measured on `.env.local.example`: `-v` exits **0**, `-q` exits **1**. `-v` exit 0
means "matched some pattern", and a match includes a *negation* pattern. Only
`-q` exit 0 means "this path is ignored".

So `git check-ignore -v` exiting 0 is entirely compatible with the file being
tracked — which is exactly the situation here. **The AC passed; its instrument
did not prove it.** The outcome was confirmed separately with `-q`.

`-q` is the correct instrument for "is this ignored".

---

**Zero code changes follow from this amendment.** All three corrections are to
claims and instruments, never to `frontend/` source. `7b8887e` remains S19 HEAD.
