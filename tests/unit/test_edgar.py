"""Unit tests for src/alphalens/etl/edgar.py.

Coverage:
  - AC #1  : sec_edgar_user_agent validator (valid + invalid formats)
  - AC #9  : fetch_primary_doc idempotency counter (mocked I/O)
  - AC #11 : live SEC integration (skipped by default — set RUN_INTEGRATION=1)
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from alphalens.config import Settings
from alphalens.etl.edgar import EdgarClient, FilingMetadata, _is_retryable

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
    # C4 (#13): the cache-miss PUT must set ContentType so R2 serves text/html.
    assert r2_mock.put_object.call_args.kwargs.get("ContentType") == "text/html"


# ── C2 (#6): retry predicate — transient + 429/5xx retried; 401/402/404 not ──


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.test")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"status {code}", request=req, response=resp)


def test_is_retryable_transient_network_errors() -> None:
    """Transient network errors (incl. all timeout subtypes via TimeoutException) → retry."""
    assert _is_retryable(httpx.ConnectError("refused"))
    assert _is_retryable(httpx.ReadTimeout("slow"))  # subclass of httpx.TimeoutException
    assert _is_retryable(httpx.WriteTimeout("slow"))  # subclass of httpx.TimeoutException
    assert _is_retryable(httpx.PoolTimeout("slow"))  # subclass of httpx.TimeoutException
    assert _is_retryable(httpx.RemoteProtocolError("proto"))


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_is_retryable_status_retried(code: int) -> None:
    assert _is_retryable(_status_error(code))


@pytest.mark.parametrize("code", [401, 402, 404, 400, 403])
def test_is_retryable_status_not_retried(code: int) -> None:
    assert not _is_retryable(_status_error(code))


def test_is_retryable_ignores_unrelated_exception() -> None:
    assert not _is_retryable(ValueError("not network"))


# ── C3 (#7) + S-fix logging: overflow reads are INFO, unusable bounds are WARNING ──


def _overflow_client(mock_settings: MagicMock, page: dict[str, Any]) -> EdgarClient:
    """An EdgarClient whose submissions payload carries exactly one overflow *page*."""
    empty_block: dict[str, list[str]] = {
        "form": [],
        "filingDate": [],
        "reportDate": [],
        "accessionNumber": [],
        "primaryDocument": [],
    }
    payload: dict[str, Any] = {"filings": {"recent": empty_block, "files": [page]}}
    resp: MagicMock = MagicMock()
    # Every GET (submissions + the overflow page itself) returns this payload; the page
    # body parses to zero rows, which is all these logging assertions need.
    resp.json = MagicMock(return_value=payload)
    resp.raise_for_status = MagicMock()
    http_mock: MagicMock = MagicMock(spec=httpx.AsyncClient)
    http_mock.get = AsyncMock(return_value=resp)
    http_mock.aclose = AsyncMock()
    return EdgarClient(mock_settings, http_client=http_mock)


@pytest.mark.asyncio
async def test_list_filings_logs_normal_overflow_at_info(
    mock_settings: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Overflow with usable bounds is the expected path since the S-fix → INFO, not WARNING."""
    client = _overflow_client(
        mock_settings,
        {
            "name": "CIK0000320193-submissions-001.json",
            "filingFrom": "2022-03-01",
            "filingTo": "2023-04-01",
        },
    )

    with caplog.at_level(logging.INFO, logger="alphalens.etl.edgar"):
        result = await client.list_filings("0000320193")

    assert result == []
    info_text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "overflow" in info_text.lower()
    assert "0000320193" in caplog.text
    # The anomaly channel stays silent on the happy path.
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_list_filings_warns_on_unusable_page_bounds(
    mock_settings: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing/unparseable filingFrom-filingTo → fetch-on-unknown, and that IS a WARNING."""
    client = _overflow_client(mock_settings, {"name": "CIK0000320193-submissions-001.json"})

    with caplog.at_level(logging.INFO, logger="alphalens.etl.edgar"):
        result = await client.list_filings("0000320193")

    assert result == []
    warn_text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "CIK0000320193-submissions-001.json" in warn_text
    assert "0000320193" in warn_text


@pytest.mark.asyncio
async def test_list_filings_no_warning_without_overflow(
    mock_settings: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """C3: no overflow key → no warning (current in-scope CIK behaviour unchanged)."""
    payload: dict[str, Any] = {
        "filings": {
            "recent": {
                "form": [],
                "filingDate": [],
                "reportDate": [],
                "accessionNumber": [],
                "primaryDocument": [],
            }
        }
    }
    resp: MagicMock = MagicMock()
    resp.json = MagicMock(return_value=payload)
    resp.raise_for_status = MagicMock()
    http_mock: MagicMock = MagicMock(spec=httpx.AsyncClient)
    http_mock.get = AsyncMock(return_value=resp)
    http_mock.aclose = AsyncMock()

    client = EdgarClient(mock_settings, http_client=http_mock)
    with caplog.at_level(logging.WARNING, logger="alphalens.etl.edgar"):
        result = await client.list_filings("0000320193")

    assert result == []
    assert "overflow" not in caplog.text.lower()


# ── S-fix AC #8: _page_overlaps (overlap / no-overlap / boundary-touch) ───────

_WINDOW_FROM = date(2022, 1, 1)
_WINDOW_TO = date(2026, 12, 31)


def _page(frm: str, to: str) -> dict[str, Any]:
    return {"name": "CIK0000019617-submissions-001.json", "filingFrom": frm, "filingTo": to}


@pytest.mark.parametrize(
    ("frm", "to", "expected", "why"),
    [
        # -- overlapping --
        ("2023-01-01", "2023-06-30", True, "fully inside the window"),
        ("2020-01-01", "2027-12-31", True, "strictly contains the window"),
        ("2021-06-01", "2022-06-01", True, "straddles the lower bound"),
        ("2026-06-01", "2027-06-01", True, "straddles the upper bound"),
        # -- boundary touch (inclusive on both ends) --
        ("2020-01-01", "2022-01-01", True, "filingTo == date_from"),
        ("2026-12-31", "2027-06-01", True, "filingFrom == date_to"),
        ("2022-01-01", "2026-12-31", True, "exactly the window"),
        # -- no overlap (one day clear of each boundary) --
        ("2020-01-01", "2021-12-31", False, "ends the day before date_from"),
        ("2027-01-01", "2027-12-31", False, "starts the day after date_to"),
    ],
)
def test_page_overlaps(frm: str, to: str, expected: bool, why: str) -> None:
    """AC #8: overlap rule is filingTo >= date_from AND filingFrom <= date_to."""
    assert EdgarClient._page_overlaps(_page(frm, to), _WINDOW_FROM, _WINDOW_TO) is expected, why


@pytest.mark.parametrize(
    "page",
    [
        {"name": "x.json"},  # no bounds at all
        {"name": "x.json", "filingFrom": "2020-01-01"},  # half bounds
        {"name": "x.json", "filingFrom": "", "filingTo": ""},  # empty strings
        {"name": "x.json", "filingFrom": "not-a-date", "filingTo": "2021-01-01"},  # unparseable
    ],
)
def test_page_overlaps_fetches_when_bounds_unusable(page: dict[str, Any]) -> None:
    """Unknown/broken bounds must fetch, never silently skip — that's the bug being fixed."""
    assert EdgarClient._page_overlaps(page, _WINDOW_FROM, _WINDOW_TO) is True


# ── S-fix AC #1-#5: overflow pages are read, filtered, merged, deduped ─────────


def _block(rows: list[tuple[str, str, str, str, str]]) -> dict[str, list[str]]:
    """Build a flat parallel-array block: (form, filingDate, reportDate, accession, doc)."""
    return {
        "form": [r[0] for r in rows],
        "filingDate": [r[1] for r in rows],
        "reportDate": [r[2] for r in rows],
        "accessionNumber": [r[3] for r in rows],
        "primaryDocument": [r[4] for r in rows],
    }


@pytest.mark.asyncio
async def test_list_filings_reads_only_overlapping_overflow_pages(
    mock_settings: MagicMock,
) -> None:
    """AC #1-#5: overlapping page fetched + parsed + merged + deduped; stale page never GET."""
    root: dict[str, Any] = {
        "filings": {
            "recent": _block(
                [("10-K", "2025-02-13", "2024-12-31", "0000019617-25-000123", "jpm-2024.htm")]
            ),
            "files": [
                # overlaps -> must be fetched
                {
                    "name": "page-in.json",
                    "filingFrom": "2022-01-05",
                    "filingTo": "2023-06-30",
                },
                # entirely before the window -> must NOT be fetched
                {
                    "name": "page-out.json",
                    "filingFrom": "2019-01-01",
                    "filingTo": "2021-12-31",
                },
            ],
        }
    }
    page_in: dict[str, list[str]] = _block(
        [
            # duplicate of the recent row -> must dedup to one
            ("10-K", "2025-02-13", "2024-12-31", "0000019617-25-000123", "jpm-2024.htm"),
            # new, in-window -> must appear
            ("10-K", "2023-02-21", "2022-12-31", "0000019617-23-000456", "jpm-2022.htm"),
            # right form, out of window -> filtered
            ("10-K", "2021-02-23", "2020-12-31", "0000019617-21-000789", "jpm-2020.htm"),
            # in window, wrong form -> filtered
            ("424B2", "2023-03-01", "", "0001213900-23-000111", "note.htm"),
        ]
    )

    requested: list[str] = []

    async def fake_get(url: str, **kwargs: Any) -> MagicMock:
        requested.append(url)
        resp: MagicMock = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=page_in if "page-in.json" in url else root)
        return resp

    http_mock: MagicMock = MagicMock(spec=httpx.AsyncClient)
    http_mock.get = AsyncMock(side_effect=fake_get)
    http_mock.aclose = AsyncMock()

    client = EdgarClient(mock_settings, http_client=http_mock)
    result = await client.list_filings("0000019617")

    assert any("page-in.json" in u for u in requested), "overlapping page must be fetched"
    assert not any("page-out.json" in u for u in requested), "stale page must not be fetched"
    assert len(requested) == 2, f"expected root + 1 overlapping page, got {requested}"

    accessions = [f.accession_number for f in result]
    assert accessions == ["0000019617-25-000123", "0000019617-23-000456"]
    assert len(set(accessions)) == len(accessions), "AC #5: dedup on accession_number"
    # AC #4: the same form + date filters applied to the overflow rows.
    assert all(f.form_type in {"10-K", "10-Q"} for f in result)
    assert all(date(2022, 1, 1) <= f.filing_date <= date(2026, 12, 31) for f in result)
    # Overflow rows are fully-formed FilingMetadata, not a degraded shape.
    recovered = result[1]
    assert recovered.period_of_report == date(2022, 12, 31)
    assert recovered.primary_doc_filename == "jpm-2022.htm"
    assert "000001961723000456" in recovered.primary_doc_url


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
