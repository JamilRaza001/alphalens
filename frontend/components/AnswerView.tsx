import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// @tailwindcss/typography is not installed, so Preflight leaves markdown output
// with no element styling at all. These child selectors are the minimum that
// keeps it legible. Table borders are not decoration: remark-gfm tables are the
// visible evidence for AC10, and an unstyled table reads as broken output.
const MARKDOWN_CLASSES = [
  '[&_h1]:mt-4 [&_h1]:mb-2 [&_h1]:text-xl [&_h1]:font-semibold',
  '[&_h2]:mt-4 [&_h2]:mb-2 [&_h2]:text-lg [&_h2]:font-semibold',
  '[&_h3]:mt-3 [&_h3]:mb-1 [&_h3]:text-base [&_h3]:font-semibold',
  '[&_p]:my-2',
  '[&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-6',
  '[&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-6',
  '[&_li]:my-1',
  '[&_table]:my-3 [&_table]:w-full [&_table]:border-collapse [&_table]:text-sm',
  '[&_th]:border [&_th]:border-black/25 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:font-semibold',
  'dark:[&_th]:border-white/30',
  '[&_td]:border [&_td]:border-black/25 [&_td]:px-2 [&_td]:py-1',
  'dark:[&_td]:border-white/30',
  '[&_code]:rounded [&_code]:bg-black/5 [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-sm',
  'dark:[&_code]:bg-white/10',
  '[&_pre]:my-3 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-black/5 [&_pre]:p-3',
  'dark:[&_pre]:bg-white/10',
  '[&_blockquote]:my-2 [&_blockquote]:border-l-2 [&_blockquote]:border-black/25 [&_blockquote]:pl-3',
  'dark:[&_blockquote]:border-white/30',
].join(' ')

/** Renders the streamed answer. Markdown only — no raw HTML is enabled (D9).
 *  In-answer citation markers stay inert text; nothing here parses them (D10). */
export default function AnswerView({
  text,
  incomplete,
}: {
  text: string
  incomplete: boolean
}) {
  return (
    <section aria-label="Answer" className="flex flex-col gap-2">
      {incomplete && (
        // A stop is a user action, not a failure — neutral styling, never error
        // styling (D7).
        <p className="rounded border border-black/15 px-3 py-1.5 text-sm text-black/70 dark:border-white/20 dark:text-white/70">
          Stopped — this answer is incomplete.
        </p>
      )}

      {/* Re-parsed on every token, deliberately (G9). The cost is measured on a
          real run before any caching is considered. */}
      <div className={MARKDOWN_CLASSES}>
        <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
      </div>
    </section>
  )
}
