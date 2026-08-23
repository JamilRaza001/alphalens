import type { CitationOut } from '../lib/types'

/** The citation list. `citations` arrives as a bare JSON array and may be empty —
 *  an empty corpus match is a real outcome, so it gets a visible state (AC5). */
export default function CitationList({ citations }: { citations: CitationOut[] }) {
  return (
    <section aria-label="Citations" className="flex flex-col gap-2">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-black/60 dark:text-white/60">
        Citations
      </h2>

      {citations.length === 0 ? (
        <p className="text-sm text-black/60 dark:text-white/60">
          No citations were returned for this answer.
        </p>
      ) : (
        <ol className="flex flex-col gap-1 text-sm">
          {citations.map((citation, index) => (
            <li key={citation.chunk_id} className="flex gap-2">
              <span className="font-mono text-xs text-black/50 dark:text-white/50">
                {index + 1}
              </span>
              <span>
                {citation.ticker} {citation.filing_type} {citation.period_year}
                {citation.section === null ? null : ` · ${citation.section}`}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
