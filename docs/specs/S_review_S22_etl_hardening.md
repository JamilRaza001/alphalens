# S_review_S22 — ETL Review Remediation

**Source:** Session 22 read-only ETL code review (20 findings).
**Status:** Triaged. This spec covers the **fix-now** batch; deferrals recorded at the end.
**Format:** L21 lightweight (Goal + Signatures + Acceptance Criteria + Gotchas).
**Working rule:** Implement via Claude Code plan-mode (recon → plan → approve → implement → gates → commit). Gates = `ruff` + `mypy --strict` + `pytest` green. One conventional commit per group.

> Naming note: `S_`-prefix (non-build, cross-cutting spec) to avoid colliding with numbered build specs. Agent specs S12–S15 stay reserved.

---

## 0. Scope Summary

| Group | Findings (fix-now) | Files touched |
|---|---|---|
| A — Crash-recovery & automation-readiness | #3, #4, #17 | `etl/state.py`, `etl/runner.py` |
| B — Schema bootstrap reconcile | #10, #11 | `scripts/`, Alembic migrations, `create_schema.sql` |
| C — Client correctness | #5, #6, #7, #13 | `etl/embeddings.py`, `etl/edgar.py`, `etl/runner.py` |
| D — Typing & hygiene | #19, #20 | `pyproject.toml`, `etl/*`, `etl/sections.py` |
| E — Dead code & docs | #8, #9, #12 | `etl/embeddings.py`, `etl/state.py`, `etl/edgar.py` + tests |

**Recommended implementation order:** B → A → C → D → E.
(B is foundational — baseline migration also unblocks the future CI throwaway-DB. A delivers the automation-ready core. C/D/E are independent hygiene.)

---

## Group A — Crash-recovery & Automation-readiness

### A1 (#3) — Make `complete_attempt` / `fail_attempt` atomic

**Goal:** Each of these performs ≥2 writes (job row + `filings.status`); a crash between writes currently leaves `job=done/failed` while `filings.status='processing'`, causing reprocessing. Wrap them so the writes commit all-or-nothing — mirroring `start_attempt`, which already uses `conn.transaction()`.

**Signatures (call-site wrap; no signature change):**
```python
async with conn.transaction():
    await complete_attempt(conn, job_id, ...)   # both UPDATEs inside one tx

async with conn.transaction():
    await fail_attempt(conn, job_id, error=...)  # SELECT + both UPDATEs inside one tx
```

**Acceptance Criteria:**
1. `complete_attempt` and `fail_attempt` call sites in `runner.py` are wrapped in `conn.transaction()`.
2. An abort injected between the two writes leaves DB consistent (either both applied or neither) — verifiable by manual local test.
3. `start_attempt`'s existing transaction usage is unchanged.

**Gotchas:**
- Wrap at the **call site** (runner) consistently with `start_attempt`; do not nest a second transaction inside the functions if the caller already opens one.

---

### A2 (#4) — Startup-only stale-`running` reaper + backoff

**Goal:** A process that crashes mid-pipeline leaves a `running` job; `claim_retryable_filings` re-claims it immediately (`last_failed_at IS NULL` → unconditional, no backoff) and never closes the orphan. Add a **startup-only reaper** that, before claiming, transitions leftover `running` jobs to `failed`/`stale`, so they enter the normal backoff path.

**Signatures:**
```python
async def reap_stale_running(conn: Connection) -> int:
    """Mark leftover 'running' jobs from a prior crashed run as failed/stale.
    Run ONCE at run() startup, before claim_retryable_filings.
    Returns count reaped. Single-runner: any 'running' at startup is a corpse."""
```

**Acceptance Criteria:**
1. `reap_stale_running` runs at `run()` startup, before the first claim.
2. Reaped jobs receive `last_failed_at = now()` (or equivalent) so backoff applies on next claim — no immediate tight-loop retry.
3. Reaper is a **status transition only** — no rows deleted; the per-row audit trail is preserved.
4. Reaped job's filing is NOT re-claimed within its backoff window.

