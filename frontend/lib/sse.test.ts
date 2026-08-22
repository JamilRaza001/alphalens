import { expect, it } from 'vitest'

import { parseFrame, splitFrames } from './sse'

// The literal ping frame observed as the FIRST frame of run 1, ahead of `meta`,
// because the graph ran 26.7s against sse-starlette's fixed 15s cadence.
// Transcribed from the run 1 cat -A output preserved in the session record;
// the raw file was lost to tmpfs.
const RUN1_PING = ': ping - 2026-08-21 11:30:55.996070+00:00\r\n\r\n'

// 1 — frame split across two chunks
it('reassembles a frame split across two chunks', () => {
  const first = splitFrames('event: token\r\ndata: {"text":"Rev')
  expect(first.frames).toEqual([])

  const second = splitFrames(first.rest + 'enue"}\r\n\r\n')
  expect(second.frames).toHaveLength(1)
  expect(second.rest).toBe('')
  expect(parseFrame(second.frames[0])).toEqual({
    event: 'token',
    data: { text: 'Revenue' },
  })
})

// 2 — CRLF line endings
it('closes a frame on a CRLF terminator, not on LF alone', () => {
  const frame =
    'event: done\r\ndata: {"latency_s":26.7,"token_count":412,"breaker_open":false}\r\n\r\n'

  // Splitting on "\n\n" without normalising would never match here.
  expect(frame.includes('\n\n')).toBe(false)

  const { frames, rest } = splitFrames(frame)
  expect(frames).toHaveLength(1)
  expect(rest).toBe('')
  expect(parseFrame(frames[0])).toEqual({
    event: 'done',
    data: { latency_s: 26.7, token_count: 412, breaker_open: false },
  })
})

// 3 — comment/ping frame
it('returns null for a comment frame, which carries no application payload', () => {
  expect(parseFrame(': keep-alive')).toBeNull()
})

// 4 — empty data
it('returns null for a frame whose data field is empty', () => {
  expect(parseFrame('event: token\ndata:')).toBeNull()
  expect(parseFrame('event: token\ndata: ')).toBeNull()
})

// 5 — token whose text contains a newline
it('parses a token whose text contains a newline', () => {
  // One data: line, the newline JSON-escaped — the only form this wire produces,
  // since json.dumps never emits a raw newline inside a string.
  // Routed through splitFrames: parseFrame consumes LF-normalised frames.
  const { frames } = splitFrames(
    'event: token\r\ndata: {"text":"line one\\nline two"}\r\n\r\n',
  )
  expect(frames).toHaveLength(1)
  expect(parseFrame(frames[0])).toEqual({
    event: 'token',
    data: { text: 'line one\nline two' },
  })
})

// 6 — Amendment 1 #15: the real ping comment observed in run 1
it('returns null for the ping comment observed in run 1', () => {
  const { frames, rest } = splitFrames(RUN1_PING)
  expect(frames).toHaveLength(1)
  expect(rest).toBe('')
  expect(parseFrame(frames[0])).toBeNull()
})

// 7 — Amendment 1 #16: chunk boundary inside a CRLF
it('holds back a lone CR when the chunk boundary falls inside a CRLF', () => {
  const whole = 'event: meta\r\ndata: {"request_id":"abc","plan_tickers":[]}\r\n\r\n'

  // Cut between the final \r and its \n — the byte pair that terminates the frame.
  const head = whole.slice(0, -1)
  const tail = whole.slice(-1)
  expect(head.endsWith('\r')).toBe(true)
  expect(tail).toBe('\n')

  // Normalising that trailing CR now would close the frame one byte early.
  const first = splitFrames(head)
  expect(first.frames).toEqual([])
  expect(first.rest.endsWith('\r')).toBe(true)

  const second = splitFrames(first.rest + tail)
  expect(second.frames).toHaveLength(1)
  expect(second.rest).toBe('')
  expect(parseFrame(second.frames[0])).toEqual({
    event: 'meta',
    data: { request_id: 'abc', plan_tickers: [] },
  })
})

// 8 — run 1's actual frame order: the ping arrived FIRST, ahead of `meta`
it('parses a leading ping to null and the following meta to an event', () => {
  const meta = 'event: meta\r\ndata: {"request_id":"abc","plan_tickers":["AAPL"]}\r\n\r\n'

  const { frames, rest } = splitFrames(RUN1_PING + meta)
  expect(frames).toHaveLength(2)
  expect(rest).toBe('')

  // The first frame is therefore not guaranteed to be `meta`.
  expect(parseFrame(frames[0])).toBeNull()
  expect(parseFrame(frames[1])).toEqual({
    event: 'meta',
    data: { request_id: 'abc', plan_tickers: ['AAPL'] },
  })
})
