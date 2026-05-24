# AlphaLens v8 — Agentic RAG over SEC 10-K/10-Q filings

## Stack (locked)

- **Compute:** AWS Lambda Container (FastAPI + LangGraph 5-node + Lambda Web Adapter) — ap-southeast-1
- **Database:** Neon Postgres + pgvector (HNSW `VECTOR(768)` + tsvector GIN)
- **Storage:** Cloudflare R2 (filings cache, $0 egress, 10 GB free)
- **LLM:** Groq LLaMA 3.3 70B Versatile
- **Embeddings:** Jina v3 primary (`truncate_dim=768`) + nomic-embed-text-v1.5 fallback; ms-marco-MiniLM-L-6-v2 reranker (in-process)

## Key Pointers

- Authoritative design doc: `docs/design/AlphaLens_v8.md`
- Current status: `docs/PROJECT_STATUS.md`

## Spec Workflow

Claude Code reads spec from `docs/specs/Sn_*.md`, runs in **Plan mode**, and **waits for user approval before executing**.

## Locked Decisions (L1–L21) — Do Not Re-litigate Without v8→v9 Bump

| # | Decision |
|---|---|
| L1 | `ts_rank_cd` is NOT BM25 — say "lexical search via ts_rank_cd", never "BM25" |
| L2 | Lambda **Container Image** (not ZIP) — PyTorch exceeds 250 MB ZIP limit |
| L3 | Reranker model baked at Docker build time — avoids 10–15s cold-start download |
| L4 | **SSE** not WebSocket — one-way streaming, no ping/pong overhead |
| L5 | `Annotated[list, operator.add]` for `retrieved_chunks` — accumulates across iterations |
| L6 | Refine node deferred to v3; when added, must ONLY update `query` + `query_plan.time_range` |
| L7 | `original_query` is an anchor — never mutate |
| L8 | Groq Circuit Breaker: 5 failures/60s → OPEN 2 min → HALF-OPEN |
| L9 | `embedding_model_version` column in `chunks` — enables gradual Jina→nomic migration |
| L10 | **Function URLs** over API Gateway — free forever, native SSE streaming |
| L11 | **Vercel OIDC auth** on Lambda (`auth=NONE`); PyJWT verifies `VERCEL_OIDC_TOKEN` |
| L12 | Neon **Pooled URL for app**, Direct URL for Alembic migrations |
| L13 | **Region: ap-southeast-1 (Singapore)** — Lambda + Neon co-located |
| L14 | Reranker **merged into Agent Lambda** (in-process) — no separate Lambda |
| L15 | **`uv`** (not pip/poetry) for all dependency management |
| L16 | **Python 3.12 pinned** (`>=3.12,<3.13`) — Lambda runtime parity |
| L17 | **Jina embedding dimensions = 768d** — `VECTOR(768)` everywhere; nomic fallback native 768d |
| L18 | Jina v3 primary + nomic-embed-text-v1.5 fallback; both produce 768d; auto-switch on quota |
| L19 | **5-node LangGraph** (Single-Pass Agentic RAG): Plan → Retrieve+Filter → Rerank → Evaluate → Synthesize |
| L20 | **XBRL parser deferred to v2** — `financial_facts` table schema-present but empty in v1 |
| L21 | **Spec format:** Goal + Function Signatures + Acceptance Criteria + Gotchas (~1 page each) |

## Style Rules

- **English only** in code, specs, and commits (Hinglish OK in informal discussion)
- **No legacy syntax** — latest LangGraph, pgvector, FastAPI conventions; no deprecated APIs
- **`uv` not `pip`** — `uv add`, `uv run`, `uv sync`
- **Python 3.12 pinned** — never use 3.13+ syntax
- Type hints everywhere; `mypy --strict` must pass
- Async-first: `asyncpg`, `httpx` async, FastAPI async endpoints