**Gotchas:**
- **No wall-clock timeout heuristic.** A fixed `started_at < now() - Nmin` check conflates "slow (Jina-throttled)" with "dead" and would kill live jobs / waste tokens. Startup-only reaping is safe precisely because the single sequential runner has no live worker at startup.
- **Deferred to v4 (concurrency bucket):** heartbeat/lease liveness + `SELECT ... FOR UPDATE SKIP LOCKED` on the claim. Only needed once processing parallelizes.

---

### A3 (#17) — Meaningful exit codes in `main`

**Goal:** Outer `main` returns `1` for every failure, so a scheduler/CI cannot distinguish "fix the config" from "transient, retry next run." Distinguish exit codes for automation.

**Signatures:**
```python
def main() -> int:
    try:
        ...
        return 0
    except (ValidationError, ConfigError) as exc:   # non-retryable
        _log.error("Config error — not retryable: %s", exc)
        return 2
    except Exception:                               # unexpected runtime
        _log.exception("Unexpected runtime error")
        return 1
```

**Acceptance Criteria:**
1. Success → `0`; config/validation errors → `2` (or a distinct non-retryable code); unexpected runtime → `1`.
2. Per-filing resilience (`runner.py:152`) and fail-attempt fallback (`runner.py:253`) catches are unchanged — they correctly keep the batch alive and log.
3. Each branch logs a clear, categorized message.

**Gotchas:**
- This is forward-prep for scheduled GitHub Actions / CI-CD (that milestone is later); the exit-code contract is established now so automation can rely on it.

---

## Group B — Schema Bootstrap Reconcile (#10 + #11)

**Goal:** `create_schema.sql` is badly stale vs the live DB + S2 spec (missing `companies.cik` PK, `filings.cik`, generated `r2_key`, `ingestion_jobs.step`, full `queries` shape), and the migration chain references a non-existent prior migration. A fresh bootstrap from the repo is currently broken. Fix via **baseline-squash**: treat the **live DB as ground truth**, capture it as a single baseline migration, stamp the live DB at that baseline, and reserve `m01+` for future changes.

**Approach:**
```
m00_baseline.sql / m00_baseline.py   # exact current LIVE schema
  - companies: cik PK, ticker UNIQUE, sic_code, fiscal_year_end
  - filings: cik col, generated r2_key, status, all indexes
  - chunks: VECTOR(768), HNSW (m=16, ef_construction=64), GIN(tsv)
  - financial_facts, entities (v2 stubs)
  - ingestion_jobs: step TEXT CHECK(...), all columns
  - queries: full S2 shape + indexes
  - CREATE EXTENSION vector
```

**Acceptance Criteria:**
1. A single `m00` baseline migration reproduces the **exact live schema** (verified by diffing `\d` output of a fresh-from-m00 DB vs the live DB — identical).
2. Live DB is **stamped** at `m00` (Alembic `stamp`) — baseline is NOT re-run against the populated DB.
3. Stale `create_schema.sql` is deleted (or regenerated as a derived, non-hand-edited snapshot of the baseline).
4. Broken `m01_r2_key_generated.sql` is absorbed into the baseline and removed from the active chain.
5. A fresh DB built from `m00` passes the existing smoke-test schema checks (5 tables, HNSW index, pgvector).
6. `m01+` reserved for the next *future* schema change.

