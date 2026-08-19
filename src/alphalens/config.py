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

from pydantic import (
    SecretStr,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
)
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

    # ── Agent DB pool (S16) ───────────────────────────────────────────────────
    # The agent asyncpg pool endpoint. Defaults to the POOLED URL (neon_database_url) —
    # mirroring the ETL runner, which runs prepared-statements-ON + init=register_pgvector on
    # the pooled endpoint live (S16/D-endpoint). Env AGENT_DB_URL overrides for a scale-flip
    # (e.g. to the direct endpoint) with zero code change. See _default_agent_db_url below.
    agent_db_url: SecretStr | None = None
    agent_pool_min_size: int = 1
    agent_pool_max_size: int = 5
    # Bounded waits on the agent pool. asyncpg leaves TWO of the three waits unbounded by
    # default (acquire = None = forever; command_timeout = None = forever), so a stall is
    # silent rather than diagnostic. Every agent-path call site passes its own acquire budget —
    # Pool.__init__ has no timeout slot, so there is nothing pool-level to inherit.
    agent_pool_connect_timeout_seconds: float = 30.0  # TCP/TLS/auth + init=; absorbs a cold resume
    agent_pool_acquire_timeout_seconds: float = 30.0  # wait for a free connection (fan-out)
    # ~50x measured steady state (~0.21 s/cell) and ~9x the worst observed first query (~1.08 s).
    # Also applies pool-wide (create_pool), so it must clear cold-start load_ticker_universe and
    # the register_pgvector codec round-trips — do not tune below cold-start cost.
    agent_command_timeout_seconds: float = 10.0

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
    groq_model: str = "openai/gpt-oss-120b"
    # Synthesize-node temperature ONLY (S16/D-temp). Plan + Evaluate pin temperature=0
    # invariantly at their call sites (structured decoding depends on it, S13 lock) and ignore
    # this value. Default 0.0 → deterministic v1 synthesis; env GROQ_TEMPERATURE tunes it.
    groq_temperature: float = 0.0

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

    # ── Selection — cell-aware per-pair floor (S17) ───────────────────────────
    # Replaces the old global top-N slice (rerank_top_n). `floor_per_pair` guarantees each
    # non-empty (ticker, year) pair a minimum number of chunks so one ticker can no longer
    # occupy every slot; `max_context_chunks` is the hard budget of chunks handed to Synthesize.
    # Env-overridable: FLOOR_PER_PAIR / MAX_CONTEXT_CHUNKS.
    floor_per_pair: int = 2
    max_context_chunks: int = 8

    # ── Retrieval — per-cell hybrid fan-out (S15 D1) ──────────────────────────
    # All env-overridable: RETRIEVAL_K_VECTOR, RETRIEVAL_K_LEXICAL, RETRIEVAL_RRF_C, RETRIEVAL_N_PER_CELL.
    retrieval_k_vector: int = 20  # HNSW candidates fetched per cell (before fusion)
    retrieval_k_lexical: int = 20  # ts_rank_cd candidates fetched per cell (before fusion)
    retrieval_rrf_c: int = 60  # RRF smoothing constant in 1/(c + rank); standard, not tuned per-run
    retrieval_n_per_cell: int = 10  # RRF survivors kept per cell → merge

    # ── Corpus year bounds (Plan year-rail) ───────────────────────────────────
    # Hard gate on Plan's time_range.years: rejects implausible years — notably the observed
    # concatenation of "2023 vs 2024" into the single integer 20232024 — before they reach
    # retrieval, where an implausible year matches zero rows and degrades into a false
    # empty-coverage answer. Env: CORPUS_MIN_YEAR / CORPUS_MAX_YEAR — bump as new filings land.
    corpus_min_year: int = 2021
    corpus_max_year: int = 2026

    # ── Synthesis circuit breaker (L8, S14) ───────────────────────────────────
    breaker_failure_threshold: int = (
        3  # consecutive hard failures to trip; env: BREAKER_FAILURE_THRESHOLD (D2)
    )
    breaker_reset_timeout_seconds: float = (
        30.0  # OPEN cool-off before probe; env: BREAKER_RESET_TIMEOUT_SECONDS (D3)
    )

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

    @field_validator("floor_per_pair", "max_context_chunks")
    @classmethod
    def _validate_selection_positive(cls, v: int, info: ValidationInfo) -> int:
        """Both selection knobs must be >= 1 (S17): a floor/cap below 1 selects nothing."""
        if v < 1:
            raise ValueError(f"{info.field_name} must be >= 1 (got {v})")
        return v

    @model_validator(mode="after")
    def _validate_floor_within_cap(self) -> Settings:
        """S17: the per-pair floor cannot exceed the total context budget."""
        if self.floor_per_pair > self.max_context_chunks:
            raise ValueError(
                f"floor_per_pair ({self.floor_per_pair}) must be <= max_context_chunks "
                f"({self.max_context_chunks}); a floor above the cap is unsatisfiable"
            )
        return self

    @model_validator(mode="after")
    def _default_agent_db_url(self) -> Settings:
        """Default the agent pool endpoint to the POOLED URL when AGENT_DB_URL is unset (S16).

        Mirrors the ETL runner, which runs live on the pooled endpoint. Populated here so the
        resolved DSN is a single source of truth; env AGENT_DB_URL still overrides.
        """
        if self.agent_db_url is None:
            self.agent_db_url = self.neon_database_url
        return self

    @model_validator(mode="after")
    def _validate_corpus_year_bounds(self) -> Settings:
        """Enforce a non-empty corpus year window — an inverted range would drop every year."""
        if self.corpus_min_year > self.corpus_max_year:
            raise ValueError(
                f"corpus_min_year ({self.corpus_min_year}) must be <= corpus_max_year "
                f"({self.corpus_max_year}); an inverted range rejects every planned year"
            )
        return self

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
    return Settings()
