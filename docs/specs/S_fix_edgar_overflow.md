# S-fix — EDGAR Overflow Reading (`list_filings`)

**Status:** authored — not implemented
**Module:** `src/alphalens/etl/edgar.py`
**Locks:** Fix=B (smart overflow) · Scope=M2-only · Floor `2022-01-01` unchanged (NVDA 2021 out)

---

## Root cause (recon-confirmed, do not re-recon)

`list_filings()` only reads `data["filings"]["recent"]` — a fixed-size (~1000-entry / byte-capped)
window over **all** form types. For high-volume filers the window fills with 8-K / 424B / FWP
noise and reaches back only months. Older filings spill into `data["filings"]["files"][]`
(overflow pages), which the current code detects and **deliberately skips** (only logs a warning,
`edgar.py:210-212`). Result: 9 (ticker, year) pairs never enqueued.

- **M2 (this fix, 9 pairs):** GOOGL 2021+22 · JPM 2022+23+24 · META 2021+22+23 — data was in-window
  but lived in unread overflow pages. Gap is monotonic with issuer filing volume
  (JPM 68 overflow pages ≫ META ≫ GOOGL).
- **M1 (out of scope, 1 pair):** NVDA 2021 — cut by the `2022-01-01` filing_date floor. Deferred
  to v2 (fiscal-year axis change lands with KG + XBRL).

Overflow page keys (live-confirmed): `name`, `filingCount`, `filingFrom`, `filingTo`.
`filingFrom`/`filingTo` are date bounds → pages are skippable against `[date_from, date_to]`
without fetching them.

---

## Goal

Read overflow pages too — but **only** those whose `[filingFrom, filingTo]` overlaps the requested
`[date_from, date_to]` window. Everything else (form filter, date filter, rate limit, Pydantic
models, idempotency, public signature) stays exactly as-is.

---

## Signature

**No public signature change.** Contract unchanged:

```python
async def list_filings(
    self,
    cik: str,
    form_types: Iterable[str] = ("10-K", "10-Q"),
    date_from: date = date(2022, 1, 1),
    date_to: date = date(2026, 12, 31),
) -> list[FilingMetadata]:
```

New private helper (pure, unit-testable):

```python
@staticmethod
def _page_overlaps(page: dict[str, Any], date_from: date, date_to: date) -> bool:
    """True if an overflow page's [filingFrom, filingTo] intersects the window.
    Overlap rule: page.filingTo >= date_from AND page.filingFrom <= date_to."""
```

Internal refactor: extract the current `recent`-parsing loop into a shared row-parser so both
`recent` and each fetched overflow page go through the **same** parse + filter path (no duplicated
filter logic).

---

## Acceptance Criteria

1. `list_filings()` parses `filings.recent` as today, then evaluates `_page_overlaps` on **every**
   entry in `filings.files[]`.
2. Only overlapping pages are fetched (`https://data.sec.gov/submissions/{page['name']}`);
   non-overlapping pages are skipped (no GET). JPM fetches only the pages touching 2022–2026, not
   all 68.
3. Every fetched overflow page passes through the **shared rate limiter** (single instance), same as
   `recent`. Fetches are sequential — no parallel page fetches (rate limit is per-IP global).
4. Overflow rows + recent rows are merged, then the **same** `form ∈ form_types` and
   `date_from <= filing_date <= date_to` filters apply to the union. Filter logic is not duplicated.
5. Merged result is **deduplicated on `accession_number`** (defensive — a filing appearing in both
   recent and an overflow page must not double-enqueue).
6. After a `discover` re-run, these 9 pivot cells flip from `--` to populated:
   GOOGL 2021+22, JPM 2022+23+24, META 2021+22+23. **NVDA 2021 stays `--`** (floor unchanged).
7. Full-coverage tickers (AAPL, AMZN, BRK-B, MSFT, TSLA, V) corpus is **unchanged** — no new pairs
   appear. (Their overflow pages either don't touch 2022–2026, or only yield already-present
   accessions → removed by dedup / idempotent insert.) This rail catches any accidental FY2020
   bleed-in.
8. `ruff` clean, `mypy --strict` clean, existing tests green, plus a new unit test for
   `_page_overlaps` (overlap / no-overlap / boundary-touch cases).

---

## Gotchas

- **LIVE-VERIFY overflow page shape before implementing (S28).** `recent` is a flat dict of parallel
  arrays (`form`, `filingDate`, `reportDate`, `accessionNumber`, `primaryDocument`). Older
  `CIK...-submissions-NNN.json` files *usually* share this flat shape — but do **not** assume it.
  Fetch one overflow page's raw JSON first, confirm the keys, and only then wire the shared parser.
  If the shape differs, the parser must branch.
- **Pages are in descending order** (newest first). `_page_overlaps` is order-independent — scan all
  pages; do not break early on the first non-overlapping page.
- **Rate-limit budget.** Each overlapping page is one extra SEC GET. JPM worst case ~2–3 pages. The
  single shared limiter already covers this; keep fetches sequential.
- **Idempotency intact.** New candidate rows still hit `ON CONFLICT (accession_number) DO NOTHING`
  in `discover`. This fix only widens the candidate set; it does not change enqueue behaviour.
- **Window slides — accepted for v1.** Even after the fix the corpus is not perfectly reproducible
  (recon-flagged). A deterministic snapshot is a v2 concern, not a blocker here.

---

## Backfill (post-fix, separate step)

Fix is behaviour-only; no data teardown. All 9 target pairs are category-A (zero existing filings /
sections / chunks / embeddings / R2 objects) → nothing to remove. Sequence:

1. Ship fix (own commit).
2. `discover` re-run → 9 new pairs enqueue via `ON CONFLICT DO NOTHING`; existing rows untouched.
3. `run` (no limit) → processes only the new `pending` filings; already-`processed` rows immune,
   never reprocessed (resumable state machine).