**Gotchas:**
- **Do not run the baseline against the live populated DB** — stamp only. Re-running would error or duplicate. Verify with `\d` afterward that the live schema is unchanged.
- This baseline becomes the foundation for the deferred CI throwaway-Postgres (Group/Deferred #18).
- Keep a **single** source of truth: migrations. Any `create_schema.sql` that remains must be generated from the baseline, never hand-edited.

---

## Group C — Client Correctness

### C1 (#5) — Commit Jina tokens per successful batch

**Goal:** `_total_tokens` is committed only in the end-of-call success branch; a 402 mid multi-batch discards tokens already spent on earlier batches → under-count exactly when balance is low (worst time). Accumulate per batch.

**Signatures:**
```python
for batch in batches:
    resp = await self._jina_paced(batch)          # may raise 402
    self._total_tokens += resp.usage.total_tokens  # commit on each success
```

**Acceptance Criteria:**
1. `_total_tokens` increases as each batch returns, not once at the end.
2. A 402 on batch *k* retains the tokens from batches `1..k-1` in the counter.
3. Usage logs reflect true spend after a partial failure.

**Gotchas:**
- The counter's primary consumer (80% soft cutover) is deferred to v4 with #1, but the counter still feeds **usage logging** today — that's why this is fix-now.

---

### C2 (#6) — Retry transient network errors (edgar + jina)

**Goal:** `_is_retryable` / `_is_retryable_jina` only match `HTTPStatusError`; raw transient network exceptions propagate on first hit. Treat transient network errors as retryable (with the existing exponential backoff).

**Signatures:**
```python
_TRANSIENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)

def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _TRANSIENT):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return False
```

**Acceptance Criteria:**
1. `ConnectError`, `ConnectTimeout`, `ReadTimeout`, `RemoteProtocolError` are retried (both edgar + jina predicates).
2. Permanent statuses (401/402/404) are **not** retried.
3. Retries use the existing `wait_exponential_jitter` policy.

**Gotchas:**
- Per-HTTP-call retry is cheaper than whole-filing job retry (no re-download/re-chunk); keep both layers.

---

### C3 (#7) — Warn on `list_filings` overflow

**Goal:** Only `filings.recent` is read; the `files[]` overflow pages are silently ignored. Harmless for current 10 mega-cap CIKs / 2022–2026, but a silent correctness cliff if scope grows. Add a guard that makes the cliff loud.

**Signatures:**
```python
overflow = data["filings"].get("files") or []
if overflow:
    _log.warning("EDGAR overflow detected for CIK %s: %d extra page(s) NOT read",
                 cik, len(overflow))
```

**Acceptance Criteria:**
1. Non-empty `files[]` logs a warning naming the CIK and page count.
2. Current corpus behaviour is unchanged (no overflow for in-scope CIKs).

**Gotchas:**
- Full pagination/merge is deferred (only the warn-guard now). Revisit when corpus/window grows.

---

### C4 (#13) — Set `ContentType="text/html"` on R2 PUTs

**Goal:** Neither R2 `put_object` sets `ContentType`; objects default to `application/octet-stream`, violating S5 AC#5 and breaking any future direct-serve/preview.

**Signatures:**
```python
await self._r2.put_object(Bucket=bucket, Key=key, Body=content,
                          ContentType="text/html")   # both call sites
```

**Acceptance Criteria:**
1. Both PUTs (`edgar.fetch_primary_doc`, `runner._upload_html_to_r2`) set `ContentType="text/html"`.
2. `grep ContentType src/` returns the two new occurrences.

**Gotchas:**
- Existing 151 objects keep their old type; no re-upload required now (matters only on direct-serve).

---

## Group D — Typing & Hygiene

### D1 (#19) — Full strict typing at boundaries (asyncpg + R2)

**Goal:** Pervasive `Any` around aioboto3 and asyncpg silences `mypy --strict` exactly at the boundaries where wrong kwargs/fields are most likely (it masked #13). Add typed stubs and replace `Any`.

**Signatures / deps:**
```toml
# pyproject.toml [dev]
"boto3-stubs[s3]>=1.34.0",     # or types-aioboto3[s3]
"asyncpg-stubs>=0.29.0",
```
Replace `self._r2: Any`, `Connection[Any]`, and query-result `Any` with concrete types / `Protocol` / `TypedDict` where consumed.

**Acceptance Criteria:**
1. R2 client and asyncpg connections are typed; `put_object` kwargs are statically checked.
2. `mypy --strict` passes **without** boundary `Any` escapes for these clients.
3. Any newly surfaced real type errors are fixed (they are bugs, not noise).

**Gotchas:**
- Stubs are dev-only; runtime behaviour unchanged.

---

### D2 (#20) — `assert` → explicit `if/raise` for the section invariant

**Goal:** `sections.py:265` uses `assert section.char_count == len(section.text)`; `assert` is stripped under `python -O`, silently disabling an integrity guarantee. Use an explicit check.

**Signatures:**
```python
if section.char_count != len(section.text):
    raise ValueError(
        f"char_count mismatch: {section.char_count} != {len(section.text)}"
    )
```

**Acceptance Criteria:**
1. The invariant is enforced regardless of `-O`.
2. Failure raises a clear, message-bearing exception.

**Gotchas:**
- Reserve `assert` for logically-impossible developer-sanity only; integrity/data checks always `if/raise`.

---

## Group E — Dead Code & Docs

### E1 (#8) — Remove orphan Jina module-level path

**Goal:** `jina_embed()`, the lazy `_JINA_HTTP` singleton, and `_get_jina_http` (never closed) have no non-test callers; production goes through `EmbeddingClient`. Dead surface that gives false coverage and a latent connection leak. Remove — with a port-check first.

**Acceptance Criteria:**
1. `grep` confirms no non-test caller of the orphan symbols.
2. Orphan tests are scanned; any **unique, valuable** assertion is **ported** to a production-path (`EmbeddingClient`) test before deletion.
3. Orphan function + singleton + `_get_jina_http` + their dedicated tests are deleted in one commit.
4. Gates green on the clean tree (live-path coverage intact).

**Gotchas:**
- Port-check is mandatory — do not lose real coverage. Net **live-code** coverage must not drop.

---

### E2 (#9) — Fix `IngestionStep` docstring spec refs

**Goal:** Comments label steps `(S3)…(S7)`; actual modules are S5 (edgar), S6 (sections), S7 (chunker), S8 (embeddings), S9 (upsert). Misleading docs.

**Acceptance Criteria:**
1. `state.py:37-41` comments reference the correct `S5…S9` mapping.

**Gotchas:**
- Pure comment edit; zero runtime impact.

---

### E3 (#12) — Document the two-key R2 design (NOT a bug)

**Goal:** The double-write flagged by the review is **intentional** — `fetch_primary_doc` is a write-through cache (HEAD → hit:GET / miss:SEC GET → PUT), keyed by raw accession, kept decoupled from the DB; the canonical `filings/...` `r2_key` is the DB-linked record. Add a comment so it is not re-flagged.

**Signatures (doc-comment only):**
```python
# Two R2 keys by design (NOT a bug):
#   {cik}/{nodash}.html        -> edgar.py write-through download cache
#                                 (HEAD-checked to skip SEC re-fetch)
#   filings/{cik}/{acc}.html   -> canonical r2_key, DB-linked record
# Kept separate so edgar.py stays decoupled from DB schema. See S_review_S22.
```

**Acceptance Criteria:**
1. The rationale comment exists near the cache write and/or the canonical write.
2. No behavioural change — both writes retained.

**Gotchas:**
- The cache copy **is** read (via `head_object`) on every re-fetch; it is not orphaned. The review's "nothing reads it" claim was a false positive.

---

## Deferred — Recorded Decisions (NOT in this batch)

### v4
| # | Item | Reason deferred |
|---|---|---|
| #1 | Hoist `EmbeddingClient` to `run()` scope (cumulative token state) | Jina-v3 preferred; nomic auto-switch not desired; balance topped up. Pair with #2. |
| #2 | Quota-flip → `CorpusModelConflict` dead-end → explicit quota policy (stop-on-flip) | Only bites on mid-run quota cross; corpus is 100% jina-v3 and complete. Pair with #1. |
| #14 | Consolidate the two token-bucket implementations | Both live-tested & serve different purposes (req/s vs TPM); refactor risk > value (rule of three). |
| #15 | Process-global `RateLimiter` (vs per-EdgarClient) | Safe under sequential runner; only matters when parallelizing. Concurrency bucket. |
| #16 | Sanitize Jina error body before DB store | No confirmed leak; single-user; low-risk bodies. |

> Concurrency bucket (do together at v4 parallelization): #4 lease/heartbeat + `SKIP LOCKED`, #14, #15.

### CI/CD milestone (MANDATORY — not optional)
| # | Item | Notes |
|---|---|---|
| #18 | Integration tests on throwaway Postgres + CI wiring | Covers crash-recovery (#3) and reaper (#4) — invisible to current mock suite. Needs a separate disposable DB (NOT live Neon). Built from the #10/#11 baseline. GitHub Actions `services: postgres:16` + pgvector. |

---

## Gate / Done Definition (this batch)

- `ruff format` + `ruff check` clean.
- `mypy --strict` green (no boundary `Any` escapes for R2/asyncpg).
- `pytest` green on a clean tree.
- Live DB `\d` unchanged after #10/#11 stamp (verify manually in WSL).
- One conventional commit per group (B/A/C/D/E); never chain `commit && push`.
