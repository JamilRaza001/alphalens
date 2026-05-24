# AlphaLens

**RAG agent over SEC 10-K/10-Q filings — near-zero-cost serverless pipeline**

![Status](https://img.shields.io/badge/status-Phase%201%20setup%20in%20progress-yellow)

---

## What It Does

AlphaLens lets investors and analysts query SEC filings using natural language. Ask *"How did Apple's services revenue evolve from 2022–2026?"* — get a cited, multi-document answer in ~15 seconds.

Coverage: top 10 S&P 500 companies, 10-K + 10-Q filings, 2022–2026.

---

## Stack

AWS Lambda (FastAPI + LangGraph 5-node) → Neon Postgres (pgvector HNSW `VECTOR(768)` + tsvector) + Cloudflare R2 + Groq LLaMA 3.3 70B + Jina v3 768d embeddings + ms-marco-MiniLM reranker. Frontend: Next.js 15 on Vercel via SSE.

---

## Setup

See [docs/setup/Phase1_Setup_Guide.md](docs/setup/Phase1_Setup_Guide.md) for the full Phase 1 bootstrap walkthrough (environment, services, IAM, repo, schema, smoke test).

Quick start:

```bash
# Install dependencies (requires Python 3.12 + uv)
uv sync --extra dev

# Install pre-commit hooks
uv run pre-commit install

# Copy env template and fill in your credentials
cp .env.example .env
# Edit .env with real values

# Run verification
bash scripts/verify_s1.sh
```

---

## Architecture

See [docs/design/AlphaLens_v8.md](docs/design/AlphaLens_v8.md) for full architecture, locked decisions, cost analysis, and version roadmap.

---

## License

MIT
