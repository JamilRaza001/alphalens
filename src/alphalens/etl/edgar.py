"""AlphaLens v8 — SEC EDGAR async client with R2 cache-through.

Pure I/O layer: lists 10-K/10-Q filings and fetches primary HTML documents.
No DB writes — those live in S9+.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from datetime import date
from types import TracebackType
from typing import TYPE_CHECKING, Any

import aioboto3
import httpx
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError
from pydantic import BaseModel, field_validator
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from alphalens.config import Settings

if TYPE_CHECKING:
    # Stub-only packages (dev deps) — never imported at runtime in the Lambda image.
    from aiobotocore.session import ClientCreatorContext
    from types_aiobotocore_s3.client import S3Client

_log = logging.getLogger(__name__)

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_OVERFLOW_URL = "https://data.sec.gov/submissions/{name}"
_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{doc}"

# Transient network errors worth retrying (no HTTP status to inspect). httpx.TimeoutException
# is the base for Connect/Read/Write/Pool timeouts — covered as a group.
_TRANSIENT: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
)
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    """Retry transient network errors and 429/5xx; other 4xx (401/402/404) propagate."""
    if isinstance(exc, _TRANSIENT):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in _RETRYABLE_STATUS


class FilingMetadata(BaseModel):
    """Single 10-K or 10-Q filing record returned by :meth:`EdgarClient.list_filings`."""

    cik: str
    accession_number: str
    form_type: str
    filing_date: date
    period_of_report: date
    primary_doc_url: str
    primary_doc_filename: str

    @field_validator("cik")
    @classmethod
    def _validate_cik(cls, v: str) -> str:
        if not re.fullmatch(r"\d{10}", v):
            raise ValueError(f"cik must be 10 zero-padded digits; got {v!r}")
        return v

    @field_validator("accession_number")
    @classmethod
    def _validate_accession(cls, v: str) -> str:
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", v):
            raise ValueError(
                r"accession_number must be dashed format \d{10}-\d{2}-\d{6}; " f"got {v!r}"
            )
        return v

    @property
    def accession_nodash(self) -> str:
        """Accession number with dashes removed — used in SEC URLs and R2 cache key."""
        return self.accession_number.replace("-", "")

    @property
    def r2_cache_key(self) -> str:
        """R2 object key for the upstream write-through cache: ``{cik}/{accession_nodash}.html``.

        Two distinct R2 keys exist by design (#12):
          - THIS key (``{cik}/{accession_nodash}.html``) is the EDGAR write-through cache,
            keyed on the dash-stripped accession to mirror SEC archive paths. It dedupes raw
            upstream fetches and is owned by :class:`EdgarClient`.
          - The canonical, DB-linked location is ``filings.r2_key``
            (``filings/{cik}/{accession_number}.html``, a generated column), written during
            ingestion by ``runner._upload_html_to_r2``.
        Both are retained: the cache short-circuits re-fetching from SEC, while ``r2_key`` is
        the stable address the rest of the system references.
        """
        return f"{self.cik}/{self.accession_nodash}.html"


class RateLimiter:
    """Token-bucket rate limiter — enforces ≤rate requests per `per` seconds.

    One shared instance per :class:`EdgarClient` covers both EDGAR hostnames
    (``data.sec.gov`` and ``www.sec.gov``) against the 10 req/s IP-level budget.
    """

    def __init__(self, rate: int = 10, per: float = 1.0) -> None:
        self._rate = rate
        self._per = per
        self._tokens: float = float(rate)
        self._last: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                if self._last == 0.0:
                    self._last = now
                elapsed = now - self._last
                self._tokens = min(
                    float(self._rate),
                    self._tokens + elapsed * self._rate / self._per,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) * self._per / self._rate
            await asyncio.sleep(wait)


class EdgarClient:
    """Async SEC EDGAR client with Cloudflare R2 cache-through for primary HTML docs."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._own_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        self._rate_limiter = RateLimiter()
        self._session = aioboto3.Session(
            aws_access_key_id=settings.r2_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),
        )
        self._r2_ctx: ClientCreatorContext[S3Client] | None = None  # aioboto3 ctx handle
        self._r2: S3Client | None = None  # entered S3 client

    async def __aenter__(self) -> EdgarClient:
        self._r2_ctx = self._session.client(
            "s3",
            endpoint_url=self._settings.r2_endpoint_url,
            region_name="auto",
            config=AioConfig(signature_version="s3v4"),
        )
        self._r2 = await self._r2_ctx.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release R2 client and (if owned) the httpx client."""
        if self._r2_ctx is not None:
            await self._r2_ctx.__aexit__(None, None, None)
            self._r2_ctx = None
            self._r2 = None
        if self._own_http:
            await self._http.aclose()

    @staticmethod
    def _page_bounds(page: dict[str, Any]) -> tuple[date, date] | None:
        """Parsed ``(filingFrom, filingTo)`` of an overflow page, or ``None`` if unusable.

        ``None`` means the page cannot be judged against the window — missing or
        unparseable bounds. Callers must treat that as "fetch anyway", never as a skip.
        """
        raw_from = page.get("filingFrom")
        raw_to = page.get("filingTo")
        if not raw_from or not raw_to:
            return None
        try:
            return date.fromisoformat(raw_from), date.fromisoformat(raw_to)
        except ValueError:
            return None

    @staticmethod
    def _page_overlaps(page: dict[str, Any], date_from: date, date_to: date) -> bool:
        """True if an overflow page's ``[filingFrom, filingTo]`` intersects the window.

        Overlap rule: ``page.filingTo >= date_from AND page.filingFrom <= date_to``
        (inclusive on both ends — a page that merely touches a boundary still overlaps).

        Pages carry their own date bounds, so non-overlapping pages are skipped without
        a GET. If the bounds are unusable (:meth:`_page_bounds` returns ``None``) we return
        ``True`` and fetch: the whole point of this path is to stop silently dropping
        filings, so an unknown bound must never cause a skip.
        """
        bounds = EdgarClient._page_bounds(page)
        if bounds is None:
            return True
        page_from, page_to = bounds
        return page_to >= date_from and page_from <= date_to

    @staticmethod
    def _parse_filing_block(
        block: dict[str, list[Any]],
        cik: str,
        *,
        form_set: set[str],
        date_from: date,
        date_to: date,
    ) -> list[FilingMetadata]:
        """Parse one flat block of parallel arrays into filtered :class:`FilingMetadata`.

        Shared by ``filings.recent`` and every fetched overflow page — both use the exact
        same flat shape (``form`` / ``filingDate`` / ``reportDate`` / ``accessionNumber`` /
        ``primaryDocument``), live-confirmed. Single home for the form + date filters so
        the two sources can never drift apart.
        """
        forms: list[str] = block.get("form", [])
        filing_dates: list[str] = block.get("filingDate", [])
        report_dates: list[str] = block.get("reportDate", [])
        accessions: list[str] = block.get("accessionNumber", [])
        primary_docs: list[str] = block.get("primaryDocument", [])

        cik_int = str(int(cik))  # strip leading zeros for archive URL
        results: list[FilingMetadata] = []

        for form, fd_str, rd_str, acc, doc in zip(
            forms, filing_dates, report_dates, accessions, primary_docs, strict=False
        ):
            if form not in form_set:
                continue
            filing_date = date.fromisoformat(fd_str)
            if not (date_from <= filing_date <= date_to):
                continue
            period = date.fromisoformat(rd_str) if rd_str else filing_date
            acc_nodash = acc.replace("-", "")
            primary_url = _ARCHIVES_URL.format(
                cik_int=cik_int, accession_nodash=acc_nodash, doc=doc
            )
            results.append(
                FilingMetadata(
                    cik=cik,
                    accession_number=acc,  # keep dashed — gotcha 2
                    form_type=form,
                    filing_date=filing_date,
                    period_of_report=period,
                    primary_doc_url=primary_url,
                    primary_doc_filename=doc,
                )
            )
        return results

    async def list_filings(
        self,
        cik: str,
        form_types: Iterable[str] = ("10-K", "10-Q"),
        date_from: date = date(2022, 1, 1),
        date_to: date = date(2026, 12, 31),
    ) -> list[FilingMetadata]:
        """Return filings for *cik* filtered by form type and date range.

        Reads ``data.sec.gov/submissions/CIK{cik}.json``, then follows the overflow pages
        in ``filings.files[]`` whose date bounds intersect ``[date_from, date_to]``.

        The ``recent`` window holds only the latest ~1000 filings across *all* form types,
        so for high-volume filers (JPM: 68 overflow pages) it reaches back mere months and
        the 10-K/10-Q history lives entirely in overflow. Both sources go through the same
        parser and filters; the union is deduplicated on ``accession_number``.
        """
        url = _SUBMISSIONS_URL.format(cik=cik)
        data: dict[str, Any] = (await self._get_sec(url)).json()
        form_set = set(form_types)

        recent: dict[str, list[Any]] = data["filings"]["recent"]
        results: list[FilingMetadata] = self._parse_filing_block(
            recent, cik, form_set=form_set, date_from=date_from, date_to=date_to
        )

        # Pages are newest-first, but _page_overlaps is order-independent: scan every page,
        # never break early on the first miss.
        overflow: list[dict[str, Any]] = data["filings"].get("files") or []
        pages = [p for p in overflow if self._page_overlaps(p, date_from, date_to)]
        if overflow:
            # Expected path since the S-fix — reading overflow is normal, not an anomaly.
            _log.info(
                "EDGAR overflow for CIK %s: %d page(s) found, %d overlap the window and "
                "will be read",
                cik,
                len(overflow),
                len(pages),
            )

        # Sequential — the 10 req/s budget is per-IP and global, so no parallel page fetches.
        for page in pages:
            if self._page_bounds(page) is None:
                # The real anomaly: SEC gave us a page we cannot judge, so we fetch blind
                # rather than risk dropping filings. Worth a human look if it persists.
                _log.warning(
                    "EDGAR overflow page %s for CIK %s has unusable date bounds "
                    "(filingFrom/filingTo missing or unparseable) — fetching it anyway",
                    page.get("name"),
                    cik,
                )
            page_data: dict[str, list[Any]] = (
                await self._get_sec(_OVERFLOW_URL.format(name=page["name"]))
            ).json()
            results.extend(
                self._parse_filing_block(
                    page_data, cik, form_set=form_set, date_from=date_from, date_to=date_to
                )
            )

        # Defensive: a filing present in both recent and an overflow page must not
        # double-enqueue. Order-preserving, recent wins.
        deduped: dict[str, FilingMetadata] = {}
        for meta in results:
            deduped.setdefault(meta.accession_number, meta)
        return list(deduped.values())

    async def fetch_primary_doc(self, meta: FilingMetadata) -> bytes:
        """Return primary document bytes, using R2 as a write-through cache.

        Flow: R2 HEAD → hit: R2 GET; miss: SEC GET → R2 PUT → return bytes.
        """
        if self._r2 is None:
            raise RuntimeError("EdgarClient must be entered via 'async with' before fetch")
        r2 = self._r2
        key = meta.r2_cache_key
        bucket = self._settings.r2_bucket_name
        try:
            await r2.head_object(Bucket=bucket, Key=key)
            obj = await r2.get_object(Bucket=bucket, Key=key)
            return bytes(await obj["Body"].read())
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("404", "NoSuchKey"):
                raise
        content: bytes = (await self._get_sec(meta.primary_doc_url)).content
        await r2.put_object(Bucket=bucket, Key=key, Body=content, ContentType="text/html")
        return content

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential_jitter(initial=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _get_sec(self, url: str) -> httpx.Response:
        """Rate-limited GET to an EDGAR endpoint; retries 429/5xx up to 3×."""
        await self._rate_limiter.acquire()
        r = await self._http.get(
            url,
            headers={"User-Agent": self._settings.sec_edgar_user_agent},
        )
        r.raise_for_status()
        return r
