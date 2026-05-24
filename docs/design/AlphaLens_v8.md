# AlphaLens — Master Documentation v8

**SEC Filing Research Agent | Portfolio Project | Near-Zero-Cost Serverless RAG**

> Single source of truth. All earlier versions (v1–v7) are deprecated; refer to v8 for any architectural, operational, or status question.

---

## 0. Quick Summary (paste this into a new chat to resume context)

> **AlphaLens** — RAG agent over SEC 10-K/10-Q filings (top 10 S&P 500, 2022–2026, ~200 filings, ~22k chunks). Query latency target ≤15s, ~$0/mo cost target.
>
> **Stack (locked):** AWS Lambda Container (FastAPI + LangGraph 5-node + Lambda Web Adapter) → Neon Postgres (pgvector HNSW `VECTOR(768)` + tsvector GIN) + Cloudflare R2 (filings cache) + Groq LLaMA 3.3 70B + Jina v3 embeddings (`truncate_dim=768`) + nomic-embed-text-v1.5 fallback + ms-marco-MiniLM-L-6-v2 reranker (in-process). Frontend: Next.js 15 on Vercel via SSE. Region: `ap-southeast-1` (Singapore). Auth: Vercel OIDC (Lambda URL `auth=NONE`, PyJWT middleware). Observability: Opik (LLM traces) + Sentry (frontend errors) + CloudWatch (Lambda logs).
>
> **RAG type:** Single-Pass Agentic RAG. Graph: `Plan → Retrieve+Filter → Rerank → Evaluate → Synthesize`. No retry loop in v1 (deferred to v3). KG-Augmented RAG deferred to v2 (Apache AGE). XBRL parser deferred to v2.
>
> **Version Roadmap:** v1 (now) → v2 (KG + XBRL, week 3–4) → v3 (Fully Agentic retry loop, week 5–6).
>
> **Constraints:** AWS account active with **$100 free-tier credits, 6-month expiry**. ECR has a 12-month free-tier cliff — after ~month 13, ~$0.20–0.30/mo real charge. Pakistani HBL debit card on file (international transactions must be enabled, $30k/yr SBP cap applies).
>
> **Status:** Phase 1 setup ~10% complete (AWS account created with credits + MFA). Outstanding: Neon/R2/Groq/Jina/Vercel/Sentry/Opik signups, 5-layer billing guardrails, IAM scoped users, repo bootstrap, smoke test. Phase 2 (Claude Code spec-driven implementation of 18 specs) **not started**.
>
> **Next step:** Complete Phase 1 Setup Guide checklist (Parts 0–8), then Phase 2 begins with authoring specs 01–18 (lightweight format: Goal + Signatures + Acceptance Criteria + Gotchas), then implementing in order: 1.A (specs 01–09 ETL) → 1.B (10–13 agent) → 1.C (14–18 deploy).
>
> **Locked decisions in v8 §3.** Do not re-litigate without explicit version bump. Open decisions tracked in §11.

---

## Table of Contents

