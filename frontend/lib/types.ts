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
