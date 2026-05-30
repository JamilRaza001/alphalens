| Spec | Title | Status | Commit | Notes |
|------|-------|--------|--------|-------|
| S1 | Repo Scaffold | DONE | `11b6a38` | pyproject, ruff, pre-commit, .env.example, dirs |
| S2 | DB Schema + Seed | DONE | `31c7d16` | Neon pgvector schema, 10-company seed, CLAUDE.md |
| S3 | Cloud Resources | DONE | `200d957` | ECR repo + 3-image lifecycle, R2 multipart cleanup (7d), log retention helper, verify_s3.sh 8/8 |
| S4 | Settings (Config) | DONE | `6fab367` | pydantic-settings v2, L17 jina_dimensions lock, L11 OIDC-in-Lambda guard, lru_cache singleton |
| S5 | EDGAR Client | DONE | `a3adc27` | async SEC client, R2 cache-through, token-bucket rate limiter (10 req/s), tenacity retry (429/5xx), SEC_EDGAR_USER_AGENT validator |
| S6 | Section Detector | DONE | `313eaf7` | hybrid text-pattern detection (not DOM), 10-K flat + 10-Q Part I/II disambig, table strip+count, TOC guard, O7 unstructured fallback; 15 unit + 1 real-10-K integration test |
| S7 | Chunker | DONE | `662a78a` | token/section/sentence-aware chunking, ~400 tok/50 overlap, spaCy splitter + nomic counter (DI), oversized-sentence + unstructured fallback, per-section index; 21 unit + 1 real-models integration test |