- [1. Document History](#1-document-history)
- [2. Project Overview](#2-project-overview)
- [3. Locked Architectural Decisions](#3-locked-architectural-decisions)
- [4. Tech Stack Reference](#4-tech-stack-reference)
- [5. Architecture Diagrams](#5-architecture-diagrams)
- [6. Database Schema](#6-database-schema)
- [7. Agent Design (LangGraph 5-Node)](#7-agent-design-langgraph-5-node)
- [8. Data Pipeline](#8-data-pipeline)
- [9. Cost Analysis (Revised with $100 Credits)](#9-cost-analysis-revised-with-100-credits)
- [10. Project Status Dashboard](#10-project-status-dashboard)
- [11. Open Decisions (Tracked)](#11-open-decisions-tracked)
- [12. Phase 1 Setup — Outstanding Tasks](#12-phase-1-setup--outstanding-tasks)
- [13. Phase 2 — Spec-Driven Implementation Plan](#13-phase-2--spec-driven-implementation-plan)
- [14. Version Roadmap](#14-version-roadmap)
- [15. Pakistan-Specific Operational Notes](#15-pakistan-specific-operational-notes)
- [16. Working Style & Conventions](#16-working-style--conventions)
- [17. Glossary](#17-glossary)
- [Appendix A — Quick Resume Snippet](#appendix-a--quick-resume-snippet)

---

## 1. Document History

| Version | Date | Changes |
|---|---|---|
| v1–v4 | (earlier) | Initial design iterations |
| v5 | (prior) | Locked stack: EC2 + RDS + S3 + Lambda reranker |
| v5.1 patch | (prior) | DB → Neon, Compute → Lambda (full), Storage → R2 |
| v6 | (prior) | Full consolidated design — superseded v5 + patch |
| v7 | (prior) | Master doc: consolidates v6 + honest cost reality + Pakistan operational notes + $100 AWS credits constraint + project status tracker. Superseded v6. |
| **v8 (current)** | May 1, 2026 | Design Review Session 2: 7 locked changes applied (see below). Supersedes v7. |

### What's New in v8 vs v7

1. **Change #1 (locked in Session 1):** Reranker Lambda merged into Agent Lambda. Single container, in-process call. `L14` superseded.
2. **Change #2 (locked in Session 1):** Auth changed from SigV4 to Vercel OIDC. Lambda URL `auth=NONE` (public). FastAPI middleware verifies `VERCEL_OIDC_TOKEN` via PyJWT + PyJWKClient. `L11` superseded.
3. **Change #3:** 7 LangGraph nodes → 5 nodes. New graph: `Plan → Retrieve+Filter → Rerank → Evaluate → Synthesize`. Filter folded into Retrieve. Refine node deferred to v3. RAG classification: Single-Pass Agentic RAG.
4. **Change #4:** XBRL parser (spec 09) deferred entirely from Phase 1 to v2. `financial_facts` table stays in schema (empty). `entities` table stub added for v2 KG prep.
5. **Change #5:** O1 resolved — Jina embedding dimensions locked at **768d**. `VECTOR(768)` across all 4 touchpoints. Nomic fallback native 768d alignment confirmed.
6. **Change #6:** Nomic fallback (`nomic-embed-text-v1.5`) built in **Spec 06** alongside Jina (not deferred). Jina free tier ~1M tokens → ~2 month runway at current query volume; fallback built proactively.
7. **Change #7:** Spec format locked at **lightweight** — Goal + Function Signatures + Acceptance Criteria + Gotchas (optional). ~1 page per spec. 18 pages total vs 90 pages full format.

---

## 2. Project Overview

### 2.1 Elevator Pitch

**AlphaLens** is a Retrieval-Augmented Generation agent that lets investors and analysts query SEC filings using natural language. Ask *"How did Apple's services revenue evolve from 2022–2026?"* — get a cited, multi-document answer in ~15 seconds.

### 2.2 Phase 1 Scope

| Dimension | Value |
|---|---|
| Companies | Top 10 by S&P 500 market cap |
| Filing types | 10-K (annual) + 10-Q (quarterly) |
| Time range | 2022–2026 |
| Filing volume | ~200 filings |
| Chunk volume | ~22,000 chunks |
| Query latency target | ≤15 seconds end-to-end |
| Concurrent users | 10 (portfolio-demo scale) |
| Monthly queries | ~600 (20 users × 30 queries) |

### 2.3 Demonstrable Competencies (Portfolio Goals)

1. **Production RAG** — multi-document synthesis with citations, hybrid retrieval, cross-encoder reranking
2. **Cost-engineered architecture** — near-zero monthly cost via scale-to-zero serverless across all layers
3. **Modern AI stack** — LangGraph agentic pipeline, hybrid search (HNSW + tsvector + RRF), full observability

### 2.4 Non-Goals

- ❌ Real-time SaaS — cold starts (~3–5s with Jina) acceptable for demo, not for paying users
- ❌ Coverage beyond top-10 S&P — Neon free-tier 500 MB ceiling
- ❌ International filings — focus is US SEC EDGAR only
- ❌ Live financial advice — research tool, not advisory

---

## 3. Locked Architectural Decisions

These decisions are **locked**. Any change requires explicit version bump (v8 → v9) with rationale.

| # | Decision | Rationale |
|---|---|---|
| L1 | `ts_rank_cd` is **NOT BM25** | tsvector uses cover-density ranking. Comments + resume must say "lexical search via ts_rank_cd" — never "BM25" |
| L2 | Lambda **Container Image**, not ZIP | PyTorch + sentence-transformers exceeds 250 MB ZIP limit; container supports up to 10 GB |
| L3 | Reranker model **baked at Docker build time** | Avoids 10–15s cold-start runtime download. Larger image, faster cold start |
| L4 | **SSE**, not WebSocket | One-way token streaming. WebSocket adds bidirectional + ping/pong overhead we don't need |
| L5 | `Annotated[list, operator.add]` for `retrieved_chunks` | LangGraph state mechanic. Accumulates chunks across iterations rather than overwriting (forward-compatible with v3 retry loop) |
| L6 | **RESERVED** — Refine node deferred to v3 | Node 6 (Refine) scope constraint preserved for v3: must ONLY update `query` + `query_plan.time_range`. NEVER mutates `tickers` or `intent` |
| L7 | `original_query` is an **anchor** | Never mutates. Used for logging, citation framing, retry comparison |
| L8 | Circuit Breaker on Groq: **5 failures/60s → OPEN 2 min → HALF-OPEN** | Protects against Groq downtime cascades. Half-open allows 1 trial before full recovery |
| L9 | `embedding_model_version` column in `chunks` table | Enables gradual re-embed migration (Jina → nomic) without big-bang ETL |
| L10 | **Function URLs** over API Gateway | API Gateway costs $1/M after free tier; Function URLs free forever; native SSE streaming |
| L11 | **Vercel OIDC auth** on Lambda (NOT SigV4 / NOT `AWS_IAM`) | **SUPERSEDES v7 L11.** Vercel OIDC eliminates SigV4 complexity from frontend. Lambda URL `auth=NONE` (public URL). FastAPI middleware verifies `VERCEL_OIDC_TOKEN` via PyJWT + PyJWKClient |
| L12 | Neon **Pooled URL for app, Direct URL for migrations** | Pooled (PgBouncer transaction-mode) lacks session features Alembic needs. Direct has 100-conn limit. Both required |
| L13 | **Region: ap-southeast-1 (Singapore)** for both Lambda and Neon | Co-located = sub-ms DB latency. ~30–60 ms RTT from Karachi |
| L14 | **Reranker merged into Agent Lambda (in-process call)** | **SUPERSEDES v7 L14.** No separate Reranker Lambda. Single container. Eliminates boto3 Lambda Invoke overhead, egress charges, extra ECR repo. Model: `ms-marco-MiniLM-L-6-v2` (~80MB), loaded at container startup |
| L15 | **`uv` (not pip/poetry)** for dependency management | 10–100× faster, used in production Dockerfile, dev/prod parity on lockfile |
| L16 | **Python 3.12 pinned** (`>=3.12,<3.13`) | Lambda runtime parity. 3.13 wheels won't run in 3.12 container |
| L17 | **Jina embedding dimensions = 768d** | MRL truncation via `truncate_dim=768`. Locks `VECTOR(768)` in pgvector, HNSW index, Jina API call, and every query vector. Nomic fallback is native 768d — perfect alignment, no padding. MTEB drop ~0.8 pts (within noise). **Resolves O1.** |
| L18 | **Embedding: Jina v3 primary + nomic-embed-text-v1.5 fallback** | Jina free tier ~1M tokens (~2 month runway at 600 queries/month). Nomic fallback built in Spec 06 (not deferred). Auto-switch on quota exhaustion. Both models produce 768d vectors. |
| L19 | **5-node LangGraph graph (Single-Pass Agentic RAG)** | Graph: `Plan → Retrieve+Filter → Rerank → Evaluate → Synthesize`. Filter folded into Retrieve (metadata filters go in WHERE clause). Refine node deferred to v3. Evaluate annotates confidence (low/high) but cannot trigger retry in v1. |
| L20 | **XBRL parser deferred to v2** | Zero v1 quality improvement; 2–3 week cost for taxonomy/context-ID complexity. `financial_facts` table stays in schema (empty). `entities` table stub added for v2 KG prep. |
| L21 | **Spec format: Goal + Function Signatures + Acceptance Criteria + Gotchas** | ~1 page per spec. 18 specs × 1 page ≈ 18 pages total (vs 90 pages full format). Claude Code needs the contract, not prose. |

---

## 4. Tech Stack Reference

| Layer | Technology | Why |
|---|---|---|
| **LLM** | Groq + LLaMA 3.3 70B Versatile | Free tier; sub-second inference |
| **Embeddings (primary)** | Jina v3 (`truncate_dim=768`) | ~1M free tokens; MRL truncation to 768d valid |
| **Embeddings (fallback)** | nomic-embed-text-v1.5 @ 768d | Best free local 768d model: 8192 token context, 86.2% retrieval acc, MRL native, Apache 2.0 |
| **Embedding versioning** | `embedding_model_version` column | Enables gradual migration without big-bang re-embed |
| **Database** | Neon serverless Postgres | Scale-to-zero compute, S3-backed storage; pgvector + tsvector both standard extensions |
| **Vector Index** | HNSW (m=16, ef_construction=64) on `VECTOR(768)` | Optimal recall/latency for 22k chunks; 25% faster than 1024d |
| **Lexical Index** | tsvector + GIN | Postgres-native; no separate search service |
| **Agent Framework** | LangGraph (5-node, single-pass) | Declarative graph, state accumulation via `operator.add` reducer, forward-compatible with v3 retry cycle |
| **Reranker** | `cross-encoder/ms-marco-MiniLM-L-6-v2` (in-process) | Industry standard; baked at build time; ~80MB; no separate Lambda |
| **API** | FastAPI + Server-Sent Events (SSE) | Async-native; SSE simpler than WebSocket for one-way streams |
| **Auth** | Vercel OIDC + PyJWT + PyJWKClient | Eliminates SigV4 from frontend; Lambda URL `auth=NONE` |
| **API Hosting** | AWS Lambda Container + Function URL | 1M req/mo always-free; no API Gateway cost |
| **Frontend** | Next.js 15 on Vercel | Hobby tier free; built-in SSE consumption |
| **Object Storage** | Cloudflare R2 | 10 GB forever-free; **$0 egress** (vs S3's $0.09/GB) |
| **Observability** | Opik (LLM traces) + Sentry (FE errors) + CloudWatch (Lambda logs) | All free tiers |
| **CI/CD** | GitHub Actions | 2000 min/mo free; pushes to ECR + Vercel |
| **Local Dev** | uv + Python 3.12 + Docker Desktop | Modern Python tooling; Docker for E2E parity |

---

## 5. Architecture Diagrams

### 5.1 High-Level Topology

```
┌─────────────┐
│   User      │
│ (browser)   │
└──────┬──────┘
       │ HTTPS
       ▼
┌─────────────────────────┐         ┌──────────────────┐
│  Vercel (Next.js)       │ ◄──────►│  Sentry          │
│  - Hobby tier (free)    │  errors │  Frontend errors │
│  - Edge-cached          │         └──────────────────┘
└──────┬──────────────────┘
       │ POST /api/query (SSE)
       │ Vercel OIDC token in header
       ▼
┌─────────────────────────────────────────┐
│  AWS Lambda (Agent — single container)  │
│  - FastAPI + LangGraph 5-node           │
│  - ms-marco-MiniLM-L-6-v2 (in-process) │ ◄────► Groq LLM
│  - Function URL auth=NONE               │        (Llama 3.3 70B)
│  - PyJWT OIDC middleware                │
│  - Jina v3 primary / nomic fallback     │
│  - 1M req/mo always-free                │
│  - Region: ap-southeast-1 (Singapore)   │
└────┬────────────────────────────────────┘
     │ asyncpg (pooled, max=5)
     ▼
┌──────────────────┐
│ Neon Postgres    │
│ - Free forever   │
│ - pgvector+HNSW  │
│   VECTOR(768)    │
│ - tsvector+GIN   │
│ - Scale-to-zero  │
│ - 500MB / 100CU-h│
└──────────────────┘

       Async ETL (separate flow, runs from local or GitHub Actions)
       ┌────────────────────┐
       │ EDGAR → Chunks →   │ ───► Cloudflare R2
       │ Embeddings (Jina   │      (filings cache)
       │ or nomic fallback) │      10 GB free, $0 egress
       │ → Neon upsert      │
       └────────────────────┘
```

### 5.2 Agent Loop (LangGraph 5-Node — Single-Pass Agentic RAG)

```
┌──────────────┐
│ 1. Plan      │ LLM-driven decomposition → tickers, intent,
│              │ time_range, sub_questions, entity extraction
└──────┬───────┘
       ▼
┌──────────────┐
│ 2. Retrieve  │ Hybrid: HNSW vector + tsvector lexical
│  + Filter    │ Metadata filters in WHERE clause (ticker,
│              │ time_range, section_type) → top-K via RRF
└──────┬───────┘
       ▼
┌──────────────┐
│ 3. Rerank    │ Cross-encoder (in-process) → top-N
│              │ ms-marco-MiniLM-L-6-v2
└──────┬───────┘
       ▼
┌──────────────┐
│ 4. Evaluate  │ LLM self-assessment on retrieved evidence
│              │ → annotates confidence: low | high
│              │ (annotation only — no retry in v1)
└──────┬───────┘
       ▼
┌──────────────┐
│ 5. Synthesize│ Groq LLM streams answer with citations via SSE
│              │ Low-confidence flag surfaced in response if set
└──────────────┘

Note: Evaluate → Refine → Retrieve retry cycle is v3.
      KG traversal branch inside Retrieve is v2.
```

---

## 6. Database Schema

### 6.1 Tables

```sql
-- companies: 10 rows (top S&P 500)
CREATE TABLE companies (
    cik VARCHAR(10) PRIMARY KEY,         -- Central Index Key (zero-padded)
    ticker VARCHAR(10) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    sic_code VARCHAR(4),                 -- Industry classification
    fiscal_year_end VARCHAR(4),          -- 'MMDD' format e.g. '0930' for Apple
    created_at TIMESTAMPTZ DEFAULT now()
);

-- filings: ~200 rows
CREATE TABLE filings (
    id BIGSERIAL PRIMARY KEY,
    cik VARCHAR(10) REFERENCES companies(cik),
    accession_number VARCHAR(20) UNIQUE NOT NULL,  -- SEC's globally unique ID
    form_type VARCHAR(10) NOT NULL,                -- '10-K' | '10-Q'
    filing_date DATE NOT NULL,
    period_of_report DATE NOT NULL,
    primary_doc_url TEXT NOT NULL,
    r2_html_key TEXT,                              -- R2 cache key
    state VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|downloading|parsing|chunking|embedding|indexed|failed
    state_updated_at TIMESTAMPTZ DEFAULT now(),
    error_message TEXT,
    retry_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_filings_cik_period ON filings(cik, period_of_report DESC);
CREATE INDEX idx_filings_state ON filings(state) WHERE state != 'indexed';

-- chunks: ~22,000 rows
CREATE TABLE chunks (
    id BIGSERIAL PRIMARY KEY,
    filing_id BIGINT REFERENCES filings(id) ON DELETE CASCADE,
    section VARCHAR(100) NOT NULL,
    section_order INT NOT NULL,
    chunk_index INT NOT NULL,
    text TEXT NOT NULL,
    token_count INT NOT NULL,
    embedding VECTOR(768),                         -- Locked: 768d (L17). Jina truncate_dim=768 or nomic native 768d
    embedding_model_version VARCHAR(50) NOT NULL,  -- 'jina-v3' | 'nomic-embed-text-v1.5'
    tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_chunks_embedding_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_chunks_tsv ON chunks USING GIN (tsv);
CREATE INDEX idx_chunks_filing_section ON chunks(filing_id, section);

-- financial_facts: schema present, empty in v1 (XBRL parser deferred to v2)
CREATE TABLE financial_facts (
    id BIGSERIAL PRIMARY KEY,
    filing_id BIGINT REFERENCES filings(id) ON DELETE CASCADE,
    concept VARCHAR(200) NOT NULL,         -- e.g., 'us-gaap:Revenues'
    period_start DATE,
    period_end DATE NOT NULL,
    value NUMERIC(20, 4) NOT NULL,
    unit VARCHAR(20),
    decimals INT,
    context_ref VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_facts_concept_period ON financial_facts(concept, period_end DESC);

-- entities: schema stub for v2 KG (Apache AGE). Empty in v1.
-- Populated by entity extraction at ingestion time in v2.
CREATE TABLE entities (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,      -- 'company' | 'person' | 'metric' | 'product'
    cik VARCHAR(10) REFERENCES companies(cik),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_entities_name_type ON entities(name, entity_type);
```

### 6.2 Storage Math

| Component | Size |
|---|---|
| 22k embeddings × 768d × 4B | ~66 MB |
| HNSW index overhead (~1.5×) | ~100 MB |
| Chunk text + tsvector | ~88 MB |
| Other tables | ~50 MB |
| Postgres overhead | ~30 MB |
| **Total** | **~334 MB** |

Free tier: 500 MB. Headroom: ~33%.

> ✅ Locking 768d (vs 1024d) saves ~22 MB on embeddings and reduces HNSW footprint proportionally — confirmed well within free tier.

---

## 7. Agent Design (LangGraph 5-Node)

### 7.1 State Schema

```python
from typing import Annotated, AsyncGenerator
from operator import add
from typing_extensions import TypedDict


class AgentState(TypedDict):
    # Anchors (set once, NEVER mutated)
    original_query: str
    user_id: str | None
    request_id: str

    # Plan (set in Node 1 — Plan)
    query_plan: QueryPlan       # tickers, intent, time_range, sub_questions
    tickers: list[str]          # Anchored after Node 1
    intent: str                 # 'comparative' | 'temporal' | 'factual' | 'qualitative'
    entities: list[str]         # Extracted for v2 KG traversal (used in v1 for metadata filtering)

    # Mutable per-iteration
    query: str                  # Reformulated in v3 Refine node (pass-through in v1)
    iteration: int

    # Accumulated state (operator.add — forward-compatible with v3 retry loop)
    retrieved_chunks: Annotated[list[Chunk], add]
    reranked_chunks: list[ScoredChunk]  # Reset each pass

    # Output
    answer_stream: AsyncGenerator[str, None] | None
    citations: list[Citation]
    confidence: float           # Set by Evaluate node; surfaced in response if low
```

### 7.2 Node Responsibilities

| Node | Responsibility | LLM call? |
|---|---|---|
| **1. Plan** | Decompose query → tickers, intent, time_range, sub_questions, entities | ✅ Yes |
| **2. Retrieve+Filter** | Hybrid HNSW + tsvector search; metadata filters in WHERE clause; RRF fusion | ❌ No |
| **3. Rerank** | Cross-encoder in-process (ms-marco-MiniLM-L-6-v2) → top-N scored chunks | ❌ No |
| **4. Evaluate** | LLM self-assessment on evidence quality → annotate `confidence` (low/high) | ✅ Yes |
| **5. Synthesize** | Groq streams answer with citations via SSE; surfaces low-confidence flag if set | ✅ Yes |

### 7.3 RAG Classification

- **"Agentic"** — Plan node does LLM-driven query decomposition; Evaluate node does LLM self-assessment
- **"Single-Pass"** — Evaluate annotates confidence but cannot trigger a retry in v1
- **NOT "Fully Agentic"** — that requires Evaluate → Refine → Retrieve cycle (v3)

### 7.4 Circuit Breaker (Groq)

```python
class GroqCircuitBreaker:
    failure_threshold = 5         # failures
    failure_window = 60           # seconds
    open_duration = 120           # seconds (2 minutes)
    half_open_trial_requests = 1
    states = ['CLOSED', 'OPEN', 'HALF_OPEN']
```

**Transitions:**
- CLOSED → OPEN: 5 failures within 60s
- OPEN → HALF_OPEN: After 2 minutes
- HALF_OPEN → CLOSED: 1 successful request
- HALF_OPEN → OPEN: 1 failed request

**When OPEN:** agent returns degraded response (top reranked chunks without LLM synthesis), with explicit "LLM unavailable" notice in citations.

---

## 8. Data Pipeline

### 8.1 Ingestion Flow

```
EDGAR API → Filing Metadata → R2 Cache (HTML) →
  → Section Detector → Chunker →
  → Embedding (Jina v3 primary | nomic-embed-text-v1.5 fallback) →
  → Neon Upsert → state='indexed'
```

### 8.2 Filing State Machine

```
pending → downloading → parsing → chunking → embedding → indexed
                  ↓
                failed (retry up to 3 times with exponential backoff)
```

### 8.3 Chunking Strategy

- **Target chunk size:** 400 tokens (balance recall vs. context fit)
- **Overlap:** 50 tokens (preserve context across chunk boundaries)
- **Section-aware:** Don't span across major sections (e.g., chunk doesn't cross from "Item 1A. Risk Factors" into "Item 2. Properties")
- **Sentence-boundary aware:** spaCy or blingfire — TBD, see §11 O2

### 8.4 Embedding Strategy (Jina + Nomic Fallback)

```python
def embed(texts: list[str], use_fallback: bool = False) -> list[list[float]]:
    """
    Primary:  Jina v3 API (truncate_dim=768)
    Fallback: nomic-embed-text-v1.5 (native 768d, loaded locally)
    Auto-switch: jina_quota_exceeded() → use_fallback=True
    Both paths produce VECTOR(768) — pgvector schema unchanged.
    """
    if use_fallback or jina_quota_exceeded():
        return nomic_embed(texts)   # local model, task prefixes required
    return jina_embed(texts)        # API call, truncate_dim=768
```

**Nomic task prefixes (required):**
- Queries: `"search_query: " + text`
- Chunks at ingestion: `"search_document: " + text`

**Migration path (Jina → nomic):**
1. Quota check hits 80% → enable nomic path for new ingestion
2. Backfill existing chunks in batches with `embedding_model_version='nomic-embed-text-v1.5'`
3. Switch query path to nomic once 100% backfilled
4. Drop Jina rows

Trigger: Jina free tier at 80% utilization (O6).

### 8.5 Embedding Versioning

`chunks.embedding_model_version` values: `'jina-v3'` | `'nomic-embed-text-v1.5'`

Both produce `VECTOR(768)` — no schema migration needed when switching. Migration is a data backfill, not a schema change.

---

## 9. Cost Analysis (Revised with $100 Credits)

### 9.1 Honest Cost Reality

1. **AWS pre-authorizes $1 USD at signup** — refunded in 3–5 business days. Real money momentarily charged on debit cards.
2. **ECR free tier is 12-month, NOT forever** — 500 MB private storage for 12 months only. After expiry, all storage at $0.10/GB/month.
3. **Cloudflare R2 free tier IS forever** — 10 GB storage, 1M Class A ops, 10M Class B ops, $0 egress; payment method on file required but not charged within limits.
4. **AWS $100 credits, 6-month expiry** — covers months 1–6 of any overage; expires after.
5. **Jina free tier ~1M tokens** — at 600 queries/month (~11 embed calls/query = ~660k tokens/month), exhausted in ~2 months. Nomic fallback covers remainder.

### 9.2 Monthly Cost Projection (Realistic Demo Traffic)

Demo traffic baseline: 600 queries/month, ~1.8k reranker calls (in-process, no Lambda cost), ~0 warmup pings (Jina path cold start 3–5s, acceptable).

| Service | Months 1–6 (with $100 credit) | Months 7–12 | Month 13+ |
|---|---|---|---|
| Neon | $0 | $0 | $0 |
| Lambda agent (merged) | $0 (credit) | $0 (free tier) | $0 (free tier) |
| Cloudflare R2 | $0 | $0 | $0 |
| CloudWatch Logs | $0 | $0 | $0 |
| **ECR storage** | $0 (credit) | ~$0.20 (500 MB free, ~2GB paid) | ~$0.25 (no free tier) |
| Groq | $0 | $0 | $0 |
| Jina | $0 (~1M tokens free) | $0 (nomic fallback after exhaustion) | $0 |
| Vercel | $0 | $0 | $0 |
| Sentry | $0 | $0 | $0 |
| Opik | $0 | $0 | $0 |
| **TOTAL** | **$0** | **~$0.20/mo** | **~$0.25/mo** |

> **Note on container size:** Single-container architecture (reranker merged in) + nomic fallback included → image ~2.1GB (CPU PyTorch ~900MB + nomic ~550MB + reranker ~80MB + deps). ECR real footprint with layer dedup may be ~1.5–1.8GB.

### 9.3 The $100 AWS Credit — How to Use It Strategically

| Credit usage strategy | Why |
|---|---|
| **Don't accelerate spend** — let credits sit and absorb actual usage | Burns naturally; protects against unexpected spikes |
| **Treat credits as a safety buffer, NOT free budget** | A runaway loop in development could spike spend by $20–50 in hours; credits absorb mistakes |
| **Don't enable Provisioned Concurrency or other "boost" features** | These would consume credits faster without portfolio benefit |
| **Track credit usage weekly** in AWS Billing → Bills → Credits tab | If credits are draining unexpectedly, investigate before they run out |

### 9.4 Vigilance List (What Triggers Real Charges)

Monitor weekly:

1. Lambda invocation count (runaway loops)
2. CloudWatch Logs storage (set 7-day retention on every log group at creation)
3. ECR image count (lifecycle policy: keep last 3 only)
4. Neon CU-hours (heavy load testing)
5. R2 Class A operations (bulk re-uploads)
6. **AWS credits remaining** — Billing → Credits → expiration date and balance
7. **Jina token usage** — dashboard/API; migrate to nomic at 80%

### 9.5 Five-Layer Billing Defense (Setup Guide Part 3)

1. AWS Free Tier alerts (85% per-service)
2. AWS Zero-Spend Budget ($0.01)
3. AWS Cost Budget ($1.00 with 80% forecast + 50% actual + 100% actual)
4. CloudWatch Billing Alarm ($0.01 in `us-east-1`)
5. AWS Budget Action ($0.50 → auto-attach `AlphaLensEmergencyDeny` IAM policy to deployer user)

### 9.6 Truly $0 Alternative (If Credits Run Out)

If after 6 months the user wants to push monthly cost from ~$0.25 to literal $0:

- **Switch ECR private → ECR public repos.** 50 GB always-free for public. Tradeoff: Docker images are publicly readable. Acceptable since application code is already on public GitHub for portfolio purposes.
- Decision deferred to month 6 evaluation (O14).

---

## 10. Project Status Dashboard

### 10.1 Current Status: **Phase 1 Setup, ~10% complete**

### 10.2 Completed ✅

| Item | Detail | Date |
|---|---|---|
| AWS account created | Personal account, primary region `ap-southeast-1` | (recently) |
| AWS $100 free-tier credits | Active, 6-month expiry from account creation | (recently) |
| AWS root MFA | Enabled (per setup guide Part 2.1.a) | (recently) |
| Design Review Session 1 | Changes #1 + #2 locked | Session 1 |
| Design Review Session 2 | Changes #3–#7 locked, v8 authored | May 1, 2026 |

### 10.3 In Progress 🟡

*(Nothing actively in progress as of v8 publication.)*

### 10.4 Pending — Phase 1 Setup ⬜

Reference: `Phase1_Setup_Guide.md`. Remaining tasks:

| Part | Item | Status |
|---|---|---|
| Part 1 | Local environment: uv, Python 3.12, Docker, Git, AWS CLI v2, VS Code | ⬜ |
| Part 2.2 | Neon project + pgvector | ⬜ |
| Part 2.3 | Cloudflare R2 bucket + API token | ⬜ |
| Part 2.4 | Groq API key | ⬜ |
| Part 2.5 | Jina API key (note actual free tier limit on signup) | ⬜ |
| Part 2.6 | Vercel account | ⬜ |
| Part 2.7 | Sentry org + Next.js project | ⬜ |
| Part 2.8 | Opik workspace | ⬜ |
| Part 2.9 | GitHub repo `alphalens` cloned | ⬜ |
| Part 3 | All 5 billing guardrail layers | ⬜ |
| Part 4 | IAM admin + deployer + frontend-invoke users | ⬜ |
| Part 5 | Repo bootstrap (pyproject, ruff, pre-commit, dirs) | ⬜ |
| Part 6 | Service init (Alembic stub, R2 lifecycle, ECR repo + lifecycle) | ⬜ |
| Part 7 | Smoke test passes 5/5 | ⬜ |

> Note: Only ONE ECR repo needed now (reranker merged into agent container).

### 10.5 Pending — Phase 2 Implementation ⬜

| Sub-phase | Specs | Status |
|---|---|---|
| 1.A — Core Pipeline | 01–08 (settings, schema, EDGAR, sections, chunker, embeddings+fallback, upsert, state machine) | ⬜ Not authored, not implemented |
| 1.B — Agent | 09–12 (state, nodes, circuit breaker, retrieval) | ⬜ Not authored, not implemented |
| 1.C — Deployment | 13–17 (Dockerfile, Lambda deploy, R2 setup, observability, frontend) | ⬜ Not authored, not implemented |

> Note: Spec numbering shifted — XBRL parser removed from Phase 1 (was spec 09, now deferred to v2). See §13 for updated spec list.

---

## 11. Open Decisions (Tracked)

| # | Decision | Trigger to resolve | Default if not resolved | Status |
|---|---|---|---|---|
| O1 | Jina embedding dim: 768 vs 1024 | — | — | ✅ **RESOLVED: 768d (L17)** |
| O2 | **spaCy vs blingfire** for sentence tokenization | Spec 05 (chunker) | spaCy (better for financial abbreviations) | ⏳ Pending |
| O3 | **HyDE cost-benefit** — retry-only or first-pass too? | Spec 11 (agent nodes) | Retry-only (saves Groq tokens) | ⏳ Pending — note: HyDE only relevant in v3 retry loop; confirm if applicable to v1 at spec 10 authoring |
| O4 | **Golden dataset** — 100–150 manual query/answer pairs sufficient? | Phase 2 evaluation milestone | Yes, manual curation | ⏳ Pending |
| O5 | **SSE disconnect → Groq stream cancel** correctness | Spec 17 (observability) testing | Add explicit cancellation token | ⏳ Pending |
| O6 | **Jina migration trigger** — 80% of free token grant? | Jina free tier balance reaches threshold | 80% utilization | ⏳ Pending |
| O7 | **Section detection fallback** for non-standard filings | Spec 04 (section detector) | Tag as "unstructured", skip section-aware chunking, flag in `metadata` | ⏳ Pending |
| O8 | **Lambda cold start mitigation priority** | Spec 14 (Lambda deployment) | Warmup ping + lazy imports first; SnapStart only if still problematic | ⏳ Pending |
| O9 | **Neon free tier overflow alert** at 80% or 90%? | Storage approaches limit | 80% (more reaction time) | ⏳ Pending |
| O10 | **ECR cleanup automation** — Lambda cron / GitHub Action / manual? | After 3 redeploys | GitHub Action | ⏳ Pending |
| O11 | **Lambda max concurrency** — default 1000 excessive? | Spec 14 deployment | Lower to 10 (caps blast radius) | ⏳ Pending |
| O12 | **Cross-region tradeoff** — ap-southeast-1 vs ap-south-1? | Spec 14 deployment | Stick with ap-southeast-1 (DB co-location) | ⏳ Pending |
| O13 | **Jina actual free tier size** — 10M or 1M? | Jina signup (Part 2.5) | Plan for 1M (worst case) | ⏳ Confirm on signup |
| O14 | **ECR private vs public** at month 7 | Month 6 review | Decide based on credit balance | ⏳ Pending |

---

## 12. Phase 1 Setup — Outstanding Tasks

The full operational walkthrough is in `Phase1_Setup_Guide.md`. This section is the **status tracker** for that guide.

### 12.1 Critical-Path Sequence

Execute in this order — out-of-order execution causes preventable failures:

```
1. Local environment (Part 1)        ← prerequisites for everything
2. AWS billing guardrails (Part 3)   ← BEFORE any deploy. Already partial: account+MFA done; budgets+alarms ⬜
3. AWS IAM users (Part 4)            ← required to complete Layer 5 budget action
4. Neon, R2, Groq, Jina, Vercel,
   Sentry, Opik signups (Part 2)     ← can be parallel; do after IAM
5. Repo bootstrap (Part 5)
6. Service init: Alembic, R2 lifecycle,
   ECR repo (Part 6)                 ← single ECR repo now (reranker merged)
7. Smoke test (Part 7) — GATING      ← MUST pass 5/5 before Phase 2
```

### 12.2 Time Budget

- **Day 1 (5 hrs):** Parts 1–4 (local + remaining accounts + billing + IAM)
- **Day 2 (3 hrs):** Parts 5–7 (repo + service init + smoke test)

### 12.3 Definition of Done — Phase 1 Setup

Phase 1 setup is complete when **every** box in `Phase1_Setup_Guide.md` Part 8 is ticked AND `scripts/smoke_test.py` shows all 5 checks passing.

---

## 13. Phase 2 — Spec-Driven Implementation Plan

### 13.1 Philosophy

Specs are source of truth. Code follows specs. Disagreement → update spec first, then code. This is Claude Code's strongest workflow: feed it a tight spec, let it generate, review, iterate.

### 13.2 Spec Format (Locked — L21)

Each spec is ~1 page with exactly 4 sections:

```
## Spec XX — [Component Name]

### Goal
One paragraph. What this component does and why it exists in the pipeline.

### Function Signatures
All public interfaces with type hints and a one-line docstring each.
No implementation — just the contract.

### Acceptance Criteria
Numbered list. Each item is concrete and testable.
"Returns VECTOR(768)" not "handles embeddings correctly."

### Gotchas (optional)
2-3 bullets max. Non-obvious implementation traps.
```

**Why this format for Claude Code:** Claude Code needs the contract (signatures + criteria), not prose explanations. Acceptance criteria doubles as pytest scaffold.

### 13.3 Build Order (Locked)

#### Phase 1.A — Core Pipeline (specs 01–08)

| Spec | Title | Depends on | Notes |
|---|---|---|---|
| 01 | `01_settings.md` — pooled+direct DB URLs, R2 config, all env settings | — | |
| 02 | `02_db_schema.md` — 5 tables (incl. entities stub), HNSW `VECTOR(768)` + GIN indexes, Alembic baseline | 01 | |
| 03 | `03_edgar_client.md` — SEC API client with rate limiting | 01 | |
| 04 | `04_section_detector.md` — parse 10-K/10-Q sections | 03 | |
| 05 | `05_chunker.md` — token-aware, section-aware chunking | 04 | |
| 06 | `06_embedding_client.md` — Jina v3 primary (`truncate_dim=768`) + nomic-embed-text-v1.5 fallback | 01 | Both paths built here |
| 07 | `07_upsert_pipeline.md` — Neon batched insert with embedding versioning | 02, 06 | |
| 08 | `08_filing_state_machine.md` — state transitions + retry logic | 02, 03 | |

> Note: Spec 09 (XBRL parser) removed from Phase 1. Deferred to v2. Spec numbering in 1.B/1.C shifted accordingly.

#### Phase 1.B — Agent (specs 09–12)

| Spec | Title | Depends on | Notes |
|---|---|---|---|
| 09 | `09_agent_state.md` — LangGraph state schema + reducers | 02 | 5-node state (no Refine in v1) |
| 10 | `10_agent_nodes.md` — all 5 nodes with prompts + transitions | 09 | Plan, Retrieve+Filter, Rerank, Evaluate, Synthesize |
| 11 | `11_circuit_breaker.md` — Groq circuit breaker | 10 | |
| 12 | `12_retrieval.md` — hybrid search (HNSW + tsvector) + RRF fusion + metadata filters | 02, 06 | |

#### Phase 1.C — Deployment (specs 13–17)

| Spec | Title | Depends on | Notes |
|---|---|---|---|
| 13 | `13_lambda_dockerfile.md` — single Dockerfile (agent + reranker merged) | 1.A + 1.B complete | One container, not two |
| 14 | `14_lambda_deployment.md` — Function URL auth=NONE, Vercel OIDC middleware, IAM, ECR push | 13 | |
| 15 | `15_r2_setup.md` — bucket lifecycle + IAM policy | — | |
| 16 | `16_observability.md` — Opik + Sentry + CloudWatch wiring | 14 | |
| 17 | `17_frontend.md` — Next.js + Vercel + SSE consumption + OIDC token injection | 14 | |

**Total: 17 specs** (was 18 — XBRL removed from Phase 1)

### 13.4 Definition of Done — Phase 2

- All 17 specs authored, reviewed, committed
- All implementations pass per-spec acceptance criteria
- ETL pipeline ingests all ~200 filings end-to-end with state='indexed' for ≥95%
- Agent answers 10 sample queries from golden dataset with citations
- E2E latency on warm Lambda ≤15s for 95th percentile
- Deployed to production (Lambda + Vercel), Function URL accessible, frontend loads
- All observability integrations show data (Opik traces, Sentry events, CloudWatch logs)

---

## 14. Version Roadmap

```
v1 — NOW (this build)
     Single-Pass Agentic RAG
     Plan → Retrieve+Filter → Rerank → Evaluate → Synthesize
     Linear graph, no retry loop, no KG
     Goal: Working pipeline, real queries, real results

     ↓ 2 weeks — measure results, collect query failures

v2 — WEEK 3-4
     KG-Augmented Agentic RAG
     Add: Apache AGE on Neon Postgres (openCypher inside Postgres — no new service)
     Add: Entity extraction at ingestion time → entities table
     Add: Graph traversal branch inside Retrieve+Filter node
     Add: XBRL parser → financial_facts → graph nodes
     Goal: Better trend/comparison queries (e.g. "Apple margin 2022 vs 2024")

     ↓ 2 weeks — measure KG impact vs pure vector

v3 — WEEK 5-6
     Fully Agentic RAG
     Add: Refine node (LLM query rewriter)
     Add: Evaluate → Refine → Retrieve cycle
     Add: Loop termination (max_retries, confidence threshold)
     Goal: Self-correcting pipeline for ambiguous queries
```

**Why this cadence works:** Each version produces real failure data that justifies the next upgrade. v1 failures will be trend/comparison queries (→ motivates KG). v2 remaining failures will be ambiguous/vague queries (→ motivates retry loop). Evidence-driven iteration, not speculation-driven over-engineering.

**Why v2 fits AlphaLens:** 10-K filings have two distinct data types:
- Narrative text (MD&A, risk factors) → vector search handles well
- Structured financial facts (revenue, EPS, margins, YoY trends) → graph is genuinely better here

---

## 15. Pakistan-Specific Operational Notes

### 15.1 Debit Card Setup (HBL or equivalent)

Required before ANY service signup that asks for payment method (AWS, Cloudflare R2):

| Check | Action |
|---|---|
| Card scheme is Visa or Mastercard (NOT PayPak only) | Confirm logo on card. PayPak-only cards fail international transactions universally. |
| International transactions enabled | HBL Mobile app → Card Management → International Transactions toggle ON. Or call 111-111-425. |
| 3D Secure / OTP setup | Mobile number registered with bank (OTP arrives via SMS for every online international transaction). |
| USD buffer in account | Keep PKR ~1000 (~$3.50) buffer. Debit cards actually deduct $1 pre-auth temporarily; credit cards just hold. Refund 3–5 business days. |

### 15.2 SBP $30k/year Cap

State Bank of Pakistan caps individual cross-border card transactions at **$30,000/year cumulative across all cards/banks, tracked by CNIC**. Resets November 1 annually.

For AlphaLens (~$3/year usage): irrelevant. For combined cloud usage across multiple projects: monitor.

### 15.3 Bank Reliability for Cloud Platforms

| Bank | AWS reliability | Notes |
|---|---|---|
| HBL Visa Debit | ✅ Reliable | Most commonly used by Pakistani devs |
| Standard Chartered | ✅ Reliable | International-friendly card behavior |
| MCB Visa | ✅ Reliable | Some users report needing repeat OTP attempts |
| UBL | ✅ Reliable | — |
| Meezan | ⚠️ Inconsistent | Some users report errors on cloud platforms |
| NayaPay / SadaPay virtual cards | ❌ Inconsistent | Reports of decline on AWS, GCP |
| Any PayPak-only | ❌ Will not work | Cannot process international transactions |

### 15.4 Network / Latency Notes

- **Karachi → Singapore RTT:** ~30–60 ms typical (excellent for Lambda calls)
- **Karachi → US-East (Vercel default):** ~250 ms typical (Vercel CDN-edges so user-facing latency is fine)
- **Backend latency budget:** Most of the 15s query budget goes to LLM streaming + reranker; geographic factor is negligible at this scale

### 15.5 Working Hours / Time Zone

- PKT is **UTC+5**. Singapore is **UTC+8** (3 hours ahead).
- AWS billing aggregations run on UTC midnight → expect daily billing data refresh around **5 AM PKT**.

---

## 16. Working Style & Conventions

### 16.1 Communication

- **Language:** Hinglish/Roman Urdu acceptable in design discussion and informal docs; **English only** in code comments, committed code, and committed specs.
- **Mentorship on errors:** Root cause first, fix second. Pattern recognition over one-off fixes.
- **Honest assessment over reassurance:** If a decision has tradeoffs, state them. Don't hide caveats to seem confident.

### 16.2 Code Style

- **No legacy syntax.** Latest LangGraph, pgvector, PyTorch, FastAPI conventions. No deprecated APIs.
- **Annotated code:** Inline comments on non-obvious logic + post-code "why" explanations for clever or critical sections.
- **Type hints everywhere.** Pydantic for boundaries, TypedDict for LangGraph state, native types elsewhere. Mypy in strict mode.
- **Async-first.** asyncpg, httpx (async), FastAPI async endpoints. Avoid sync DB calls in async contexts.

### 16.3 Versioning

- **Semantic versioning** for the design doc. Locked decisions changing → version bump (v8 → v9) with rationale.
- **No sneaky modifications.** Locked decisions in §3 cannot be changed without a documented version bump.
- **Specs are source of truth.** Code follows specs. Disagreement → update spec first, then code.

### 16.4 Git Hygiene

- **Conventional commits:** `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `ci:`
- **Pre-commit hooks** must pass before commit (ruff format + lint, secret scan, large file check)
- **Squash on merge** for PRs (cleaner history)
- **Branch protection** on `main` once Phase 2 starts

---

## 17. Glossary

| Term | Definition |
|---|---|
| **CIK** | Central Index Key — SEC's unique identifier for filers |
| **CU-hour** | Compute Unit hour — Neon's billing metric. 1 CU = 1 vCPU + 4 GB RAM |
| **HNSW** | Hierarchical Navigable Small World — graph-based ANN index |
| **HyDE** | Hypothetical Document Embeddings — RAG technique generating hypothetical answers, then retrieving similar real chunks (v3+) |
| **KG-Augmented RAG** | RAG variant that augments vector search with explicit entity relationship traversal via a knowledge graph (v2) |
| **MD&A** | Management's Discussion and Analysis — narrative section of 10-K/10-Q |
| **MRL** | Matryoshka Representation Learning — embedding technique where shorter prefixes of a vector remain semantically meaningful, enabling flexible-dim truncation downstream |
| **RRF** | Reciprocal Rank Fusion — combining rankings from multiple retrieval methods |
| **SBP** | State Bank of Pakistan — central bank, sets cross-border transaction policy |
| **Single-Pass Agentic RAG** | RAG with LLM-driven planning and self-evaluation, but no retry loop. Evaluate annotates confidence without triggering re-retrieval |
| **SigV4** | AWS Signature Version 4 — request signing protocol for IAM-authenticated API calls (superseded by Vercel OIDC in this project) |
| **SSE** | Server-Sent Events — HTTP-based one-way streaming protocol |
| **tsvector** | Postgres's tokenized text representation, enables full-text search |
| **ts_rank_cd** | Cover-density ranking function in Postgres FTS (NOT BM25) |
| **Vercel OIDC** | OpenID Connect tokens issued by Vercel to authenticate Lambda calls without SigV4 signing |
| **XBRL** | eXtensible Business Reporting Language — structured financial data format used in SEC filings (v2) |

---

## Appendix A — Quick Resume Snippet

For pasting into a new chat to instantly resume context:

```
AlphaLens v8 — RAG agent over SEC 10-K/10-Q filings (top 10 S&P, 2022-2026, ~22k chunks).

Stack (locked): AWS Lambda Container (FastAPI + LangGraph 5-node, single container)
→ Neon Postgres (pgvector HNSW VECTOR(768) + tsvector GIN) + Cloudflare R2
+ Groq LLaMA 3.3 70B + Jina v3 (truncate_dim=768) + nomic-embed-text-v1.5 fallback
+ ms-marco-MiniLM-L-6-v2 reranker (in-process). Frontend: Next.js 15 on Vercel via SSE.
Region ap-southeast-1. Lambda URL auth=NONE + Vercel OIDC middleware.

RAG type: Single-Pass Agentic RAG (v1).
Graph: Plan → Retrieve+Filter → Rerank → Evaluate → Synthesize.
Retry loop deferred to v3. KG-Augmented RAG (Apache AGE) deferred to v2.
XBRL parser deferred to v2.

Version roadmap: v1 (now, working pipeline) → v2 (KG + XBRL, week 3-4)
→ v3 (fully agentic retry loop, week 5-6).

Constraints: AWS $100 free-tier credits (6mo expiry).
ECR 12-mo cliff (~$0.20-0.25/mo after). HBL debit card, intl txns enabled.
Jina free tier ~1M tokens (~2 month runway); nomic fallback auto-activates on exhaustion.

Status: Phase 1 setup ~10% complete (AWS account + MFA + credits + v8 doc).
Pending: Neon/R2/Groq/Jina/Vercel/Sentry/Opik signups, billing guardrails,
IAM users, repo bootstrap, smoke test (Phase1_Setup_Guide.md).
Phase 2 (17 specs, lightweight format) NOT started.

Next step: Phase 1 Setup Guide Parts 1-7, then author + implement 17 specs.
Spec format: Goal + Function Signatures + Acceptance Criteria + Gotchas (~1 page each).

Locked decisions: v8 §3 (L1-L21). Open decisions: §11.
Master doc: docs/design/AlphaLens_v8.md.
```

---

**End of v8.**

This document is the authoritative reference. v7 and earlier are deprecated; refer to v8 for any architectural, operational, status, or cost question.
