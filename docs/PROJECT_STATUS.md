| Spec | Title | Status | Commit | Notes |
|------|-------|--------|--------|-------|
| S1 | Repo Scaffold | DONE | `11b6a38` | pyproject, ruff, pre-commit, .env.example, dirs |
| S2 | DB Schema + Seed | DONE | `31c7d16` | Neon pgvector schema, 10-company seed, CLAUDE.md |
| S3 | Cloud Resources | DONE | `200d957` | ECR repo + 3-image lifecycle, R2 multipart cleanup (7d), log retention helper, verify_s3.sh 8/8 |
| S4 | Settings (Config) | DONE | `6fab367` | pydantic-settings v2, L17 jina_dimensions lock, L11 OIDC-in-Lambda guard, lru_cache singleton |
| S5 | EDGAR Client | DONE | `a3adc27` | async SEC client, R2 cache-through, token-bucket rate limiter (10 req/s), tenacity retry (429/5xx), SEC_EDGAR_USER_AGENT validator |
| S6 | Section Detector | DONE | `313eaf7` | hybrid text-pattern detection (not DOM), 10-K flat + 10-Q Part I/II disambig, table strip+count, TOC guard, O7 unstructured fallback; 15 unit + 1 real-10-K integration test |
| S7 | Chunker | DONE | `662a78a` | token/section/sentence-aware chunking, ~400 tok/50 overlap, spaCy splitter + nomic counter (DI), oversized-sentence + unstructured fallback, per-section index; 21 unit + 1 real-models integration test |
| S8 | Embedding Client | DONE | `bb038ff` | Jina v3 primary (truncate_dim=768) + nomic-embed-text-v1.5 fallback (native 768d), async w/ lazy nomic load + to_thread, quota auto-flip (8M/10M), 402 whole-call re-embed, 768d asserts, RRF-ready; einops dep added; 20 unit + 1 real-models integration test
| S9 | Upsert Pipeline | DONE | `caefd23` | EmbeddedChunk assembly (count + 768d check, global re-index, per-section idx in metadata, model stamp) + idempotent batched upsert (ON CONFLICT filing_id,chunk_index) + mixed-model + corpus guards; pgvector codec + JSONB; 15 unit + 1 real-Neon integration |
| S10 | Filing State Machine | DONE | `720a7bd` | FilingStatus/JobStatus/IngestionStep StrEnums; MAX_ATTEMPTS=3; COUNT-based retry (no retry_count col); app-side exponential backoff 2/4/8s; claim_retryable_filings two-query + Python filter; caller-owned transactions; 12 unit + 1 real-Neon integration test |
| S11 | ETL Runner | DONE | `c37c8d3` | discover + run orchestrator per Spec 11; drives the 151/151 backfill; runner.py + m01 R2-key migration + 614-line unit suite. S22 hardening (atomic attempt writes, stale-running reaper, meaningful exit codes) added later in `9614651` |
| S12 | Agent State | DONE | `ca832cc` | AgentState TypedDict + QueryPlan/RetrievedChunk/ScoredChunk/Citation/TimeRange (Pydantic v2); D1 confidence Literal["low","high"], D2 single-source query_plan, D3 confidence_reason, D4 RetrievedChunk (agent-side, distinct from ETL Chunk); L5 reducer/L6/L7/L19; live-DB verified (UUIDs, single chunks→filings join, section nullable); mypy --strict clean; spec c0a0f50 |
| S13 | Agent Nodes | DONE | `e9dc3a2` | 5 nodes (Plan/Retrieve-stub/Rerank/Evaluate/Synthesize) as standalone `(state, runtime)→dict` units + prompts.py; Choice B DI via LangGraph Runtime static context; D2 Groq gpt-oss-120b, D3 strict json_schema extraction (Plan→QueryPlan, Evaluate→EvalVerdict), D4 two-signal evaluate w/ coverage precedence (LLM skipped on gaps); Choice A retrieve_node NotImplementedError stub (body→S15); input-rail validate_tickers drop-and-note→unavailable_tickers; stream_synthesis seam (S14 wraps); piggybacks: EvalVerdict (reasoning-first) + unavailable_tickers key + rerank_top_n=5; 14 unit tests, mypy --strict clean; spec docs/specs/S13_agent_nodes.md |
| S14 | Circuit Breaker | DONE | `d570371` | `SynthesisCircuitBreaker` (consecutive-failure, in-memory per-container) guarding the Groq synthesis stream; D1 `is_hard_failure` (5xx/timeout/connection→count, 429+non-429 4xx→excluded+re-raised; live-verified langchain-groq surfaces native `groq.*` exceptions); D2 threshold=3 (env `BREAKER_FAILURE_THRESHOLD`), D3 30s monotonic reset (env `BREAKER_RESET_TIMEOUT_SECONDS`), D4 single HALF_OPEN probe w/ timer restart-from-zero on probe fail; L8; `BreakerState` StrEnum CLOSED/OPEN/HALF_OPEN; `except Exception` (CancelledError propagates); pre-first-token hard fail→degraded fallback, mid-stream→propagate; piggybacks: `AgentContext.breaker` + `synthesize_node` routes S13 `stream_synthesis` seam through `breaker.stream()`, `degraded_stream` (AgentState-agnostic) in nodes.py, 2 Settings fields; 18 unit tests (fakes only), mypy --strict clean; spec docs/specs/S14_circuit_breaker.md |
| Doc | Schema Reconcile — queries as 5th table | DONE | `7d065cb` | queries DDL added to §6.1 + S2 spec + Phase1 guide; financial_facts/entities correctly marked v2-deferred everywhere; decision #9 locked in schema_reconcile_to_live.md |
| Chore | pydantic.mypy plugin | DONE | `b0ac2e6` | strict kwarg checking on Pydantic models; dropped now-redundant type-ignore in config.py |

## Phase 1 ETL Backfill

**Status: 151/151 = 100% processed** (verified 2026-06-13). Zero pending/processing/failed.

- Last 3 (JPM iXBRL filings) closed in S20 via empty-start token bucket + token-aware batching (≤6K tok/req).
- Corpus is 100% `jina-v3` embeddings — homogeneous vector space.
- Known quality caveat: 3 JPM filings parsed iXBRL-as-HTML → blob chunks (419/428/517) → weak retrieval on these until v2 parser.

## Deferred to v2 (quality)

- **D — iXBRL XML parser** + corpus-wide re-ingest (fixes JPM blob chunks)
- **Parent-document retrieval** (spec committed, not implemented)
- **403 fallback bug** — nomic fallback triggers on 402 only; Jina signals balance exhaustion with 403 `AUTHZ_INSUFFICIENT_BALANCE`. Fix trigger; do NOT auto-nomic the existing jina-v3 corpus.
