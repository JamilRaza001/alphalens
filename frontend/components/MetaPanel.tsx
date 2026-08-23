import type { ReactNode } from 'react'

import type { GapCell, MetaEvent } from '../lib/types'

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 border-t border-black/10 pt-2 first:border-t-0 first:pt-0 dark:border-white/15">
      <span className="text-xs font-semibold uppercase tracking-wide text-black/60 dark:text-white/60">
        {label}
      </span>
      <div className="text-sm">{children}</div>
    </div>
  )
}

function Cells({ cells }: { cells: GapCell[] }) {
  return (
    <ul className="flex flex-wrap gap-1.5">
      {cells.map((cell) => (
        <li
          key={`${cell.ticker}-${cell.year}`}
          className="rounded bg-black/5 px-2 py-0.5 font-mono text-xs dark:bg-white/10"
        >
          {cell.ticker} {cell.year}
        </li>
      ))}
    </ul>
  )
}

/** The honesty rail (D6). Display only — every judgement behind these values was
 *  already made server-side. `request_id` and `plan_tickers` exist on MetaEvent
 *  but D6 does not list them, so they are deliberately not rendered. */
export default function MetaPanel({ meta }: { meta: MetaEvent }) {
  return (
    <section
      aria-label="Answer metadata"
      className="flex flex-col gap-2 rounded-lg border border-black/10 p-4 dark:border-white/15"
    >
      <Row label="Confidence">{meta.confidence}</Row>

      {meta.confidence_reason.length > 0 && (
        <Row label="Confidence reason">{meta.confidence_reason}</Row>
      )}

      {meta.unavailable_tickers.length > 0 && (
        <Row label="Unavailable tickers">{meta.unavailable_tickers.join(', ')}</Row>
      )}

      {meta.unavailable_years.length > 0 && (
        <Row label="Unavailable years">{meta.unavailable_years.join(', ')}</Row>
      )}

      {meta.unavailable_companies.length > 0 && (
        <Row label="Unavailable companies">{meta.unavailable_companies.join(', ')}</Row>
      )}

      {/* AC8: these two carry deliberately different labels. A coverage gap means
          nothing was found; a capacity drop means something was found and then
          trimmed to fit the budget. Collapsing them would misreport the run. */}
      {meta.coverage_gaps.length > 0 && (
        <Row label="Coverage gaps — no evidence found">
          <Cells cells={meta.coverage_gaps} />
        </Row>
      )}

      {meta.capacity_drops.length > 0 && (
        <Row label="Capacity drops — trimmed to fit the budget">
          <Cells cells={meta.capacity_drops} />
        </Row>
      )}
    </section>
  )
}
