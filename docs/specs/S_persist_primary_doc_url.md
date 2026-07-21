# Spec — Persist `primary_doc_url` at discover-time (kill `_process_one` re-list)

**Format:** lightweight (L21).
**Lock:** Opt 2 — persist `primary_doc_url` on the `filings` row at discover-time;
`_process_one` reads it back instead of re-listing EDGAR.
**Supersedes:** the single-day re-list window in `_process_one` (Session-36 defect).

---

## Goal

`_process_one` currently re-lists a filing from EDGAR using a single-day
(`[filing_date, filing_date]`) window to recover the primary document URL. SEC's
declared `filingTo` bounds under-report the true maximum by ~1 day, so this window
falls into a dead zone and raises
`RuntimeError: accession '...' not in EDGAR listing` (e.g. JPM 2025 Q1).

Fix: capture `primary_doc_url` once, at **discover** time (when `list_filings()`
already returns it on the in-memory `FilingMetadata`), persist it on the `filings`
row, and have `_process_one` **read** it from the claimed row. This removes the
re-list entirely — no window, no dead zone.

This is the live blocker for the Session-36 backfill (15 rows: JPM 2021/22/23,
META 2021/22/23 — 13 pending + 2 stuck `processing`).

---

## Contract

### Schema
- Add a **nullable** `primary_doc_url TEXT` column to `filings`.
- Nullable is deliberate: the 165 already-`processed` rows will stay NULL and
  never need it (they never re-list). Only pending/future rows require it.
- Author the migration in **whatever mechanism the repo actually uses** —
  Alembic (`alembic/versions/…`) or raw SQL (`scripts/migrations/…`).
  **CC recon this before writing it; do not assume.** Migration must be
  idempotent / re-runnable.

### `discover()`
- INSERT now includes `primary_doc_url`, sourced from the `FilingMetadata` field
  returned by `list_filings()`. **CC recon the exact attribute name**
  (`primary_doc_url` / `primary_doc` / other) — do not assume.
- Change `ON CONFLICT (accession_number)` from `DO NOTHING` to:
  ```
  ON CONFLICT (accession_number) DO UPDATE
    SET primary_doc_url = EXCLUDED.primary_doc_url
    WHERE filings.primary_doc_url IS NULL
  ```
  This backfills the existing pending rows on a re-discover, touches no other
  column, and never churns an already-set URL.
- `DiscoverReport.filings_enqueued` must still count **only genuinely new
  inserts**, not backfill updates. Detect insert-vs-update via
  `RETURNING (xmax = 0) AS inserted`. **CC recon how `filings_enqueued` is
  currently computed and preserve its meaning** (0 on a pure re-run where all
  rows already exist and are already populated).

### `_process_one()`
- Read `primary_doc_url` from the claimed filing row — the same row fetch that
  already reads `r2_key`. **CC recon the actual `_process_one` signature and the
  row-fetch site before editing.**
- **Remove the re-list** call to `list_filings()` inside `_process_one` entirely.
- If `primary_doc_url IS NULL` for a claimed row, raise a clear, explicit error
  (message: primary_doc_url missing for `<accession>`, re-run discover). Do **not**
  silently fall back to the old re-list path — the dead zone must be unreachable.

### Stuck `processing` rows
- 2 JPM 10-Qs are stuck in `processing` (auto-reaped artifacts of the killed run).
  After the fix they must be re-claimable. **CC recon how the state machine
  reclaims stale `processing` rows** (auto-reap on claim vs manual reset). If a
  manual reset to `pending`/retryable is required, surface it as a runbook step —
  do not add reset logic silently.

---

## Acceptance Criteria

1. `filings` has a nullable `primary_doc_url TEXT`; migration lives in the repo's
   real migration dir and is idempotent.
2. `discover` writes `primary_doc_url` on new inserts.
3. Re-running `discover` backfills `primary_doc_url` on existing rows where it
   `IS NULL`, changing no other column and re-inserting nothing.
4. `discover` idempotency preserved: `filings_enqueued` counts true inserts only
   (0 when every row already exists).
5. `_process_one` reads `primary_doc_url` from the claimed row and contains **no**
   call to `list_filings` / no re-list window.
6. `_process_one` raises an explicit error when `primary_doc_url` is NULL — the
   old single-day dead-zone path is gone.
7. **Live proof (the real target):** after `discover` (backfill) → `run`, all 15
   held rows (JPM 2021/22/23, META 2021/22/23) reach `status='processed'` with
   ≥1 embedded chunk each; the 2 previously-stuck `processing` rows are among them.
8. `ruff` clean, `mypy --strict` clean, full pytest green.

---

## Gotchas

1. **Column must be nullable.** Do not backfill the 165 processed rows; they never
   re-list. NOT NULL would force a bogus value on historical rows.
2. **`ON CONFLICT DO UPDATE … WHERE primary_doc_url IS NULL`** — the WHERE guard
   stops URL churn on re-discover and keeps the write to one column.
3. **Insert count via `RETURNING (xmax = 0)`.** With `DO UPDATE`, a naive rowcount
   counts updates as enqueues. Use the `xmax` trick (or the repo's existing
   mechanism) so `filings_enqueued` stays "new inserts only".
4. **Migration mechanism unknown — recon first.** Alembic vs `scripts/migrations/`
   raw SQL. Match the repo; don't introduce a second mechanism.
5. **`FilingMetadata` URL field name — recon.** The attribute must exist on what
   `list_filings()` returns; verify the exact name before wiring the INSERT.
6. **`_process_one` seam — recon (S28 lesson).** Confirm the real signature and the
   existing row-fetch site; add the `primary_doc_url` read there, not a second query.
7. **Stuck `processing` rows.** Confirm the state machine re-claims them; if not,
   the backfill runbook needs an explicit reset step.
8. **NVDA 2021 is out of scope.** It has no `filings` row at all (M1 floor issue) —
   deferred to v2. This spec only unblocks rows that already exist as `pending`.
