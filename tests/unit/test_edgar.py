"""Unit tests for src/alphalens/etl/edgar.py.

Coverage:
  - AC #1  : sec_edgar_user_agent validator (valid + invalid formats)
  - AC #9  : fetch_primary_doc idempotency counter (mocked I/O)
  - AC #11 : live SEC integration (skipped by default — set RUN_INTEGRATION=1)
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from alphalens.config import Settings
from alphalens.etl.edgar import EdgarClient, FilingMetadata

# ── AC #1: User-Agent field validator ─────────────────────────────────────────


def test_ua_validator_rejects_invalid() -> None:
    """AC #1: _validate_sec_ua raises ValueError for UA without email."""
    with pytest.raises(ValueError):
        Settings._validate_sec_ua("no-email-here")  # type: ignore[misc]


def test_ua_validator_accepts_valid() -> None:
    """AC #1: _validate_sec_ua accepts 'Name email@domain' format."""
    result: str = Settings._validate_sec_ua(  # type: ignore[misc]
        "AlphaLens admin@example.com"
    )
    assert result == "AlphaLens admin@example.com"


# ── Shared fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_settings() -> MagicMock:
    s: MagicMock = MagicMock()  # no spec — pydantic attrs live on instances, not class
    s.sec_edgar_user_agent = "TestBot test@test.com"
    s.r2_bucket_name = "test-bucket"
    s.r2_endpoint_url = "https://test.r2.cloudflarestorage.com"
    s.r2_access_key_id.get_secret_value.return_value = "testkey"
    s.r2_secret_access_key.get_secret_value.return_value = "testsecret"
    return s


@pytest.fixture
def sample_meta() -> FilingMetadata:
    return FilingMetadata(
        cik="0000320193",
        accession_number="0000320193-24-000123",
        form_type="10-K",
        filing_date=date(2024, 11, 1),
        period_of_report=date(2024, 9, 28),
        primary_doc_url=(
            "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"
        ),
        primary_doc_filename="aapl-20240928.htm",
    )


# ── AC #9: fetch_primary_doc idempotency counter ──────────────────────────────


@pytest.mark.asyncio
async def test_fetch_primary_doc_idempotency(
    mock_settings: MagicMock,
    sample_meta: FilingMetadata,
) -> None:
    """AC #9: 1 SEC GET + 1 R2 PUT + 2 R2 HEADs + 1 R2 GET across two fetches."""
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    payload = b"<html>filing</html>"

    # R2 mock: HEAD raises 404 on first call, succeeds on second
    r2_mock: MagicMock = MagicMock()
    r2_mock.head_object = AsyncMock(
        side_effect=[
            ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"),
            None,
        ]
    )
    r2_mock.put_object = AsyncMock(return_value=None)
    body_mock: AsyncMock = AsyncMock()
    body_mock.read = AsyncMock(return_value=payload)
    r2_mock.get_object = AsyncMock(return_value={"Body": body_mock})

    # Wrap r2_mock in a context manager
    r2_ctx: MagicMock = MagicMock()
    r2_ctx.__aenter__ = AsyncMock(return_value=r2_mock)
    r2_ctx.__aexit__ = AsyncMock(return_value=None)

    session_mock: MagicMock = MagicMock()
    session_mock.client = MagicMock(return_value=r2_ctx)

    # httpx mock — raises_for_status is a no-op (200)
    http_response: MagicMock = MagicMock()
    http_response.content = payload
    http_response.raise_for_status = MagicMock()
    http_mock: MagicMock = MagicMock(spec=httpx.AsyncClient)
    http_mock.get = AsyncMock(return_value=http_response)
    http_mock.aclose = AsyncMock()

    with patch("aioboto3.Session", return_value=session_mock):
        client = EdgarClient(mock_settings, http_client=http_mock)
        async with client:
            result1 = await client.fetch_primary_doc(sample_meta)
            result2 = await client.fetch_primary_doc(sample_meta)

    assert http_mock.get.call_count == 1, "expected exactly 1 SEC GET"
    assert r2_mock.head_object.call_count == 2, "expected exactly 2 R2 HEADs"
    assert r2_mock.put_object.call_count == 1, "expected exactly 1 R2 PUT"
    assert r2_mock.get_object.call_count == 1, "expected exactly 1 R2 GET"
    assert result1 == payload
    assert result2 == payload


# ── AC #11: Live SEC integration (skip by default) ────────────────────────────

_skip_integration = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="set RUN_INTEGRATION=1 to run live integration tests",
)


@_skip_integration
@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_list_filings() -> None:
    """AC #11: Live SEC for Apple (CIK 0000320193) — expects ≥10 filings 2022–2026."""
    from alphalens.config import get_settings

    settings = get_settings()
    async with EdgarClient(settings) as client:
        filings = await client.list_filings("0000320193")

    assert len(filings) >= 10
    for f in filings:
        assert f.form_type in {"10-K", "10-Q"}
        assert f.cik == "0000320193"
        assert f.accession_nodash == f.accession_number.replace("-", "")
