'use client'

import { useRef, useState, type ChangeEvent, type FormEvent } from 'react'

import AnswerView from '../components/AnswerView'
import CitationList from '../components/CitationList'
import MetaPanel from '../components/MetaPanel'
import { HttpError, streamQuery } from '../lib/sse'
import type { CitationOut, DoneEvent, ErrorEvent, MetaEvent } from '../lib/types'

// Inlined at build time. `.env.local` is git-ignored and may not exist, so this
// fallback is what lets a fresh clone run without a setup step first. Loopback
// IP rather than the hostname form: app.py's CORS list allows both origins, but
// mixing the two forms between the page and the API is the G8 trap.
const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://127.0.0.1:8000'

export default function Home() {
  const [question, setQuestion] = useState('')
  const [running, setRunning] = useState(false)
  const [meta, setMeta] = useState<MetaEvent | null>(null)
  const [answer, setAnswer] = useState('')
  // `null` and `[]` are different facts: null means citations never arrived, []
  // means the server sent an empty array. Only [] reaches CitationList and its
  // explicit empty state (AC5). Collapsing them would report one as the other.
  const [citations, setCitations] = useState<CitationOut[] | null>(null)
  const [finished, setFinished] = useState<DoneEvent | null>(null)
  const [incomplete, setIncomplete] = useState(false)
  const [transportError, setTransportError] = useState<string | null>(null)
  const [streamError, setStreamError] = useState<ErrorEvent | null>(null)

  const abortRef = useRef<AbortController | null>(null)

  // Submitted, but `meta` has not landed. The client genuinely does not know
  // where in the pipeline the graph is, so the pending state claims nothing
  // more than that (D5).
  const pending = running && meta === null

  /** The request starts HERE, from the user's submit — never from a mount
   *  lifecycle hook. StrictMode double-invokes those in dev, which would fire
   *  two graph runs per submit and spend two Groq budgets (G5). */
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (running) return

    const controller = new AbortController()
    abortRef.current = controller

    setRunning(true)
    setMeta(null)
    setAnswer('')
    setCitations(null)
    setFinished(null)
    setIncomplete(false)
    setTransportError(null)
    setStreamError(null)

    try {
      for await (const streamed of streamQuery(question, {
        baseUrl: API_BASE_URL,
        signal: controller.signal,
      })) {
        switch (streamed.event) {
          case 'meta':
            setMeta(streamed.data)
            break
          case 'token':
            setAnswer((previous) => previous + streamed.data.text)
            break
          case 'citations':
            // Replaces, never appends — it arrives once, already complete.
            setCitations(streamed.data)
            break
          case 'done':
            setFinished(streamed.data)
            break
          case 'error':
            // Terminal. streamQuery returns after yielding this (sse.ts:151), so
            // the loop ends here. Nothing waits for a `done` that S18 will not
            // send.
            setStreamError(streamed.data)
            break
        }
      }
    } catch (thrown) {
      const aborted =
        controller.signal.aborted ||
        (thrown instanceof DOMException && thrown.name === 'AbortError')

      if (aborted) {
        // A stop is a user action, not a failure. The partial answer stays on
        // screen, labelled — and no error is rendered (D7).
        setIncomplete(true)
      } else if (thrown instanceof HttpError) {
        setTransportError(
          `The server rejected the request with HTTP ${thrown.status}. ${thrown.body}`,
        )
      } else {
        // The API being down throws a TypeError out of fetch, not an HttpError.
        // Same slot: it is a connection fault, not the agent reporting a fault
        // of its own. Left unhandled it would strand the pending state forever,
        // which is the dishonest-progress failure D5 exists to prevent.
        setTransportError(thrown instanceof Error ? thrown.message : String(thrown))
      }
    } finally {
      setRunning(false)
      abortRef.current = null
    }
  }

  function handleStop() {
    abortRef.current?.abort()
  }

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">AlphaLens</h1>
        <p className="text-sm text-black/60 dark:text-white/60">
          Ask a question about the indexed SEC 10-K and 10-Q filings.
        </p>
      </header>

      {/* Slot 1 — the request never opened a stream. Sits above the form, beside
          the input that caused it, and is styled apart from the in-band error
          below so the two can never be mistaken for each other (AC6). */}
      {transportError !== null && (
        <p
          role="alert"
          className="rounded-lg border border-amber-500/60 bg-amber-500/10 px-3 py-2 text-sm"
        >
          <span className="font-semibold">Request rejected before the stream opened. </span>
          {transportError}
        </p>
      )}

      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <textarea
          value={question}
          onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setQuestion(event.target.value)}
          disabled={running}
          rows={3}
          placeholder="How did Apple and Microsoft revenue compare in 2023 vs 2024?"
          className="w-full resize-y rounded-lg border border-black/15 bg-transparent p-3 text-sm disabled:opacity-60 dark:border-white/20"
        />

        {/* Submit is NOT disabled on an empty or over-long question. The 422 is
            the server's to raise, and pre-validating it here would hide the one
            path AC6 checks. */}
        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={running}
            className="rounded-lg border border-black/15 px-4 py-1.5 text-sm font-medium disabled:opacity-60 dark:border-white/20"
          >
            Ask
          </button>

          {running && (
            <button
              type="button"
              onClick={handleStop}
              className="rounded-lg border border-black/15 px-4 py-1.5 text-sm font-medium dark:border-white/20"
            >
              Stop
            </button>
          )}

          {/* Slot 2 — indeterminate by design. No stage name, no fraction of the
              way through, no bar: the client knows none of those (D5). */}
          {pending && (
            <span aria-live="polite" className="text-sm text-black/60 dark:text-white/60">
              Running query…
            </span>
          )}
        </div>
      </form>

      {meta !== null && <MetaPanel meta={meta} />}

      {/* Slot 3 — the stream opened and the agent reported a fault. In the
          results column rather than above the form, and red rather than amber
          (AC6). */}
      {streamError !== null && (
        <p
          role="alert"
          className="rounded-lg border border-red-500/60 bg-red-500/10 px-3 py-2 text-sm"
        >
          <span className="font-semibold">The run failed at the {streamError.phase} step. </span>
          {streamError.message}
          {streamError.breaker_open && ' The circuit breaker is open.'}
        </p>
      )}

      {(answer.length > 0 || incomplete) && <AnswerView text={answer} incomplete={incomplete} />}

      {citations !== null && <CitationList citations={citations} />}

      {/* The `done` payload, on screen rather than in devtools — AC14 asks for it
          to be logged, and this is the cheapest place to read it from. */}
      {finished !== null && (
        <p className="text-xs text-black/50 dark:text-white/50">
          {finished.latency_s}s · {finished.token_count} tokens · breaker{' '}
          {finished.breaker_open ? 'open' : 'closed'}
        </p>
      )}
    </main>
  )
}
