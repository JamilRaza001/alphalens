// ── lib/sse.ts ── SSE transport for the S18 `POST /query` endpoint ──
// Written against the run-1 byte capture: line separator 0d0a, frame
// terminator 0d0a0d0a, and a real ping comment as the FIRST frame of the
// stream. `EventSource` is not usable here — it is GET-only.

import type {
  CitationOut,
  DoneEvent,
  ErrorEvent,
  MetaEvent,
  StreamEvent,
} from './types'

/** Non-2xx BEFORE the stream opens (the 422 path, S18 AC13). Distinct from an
 *  in-band `error` event, which arrives on an already-200 response. */
export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
  ) {
    super(`HTTP ${status}`)
    this.name = 'HttpError'
  }
}

/** Split a decoded buffer into complete SSE frames, returning the remainder.
 *  Normalises CRLF/CR to LF before splitting (G1). Pure — no I/O, unit-testable. */
export function splitFrames(buffer: string): { frames: string[]; rest: string } {
  // A trailing lone CR may be the first half of a CRLF that straddles the chunk
  // boundary. Normalising it now would fabricate a line ending and, at a frame
  // terminator, close the frame one byte early. Hold it back instead.
  let pending = ''
  if (buffer.endsWith('\r')) {
    pending = '\r'
    buffer = buffer.slice(0, -1)
  }

  const parts = buffer.replace(/\r\n?/g, '\n').split('\n\n')
  const rest = parts.pop() ?? ''
  return { frames: parts.filter((frame) => frame.length > 0), rest: rest + pending }
}

/** Parse one raw frame into a StreamEvent, or null for frames that carry no
 *  application payload (comment/ping lines, unknown event names) (G2). */
export function parseFrame(frame: string): StreamEvent | null {
  let name: string | null = null
  const dataLines: string[] = []

  for (const line of frame.split('\n')) {
    // `: ping - <ts>` — a comment, the only non-spec frame on this wire
    // (sse.py:445-447 -> event.py:34-36). No `event:`, no `data:`.
    if (line.startsWith(':')) continue

    const colon = line.indexOf(':')
    if (colon === -1) continue

    const field = line.slice(0, colon)
    const value = line.slice(colon + 1).replace(/^ /, '')

    if (field === 'event') name = value
    else if (field === 'data') dataLines.push(value)
    // `id:` / `retry:` are reachable in event.py but app.py:99 returns only
    // {event, data}, so they never appear. Ignored rather than modelled.
  }

  if (name === null) return null

  // event.py:49-51 emits one `data:` line per line of a multi-line payload;
  // rejoin with LF. Run 1 showed a single `data:` line per frame, so this
  // path is not wire-confirmed — it follows from the server source.
  const raw = dataLines.join('\n')
  if (raw.length === 0) return null

  const payload: unknown = JSON.parse(raw)

  // Branch on the event NAME, never on payload structure: `citations` is a
  // bare array while the other four are objects.
  switch (name) {
    case 'meta':
      return { event: 'meta', data: payload as MetaEvent }
    case 'token':
      return { event: 'token', data: payload as { text: string } }
    case 'citations':
      return { event: 'citations', data: payload as CitationOut[] }
    case 'done':
      return { event: 'done', data: payload as DoneEvent }
    case 'error':
      return { event: 'error', data: payload as ErrorEvent }
    default:
      return null
  }
}

/** POST the question and yield events as they arrive.
 *  Throws HttpError before the first yield if the response is not ok.
 *  Propagates AbortError on signal — the caller distinguishes it (D7). */
export async function* streamQuery(
  question: string,
  opts: { baseUrl: string; signal: AbortSignal; userId?: string | null },
): AsyncGenerator<StreamEvent, void, unknown> {
  const payload: { question: string; user_id?: string } = { question }
  if (opts.userId) payload.user_id = opts.userId

  const response = await fetch(`${opts.baseUrl}/query`, {
    method: 'POST',
    // Exactly one header. A second one widens the CORS preflight beyond
    // app.py:243's allow_headers=["Content-Type"] and fails opaquely (G3).
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: opts.signal,
  })

  if (!response.ok) {
    // The 422 path is not a stream, and HttpError's signature needs the body.
    // This is the ONLY place a body is read whole (G7).
    throw new HttpError(response.status, await response.text())
  }
  if (response.body === null) {
    throw new HttpError(response.status, 'response carried no body')
  }

  const reader = response.body.getReader()
  // One decoder for the whole stream, in streaming mode: it carries a partial
  // multi-byte sequence across a chunk boundary (G4); a decoder rebuilt per
  // chunk cannot. Not observed on the wire in run 1 — every non-ASCII
  // character arrived \uXXXX-escaped — so this is unit-tested, not wire-proven.
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const split = splitFrames(buffer)
      buffer = split.rest

      for (const frame of split.frames) {
        const parsed = parseFrame(frame)
        // null = the ping comment, or an event we do not model. Run 1 put a
        // ping FIRST, ahead of `meta`, because the graph ran 26.7s against a
        // 15s fixed ping cadence. The first non-null frame is therefore not
        // guaranteed to be `meta` — this guard is structural, not an edge case.
        if (parsed === null) continue

        yield parsed

        // `error` is terminal: app.py returns from the generator, so no `done`
        // will follow. Stop rather than block on a frame that never arrives.
        if (parsed.event === 'error') return
      }
    }
    // No end-of-stream flush of `buffer`. Run 1's tail confirmed the `done`
    // frame carries its own 0d0a0d0a terminator, so a complete stream always
    // leaves `rest` empty; anything left over is a truncated frame, not an event.
  } finally {
    reader.releaseLock()
  }
}
