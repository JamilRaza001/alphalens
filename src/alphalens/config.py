"""AlphaLens v8 — Runtime configuration.

Single source of truth for all environment-driven settings.
Validated at import time via pydantic-settings v2; fails fast on missing/invalid fields.
Use :func:`get_settings` everywhere — never instantiate :class:`Settings` directly.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated runtime configuration loaded from env vars / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,  # NEON_DATABASE_URL == neon_database_url
        extra="ignore",  # tolerate arbitrary shell env vars
    )

    # ── Database (L12) ────────────────────────────────────────────────────────
    # Pooled URL for the app (asyncpg); direct URL for Alembic migrations.
    neon_database_url: SecretStr
    neon_direct_url: SecretStr

    # ── Cloudflare R2 ─────────────────────────────────────────────────────────
    r2_account_id: str
    r2_access_key_id: SecretStr
    r2_secret_access_key: SecretStr
    r2_bucket_name: str = "alphalens-filings"
    r2_endpoint_url: str

    # ── SEC EDGAR ─────────────────────────────────────────────────────────────
    # Required format: "Your Name email@domain" — enforced by SEC; generic UAs trigger IP bans.
    sec_edgar_user_agent: str

    # ── LLM — Groq (L8) ───────────────────────────────────────────────────────
    groq_api_key: SecretStr
    groq_model: str = "llama-3.3-70b-versatile"

    # ── Embeddings — Jina primary (L17, L18) ──────────────────────────────────
    jina_api_key: SecretStr
    jina_model: str = "jina-embeddings-v3"
    jina_dimensions: int = 768  # locked at 768 — see _lock_jina_dimensions
    jina_free_tier_tokens: int = 10_000_000
    jina_tpm_limit: int = 90_000  # 90% of Jina's 100K TPM hard cap; env: JINA_TPM_LIMIT
    jina_max_request_tokens: int = (
        6_000  # max summed tokens per Jina request; env: JINA_MAX_REQUEST_TOKENS
    )

    # ── Embeddings — nomic fallback (L18) ─────────────────────────────────────
    nomic_model: str = "nomic-ai/nomic-embed-text-v1.5"
    embedding_batch_size: int = 128  # texts per Jina/nomic API call
    chunk_max_tokens: int = 512  # hard ceiling per chunk; far below Jina's 8194 limit

    # ── Reranker (L3, L14) ────────────────────────────────────────────────────
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # ── AWS ───────────────────────────────────────────────────────────────────
    # In Lambda, credentials come from the IAM execution role — these are None.
    # In local dev / CI, set them explicitly for deployer access.
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_default_region: str = "ap-southeast-1"  # L13
    lambda_role_arn: str | None = None
    ecr_registry: str | None = None
    ecr_repo_name: str = "alphalens-agent"

    # ── Auth — Vercel OIDC (L11) ──────────────────────────────────────────────
    # Public values — NOT SecretStr.
    # jwks_url and audience are optional locally; REQUIRED when is_lambda=True.
    vercel_oidc_issuer: str = "https://oidc.vercel.com"
    vercel_oidc_jwks_url: str | None = None
    vercel_oidc_audience: str | None = None

    # ── Observability ─────────────────────────────────────────────────────────
    sentry_dsn: SecretStr | None = None
    opik_api_key: SecretStr | None = None
    opik_workspace: str | None = None

    # ── App ───────────────────────────────────────────────────────────────────
    environment: Literal["development", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ── Computed fields ───────────────────────────────────────────────────────

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_lambda(self) -> bool:
        """True iff this process is running inside AWS Lambda."""
        return os.environ.get("AWS_LAMBDA_FUNCTION_NAME") is not None

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("sec_edgar_user_agent")
    @classmethod
    def _validate_sec_ua(cls, v: str) -> str:
        if not re.fullmatch(r"^[\w .'-]+ \S+@\S+\.\S+$", v):
            raise ValueError(
                "sec_edgar_user_agent must be 'Your Name email@domain' "
                f"(e.g. 'AlphaLens admin@alphalens.ai'); got {v!r}"
            )
        return v

    @field_validator("jina_dimensions")
    @classmethod
    def _lock_jina_dimensions(cls, v: int) -> int:
        """Enforce L17: VECTOR(768) everywhere — no other dimension accepted."""
        if v != 768:
            raise ValueError(
                f"jina_dimensions must be 768 (got {v}); "
                "pgvector schema is VECTOR(768) — see locked decision L17"
            )
        return v

    @model_validator(mode="after")
    def _validate_tpm_safety_invariant(self) -> Settings:
        """Enforce AC#15: worst-case window = jina_tpm_limit + one request <= 100_000."""
        if self.jina_tpm_limit + self.jina_max_request_tokens > 100_000:
            raise ValueError(
                f"jina_tpm_limit ({self.jina_tpm_limit}) + jina_max_request_tokens "
                f"({self.jina_max_request_tokens}) must be <= 100_000 "
                "(Jina rolling window safety invariant)"
            )
        return self

    @model_validator(mode="after")
    def _require_oidc_in_lambda(self) -> Settings:
        """Enforce L11: Vercel OIDC config is mandatory when running in Lambda."""
        if self.is_lambda and (
            self.vercel_oidc_jwks_url is None or self.vercel_oidc_audience is None
        ):
            raise ValueError(
                "VERCEL_OIDC_JWKS_URL and VERCEL_OIDC_AUDIENCE are required "
                "when running in AWS Lambda (is_lambda=True); see locked decision L11"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton.

    Validates all environment variables on the first call; subsequent calls
    return the same object.  Use this function everywhere — never call
    ``Settings()`` directly.
    """
    return Settings()  # type: ignore[call-arg]
