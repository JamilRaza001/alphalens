# Spec S4 — Settings (Configuration)

**Maps to:** v8 §13.3 spec 01 → `src/alphalens/config.py`
**Status:** authored, not implemented

---

## Goal

Single source of typed, validated runtime configuration for AlphaLens. Loads environment variables from `.env` (local dev) or process env (Lambda), validates types and required fields **at import time**, and exposes a cached `get_settings()` singleton consumed by every other module. Fail-fast philosophy: a missing `NEON_DATABASE_URL` or malformed `JINA_DIMENSIONS` crashes the process at startup, never at query time. This module has zero runtime dependencies on other AlphaLens code — everything else depends on it.

---

## Function Signatures

```python
# src/alphalens/config.py
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime config. Loaded once at import via get_settings()."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database (L12: pooled for app, direct for migrations) ──────
    neon_database_url: SecretStr = Field(..., description="Pooled URL for asyncpg (app)")
    neon_direct_url:   SecretStr = Field(..., description="Direct URL for Alembic")

    # ── Cloudflare R2 ──────────────────────────────────────────────
    r2_account_id:         str
    r2_access_key_id:      SecretStr
    r2_secret_access_key:  SecretStr
    r2_bucket_name:        str = "alphalens-filings"
    r2_endpoint_url:       str

    # ── LLM (Groq) ─────────────────────────────────────────────────
    groq_api_key: SecretStr
    groq_model:   str = "llama-3.3-70b-versatile"

    # ── Embeddings (L17: 768d locked, L18: Jina + nomic fallback) ──
    jina_api_key:          SecretStr
    jina_model:            str = "jina-embeddings-v3"
    jina_dimensions:       int = 768
    jina_free_tier_tokens: int = 1_000_000
    jina_tpm_limit:        int = 90_000        # Spec 06a: token-bucket refill rate (tok/min)
    jina_max_request_tokens: int = 6_000       # Spec 06a: per-request summed-token cap (Jina path)
    # model_validator (Spec 06a): jina_tpm_limit + jina_max_request_tokens <= 100_000
    nomic_model:           str = "nomic-ai/nomic-embed-text-v1.5"

    # ── Reranker (in-process, L14) ─────────────────────────────────
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # ── AWS (None inside Lambda — uses IAM role instead) ───────────
    aws_access_key_id:     SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_default_region:    str = "ap-southeast-1"
    lambda_role_arn:       str | None = None
    ecr_registry:          str | None = None
    ecr_repo_name:         str = "alphalens-agent"

    # ── Auth — Vercel OIDC (L11) ───────────────────────────────────
    vercel_oidc_issuer:   str = "https://oidc.vercel.com"
    vercel_oidc_jwks_url: str | None = None  # required when is_lambda=True
    vercel_oidc_audience: str | None = None  # required when is_lambda=True

    # ── Observability (all optional — degrade gracefully) ──────────
    sentry_dsn:     SecretStr | None = None
    opik_api_key:   SecretStr | None = None
    opik_workspace: str = "alphalens"

    # ── App ────────────────────────────────────────────────────────
    environment: Literal["development", "production"] = "development"
    log_level:   Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("jina_dimensions")
    @classmethod
    def _lock_jina_dimensions(cls, v: int) -> int:
        """Enforce L17: jina_dimensions is locked at 768."""
        ...

    @model_validator(mode="after")
    def _require_oidc_in_lambda(self) -> "Settings":
        """If running inside Lambda, vercel_oidc_jwks_url and vercel_oidc_audience must be set (L11)."""
        ...

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_lambda(self) -> bool:
        """True iff running inside AWS Lambda (checks AWS_LAMBDA_FUNCTION_NAME env)."""
        ...


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor. First call validates env; subsequent calls return cached instance."""
    ...
```

---

## Acceptance Criteria

1. `from alphalens.config import get_settings` succeeds with a valid `.env` present in the working directory.
2. Calling `get_settings()` twice returns the **same object identity** (`get_settings() is get_settings()` is `True`).
3. Missing any required field (e.g. `NEON_DATABASE_URL` unset) raises `pydantic.ValidationError` at the **first** `get_settings()` call, not at later attribute access.
4. Setting `JINA_DIMENSIONS=1024` in env raises `ValidationError` from `_lock_jina_dimensions` (L17 enforced — 768 only).
5. `get_settings().neon_database_url.get_secret_value()` returns the pooled URL string; `str(get_settings())` and `repr(get_settings())` do **not** leak any `SecretStr` raw value (pydantic shows `SecretStr('**********')`).
6. `ENVIRONMENT=staging` raises `ValidationError` (Literal restricts to `development|production` only). Same for invalid `LOG_LEVEL`.
7. With `AWS_LAMBDA_FUNCTION_NAME` env var present, `settings.is_lambda` is `True`; absent, it is `False`.
8. With no `.env` file but all required vars in process env (Lambda runtime), settings load correctly — `.env` is optional.
9. Validation errors print field name + reason (pydantic default behavior), so CloudWatch logs identify the misconfigured key.
10. With `AWS_LAMBDA_FUNCTION_NAME` set (simulating Lambda) AND `vercel_oidc_jwks_url` / `vercel_oidc_audience` unset, `get_settings()` raises `ValidationError` (L11 — OIDC config required inside Lambda). Locally (no Lambda env var), they remain optional with default `None`.
11. Smoke test passes:
    ```bash
    uv run python -c "from alphalens.config import get_settings; s = get_settings(); print(s.r2_bucket_name, s.jina_dimensions, s.groq_model)"
    # → alphalens-filings 768 llama-3.3-70b-versatile
    ```

---

## Gotchas

- **Pooled vs Direct URL (L12):** App code (asyncpg pool, agent queries, ETL upserts) **must** use `neon_database_url` (pooled, PgBouncer transaction-mode). Alembic migrations and any session-level Postgres feature (advisory locks, `SET LOCAL`, session-scoped prepared statements, `LISTEN/NOTIFY`) **must** use `neon_direct_url`. Wrong choice → silent migration corruption or "feature not supported in transaction pooling mode" errors.
- **SecretStr serialization leak:** pydantic redacts `SecretStr` in `repr()`/`str()`, but **not** in `model_dump()` with `mode="json"` and certain serializers. Never log `settings.model_dump()`. Always unwrap with `.get_secret_value()` at the exact call site (e.g. `httpx.AsyncClient(headers={"Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}"})`) and never assign the unwrapped value to a variable. Sentry/Opik integrations in S16 must scrub these fields.
- **Lambda has no `.env`:** `env_file=".env"` is safe — pydantic-settings silently skips if absent. Lambda environment vars are injected by `infra/deploy.sh` (spec S14) via `--environment Variables={...}`. `aws_access_key_id` / `aws_secret_access_key` stay `None` inside Lambda (boto3 picks up credentials from the attached IAM role automatically). `vercel_oidc_jwks_url` and `vercel_oidc_audience` **must** be in that injection — the `model_validator` will refuse to construct Settings otherwise, crashing Lambda cold-start with a clear error. This is preferred over a silent token-verification bypass at request time.
