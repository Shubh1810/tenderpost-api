"""
cloudflare_crawl.py — Cloudflare Browser Rendering /crawl async client.

Two entry points:

1. crawl_cppp_listing()
   Scrapes the CPPP "Latest Active Tenders" portal:
   https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata
   - ~3,096 pages, 10 tenders each (~30,954 tenders total)
   - render=false (server-rendered HTML, free during beta)
   - One crawl job from the root URL; Cloudflare auto-discovers ?page=N links
   - AI extracts an array of tenders per page via JSON schema
   - Returns List[Dict] of flat tender records

2. crawl_detail_pages(urls)
   Fetches individual tender detail pages from eprocure.gov.in/eprocure/app
   (the session-token-based URLs collected by Playwright).
   - render=false (static Tapestry HTML, free during beta)
   - One job per URL, rate-limited, polled concurrently
   - Fallback: Cloudflare HTML + Selectolax → direct httpx + Selectolax

Cloudflare API:
  POST /accounts/{account_id}/browser-rendering/crawl  → {result: {id: job_id}}
  GET  /accounts/{account_id}/browser-rendering/crawl/{job_id} → job result

Environment variables required:
  CLOUDFLARE_API_TOKEN   — API token with "Browser Rendering - Edit" permission
  CLOUDFLARE_ACCOUNT_ID  — Cloudflare account ID
"""

import asyncio
import os
import time
from collections import deque
from typing import AsyncIterator, Dict, List, Optional, Tuple

import httpx

from logger import log_cf, run_summary
from scraper import parse_detail_page


# ── Configuration ─────────────────────────────────────────────────────────────

CF_BASE      = "https://api.cloudflare.com/client/v4/accounts/{account_id}/browser-rendering"
CF_CRAWL_URL = CF_BASE + "/crawl"
CF_POLL_URL  = CF_BASE + "/crawl/{job_id}"

# CPPP listing URL (Central Active Tenders — no CAPTCHA, server-rendered HTML)
CPPP_LISTING_URL  = "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"
CPPP_BASE_URL     = "https://eprocure.gov.in"
# ~3,096 listing pages × 10 tenders = ~30,954 tenders; set limit with margin
CPPP_PAGE_LIMIT   = 35_000
# Max wait for the full CPPP crawl (big job: 3096 pages × ~1s each ≈ 1 hour)
CPPP_POLL_MAX     = 7200  # 2-hour timeout

# Rate limiting (Cloudflare free plan: 6 POST requests per minute)
MAX_SUBMISSIONS_PER_MINUTE = 6
RATE_WINDOW_SECS           = 60.0

# Polling
POLL_INITIAL_WAIT = 3     # seconds before first poll attempt
POLL_BACKOFF_BASE = 2     # exponential multiplier
POLL_BACKOFF_CAP  = 60    # max seconds between polls
POLL_MAX_TOTAL    = 300   # 5-minute total timeout per job

# Submission retries
MAX_RETRIES      = 3
RETRY_BASE_DELAY = 2.0    # seconds, doubles per retry

# Session-expiry indicators in returned HTML (Tapestry app)
SESSION_EXPIRY_PHRASES = [
    "session has expired",
    "session expired",
    "invalid session",
    "your session",
    "please login again",
]

# ── CPPP listing JSON extraction schema ───────────────────────────────────────
# Each listing page has a table of 10 tenders. Extract all rows as an array.

_CPPP_JSON_OPTIONS = {
    "prompt": (
        "Extract all tender rows from the table on this Indian government CPPP "
        "(Central Public Procurement Portal) page. The table has columns: "
        "Sl.No, e-Published Date, Bid Submission Closing Date, Tender Opening Date, "
        "Title/Ref.No./Tender Id, Organisation Name, Corrigendum. "
        "Each row is one tender. Also extract the href attribute of the link in the "
        "Title/Ref.No./Tender Id column (it links to /cppp/tendersfullview/...). "
        "Return all rows found on this page as an array."
    ),
    "schema": {
        "type": "object",
        "required": [],
        "properties": {
            "tenders": {
                "type": "array",
                "description": "All tender rows extracted from the listing table",
                "items": {
                    "type": "object",
                    "required": [],
                    "properties": {
                        "serial_no": {
                            "type": "integer",
                            "description": "Row serial number (Sl.No column)"
                        },
                        "published_date": {
                            "type": "string",
                            "description": "e-Published Date (e.g. 19-Mar-2026 06:05 PM)"
                        },
                        "closing_date": {
                            "type": "string",
                            "description": "Bid Submission Closing Date"
                        },
                        "opening_date": {
                            "type": "string",
                            "description": "Tender Opening Date"
                        },
                        "tender_id": {
                            "type": "string",
                            "description": "Tender ID / Reference number from Title column"
                        },
                        "title": {
                            "type": "string",
                            "description": "Tender title/description from Title column"
                        },
                        "organisation": {
                            "type": "string",
                            "description": "Organisation Name"
                        },
                        "detail_path": {
                            "type": "string",
                            "description": "href of the link in the Title column (e.g. /cppp/tendersfullview/...)"
                        },
                        "corrigendum": {
                            "type": "string",
                            "description": "Corrigendum column value (-- if none)"
                        }
                    }
                }
            },
            "page_number": {
                "type": "integer",
                "description": "Current page number from the pagination area"
            },
            "total_records": {
                "type": "integer",
                "description": "Total number of records shown on the page (e.g. 30954)"
            }
        }
    }
}


# ── eprocure detail page JSON extraction schema ────────────────────────────────

_JSON_OPTIONS = {
    "prompt": (
        "Extract structured data from this Indian government eProcurement tender "
        "detail page (eprocure.gov.in). All data is inside tables with class "
        "'tablebg'. Label cells have class 'td_caption', value cells have class "
        "'td_field'. Tender Value and EMD Amount are displayed with Indian-style "
        "comma formatting (e.g. 3,86,15,626) — extract these as plain numbers "
        "with no commas, no currency symbol, no text."
    ),
    "schema": {
        "type": "object",
        "required": [],
        "properties": {
            "tender_id": {
                "type": "string",
                "description": "Tender ID from the Basic Details section"
            },
            "tender_type": {
                "type": "string",
                "description": "Tender Type (e.g. Open Tender, Limited Tender)"
            },
            "tender_category": {
                "type": "string",
                "description": "Tender Category (e.g. Works, Goods, Services)"
            },
            "contract_type": {
                "type": "string",
                "description": "Form Of Contract field (e.g. Item Rate, Lump Sum)"
            },
            "work_description": {
                "type": "string",
                "description": "Work Description from the Work Item Details section"
            },
            "product_category": {
                "type": "string",
                "description": "Product Category (e.g. Electrical Works, Civil Works). Null if NA."
            },
            "sub_category": {
                "type": "string",
                "description": "Sub category field. Null if NA."
            },
            "location": {
                "type": "string",
                "description": "Location field in Work Item Details. Null if NA."
            },
            "pincode": {
                "type": "string",
                "description": "Pincode. Null if NA or empty."
            },
            "tender_value": {
                "type": "number",
                "description": "Tender Value in rupees as a plain number (no commas, no symbol)"
            },
            "emd_amount": {
                "type": "number",
                "description": "EMD Amount in rupees as a plain number. From EMD Fee Details section."
            },
            "period_of_work_days": {
                "type": "integer",
                "description": "Period Of Work expressed in days as an integer"
            },
            "bid_validity_days": {
                "type": "integer",
                "description": "Bid Validity expressed in days as an integer"
            },
            "inviting_authority_name": {
                "type": "string",
                "description": "Name from the Tender Inviting Authority section at the bottom"
            },
            "inviting_authority_address": {
                "type": "string",
                "description": "Address from the Tender Inviting Authority section"
            },
        }
    }
}


# ── Rate limiter ───────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Rolling-window rate limiter. Allows at most `max_calls` per `window_seconds`.

    Uses a deque of monotonic timestamps. acquire() sleeps until a slot opens.
    Thread-safe via asyncio.Lock — safe for concurrent coroutines.
    """

    def __init__(self, max_calls: int = 6, window_seconds: float = 60.0) -> None:
        self._max_calls = max_calls
        self._window    = window_seconds
        self._calls: deque = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # Evict timestamps outside the current window
            while self._calls and self._calls[0] < now - self._window:
                self._calls.popleft()
            # If at capacity, sleep until the oldest slot expires
            if len(self._calls) >= self._max_calls:
                sleep_for = self._window - (now - self._calls[0]) + 0.05
                await asyncio.sleep(max(0.0, sleep_for))
                # Re-evict after sleeping
                now = time.monotonic()
                while self._calls and self._calls[0] < now - self._window:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_session_expired(html: str) -> bool:
    """Return True if the HTML looks like a Tapestry session-expiry page."""
    lower = html.lower()
    return any(phrase in lower for phrase in SESSION_EXPIRY_PHRASES)


def _cf_headers(api_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }


def _log(level: str, msg: str) -> None:
    """Route legacy _log() calls through the structured logger."""
    {"INFO": log_cf.info, "OK": log_cf.success,
     "WARN": log_cf.warning, "ERR": log_cf.error}.get(level, log_cf.debug)(msg)


# ── Core API calls ─────────────────────────────────────────────────────────────

async def submit_crawl(
    url: str,
    client: httpx.AsyncClient,
    account_id: str,
    api_token: str,
    rate_limiter: RateLimiter,
) -> Optional[str]:
    """
    Submit a single URL to Cloudflare /crawl. Returns job_id or None on failure.

    Uses render=false (free during beta), formats=["json"] with AI schema,
    and limit=1 to prevent link following from the detail page.

    Retries up to MAX_RETRIES on 429 or 5xx responses with exponential backoff.
    Returns None for 4xx (non-429) errors — these won't succeed on retry.
    """
    endpoint = CF_CRAWL_URL.format(account_id=account_id)
    payload = {
        "url":         url,
        "render":      False,
        "limit":       1,
        "formats":     ["json"],
        "jsonOptions": _JSON_OPTIONS,
    }

    await rate_limiter.acquire()
    log_cf.debug(f"Submitting job for {url[:70]}")

    for attempt in range(MAX_RETRIES):
        t0 = time.monotonic()
        try:
            resp = await client.post(
                endpoint,
                headers=_cf_headers(api_token),
                json=payload,
                timeout=30.0,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            log_cf.warning(f"Submit network error (attempt {attempt+1}/{MAX_RETRIES}): {exc} — retry in {delay:.1f}s")
            await asyncio.sleep(delay)
            continue

        elapsed = time.monotonic() - t0

        if resp.status_code == 200:
            data   = resp.json()
            job_id = (data.get("result") or {}).get("id")
            if job_id:
                log_cf.debug(f"Job submitted → {job_id} ({elapsed:.2f}s) for {url[:60]}")
                return job_id
            log_cf.warning(f"Submit 200 but no job_id: {data}")
            return None

        if resp.status_code == 429 or resp.status_code >= 500:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            log_cf.warning(f"Submit HTTP {resp.status_code} (attempt {attempt+1}/{MAX_RETRIES}) — retry in {delay:.1f}s")
            run_summary.increment("cf_rate_limited") if resp.status_code == 429 else None
            await asyncio.sleep(delay)
            continue

        log_cf.error(f"Submit HTTP {resp.status_code} (permanent) for {url[:60]}: {resp.text[:200]}")
        return None

    log_cf.error(f"Submit exhausted {MAX_RETRIES} retries for {url[:60]}")
    return None


async def poll_job(
    job_id: str,
    client: httpx.AsyncClient,
    account_id: str,
    api_token: str,
) -> Optional[Dict]:
    """
    Poll a Cloudflare crawl job until it completes, fails, or times out.

    Polling strategy: exponential backoff starting at POLL_INITIAL_WAIT seconds,
    doubling up to POLL_BACKOFF_CAP, total timeout POLL_MAX_TOTAL seconds.

    Returns the job result dict on completion, or None on timeout/failure.
    The result dict has shape:
        {"status": "complete", "records": [{"url": ..., "json": {...}, "html": ...}]}
    """
    endpoint = CF_POLL_URL.format(account_id=account_id, job_id=job_id)
    headers  = {"Authorization": f"Bearer {api_token}"}

    wait    = POLL_INITIAL_WAIT
    elapsed = 0.0

    while elapsed < POLL_MAX_TOTAL:
        await asyncio.sleep(wait)
        elapsed += wait

        try:
            resp = await client.get(endpoint, headers=headers, timeout=30.0)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            _log("WARN", f"poll network error for {job_id}: {exc}")
            wait = min(wait * POLL_BACKOFF_BASE, POLL_BACKOFF_CAP)
            continue

        if resp.status_code == 404:
            _log("WARN", f"job {job_id} not found (expired or invalid)")
            return None

        if resp.status_code == 429:
            # Rate-limited on polling — back off harder
            wait = min(wait * 3, POLL_BACKOFF_CAP)
            continue

        if resp.status_code != 200:
            wait = min(wait * POLL_BACKOFF_BASE, POLL_BACKOFF_CAP)
            continue

        data   = resp.json()
        result = data.get("result") or data
        status = result.get("status", "")

        if status in ("complete", "completed"):
            log_cf.debug(f"Job {job_id} complete ({elapsed:.0f}s elapsed)")
            return result

        if status in ("failed", "error", "cancelled_due_to_timeout",
                      "cancelled_due_to_limits", "cancelled_by_user"):
            log_cf.warning(f"Job {job_id} terminal status: {status} ({elapsed:.0f}s)")
            return None

        # Still running — keep polling
        log_cf.debug(f"Job {job_id} status={status!r} ({elapsed:.0f}s elapsed) — next poll in {min(wait * POLL_BACKOFF_BASE, POLL_BACKOFF_CAP):.0f}s")
        wait = min(wait * POLL_BACKOFF_BASE, POLL_BACKOFF_CAP)

    log_cf.warning(f"Job {job_id} timed out after {elapsed:.0f}s")
    return None


# ── Fallback ───────────────────────────────────────────────────────────────────

async def _fallback_selectolax(url: str, client: httpx.AsyncClient) -> Dict:
    """
    Fallback: fetch the URL directly with httpx (no browser) and parse with
    the existing Selectolax parser parse_detail_page() from scraper.py.

    Used when Cloudflare returns no usable JSON fields or the job fails.
    Sets a realistic browser User-Agent to reduce risk of 403 from eprocure.gov.in.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = await client.get(url, headers=headers, timeout=20.0,
                                follow_redirects=True)
        if resp.status_code != 200:
            _log("WARN", f"fallback HTTP {resp.status_code} for {url[:60]}")
            return {}
        html = resp.text
        if _is_session_expired(html):
            _log("WARN", f"session expired on fallback for {url[:60]}")
            return {}
        return parse_detail_page(html)
    except Exception as exc:
        _log("WARN", f"fallback failed for {url[:60]}: {exc}")
        return {}


def _extract_fields_from_job_result(result: Dict, url: str) -> Optional[Dict]:
    """
    Pull the AI-extracted JSON fields from a completed job result.

    Tries the 'records' array first; falls back to Cloudflare HTML + Selectolax
    if JSON fields are absent. Returns None if nothing useful found.
    """
    records = result.get("records") or []
    for record in records:
        record_url    = record.get("url", "")
        record_status = record.get("status", "")

        if record_status != "completed":
            continue

        # Try AI JSON extraction first
        json_fields = record.get("json") or {}
        non_null = {k: v for k, v in json_fields.items() if v is not None}
        if non_null:
            return non_null

        # Try Cloudflare-fetched HTML + Selectolax
        html = record.get("html") or ""
        if html and not _is_session_expired(html):
            parsed = parse_detail_page(html)
            if parsed:
                return parsed

    return None


# ── Main orchestrator ──────────────────────────────────────────────────────────

async def crawl_detail_pages(urls: List[str]) -> Dict[str, Dict]:
    """
    Phase 2 orchestrator: submit all detail URLs to Cloudflare /crawl,
    poll concurrently, apply fallback chain, return url→fields mapping.

    Pipeline:
    1. Create one shared httpx.AsyncClient for all requests.
    2. Submit each URL as a separate crawl job (rate-limited: 6/min).
       URLs that fail submission are flagged for the direct-fetch fallback.
    3. Poll all successful jobs concurrently via asyncio.gather().
    4. For each result, apply fallback chain:
       a. Cloudflare AI JSON → use if >= 1 non-null field
       b. Cloudflare HTML + Selectolax → use if HTML present and not expired
       c. Direct httpx fetch + Selectolax → last resort
       d. Empty dict → log warning, no detail data for this URL
    5. Return Dict[url → extracted_fields_dict].

    Args:
        urls: List of tender detail page URLs from Phase 1.
              These contain Tapestry sp= session tokens — submit immediately
              after Phase 1 completes to minimise token expiry risk.

    Returns:
        Dict mapping every input URL to its (possibly empty) fields dict.
    """
    if not urls:
        return {}

    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    api_token  = os.getenv("CLOUDFLARE_API_TOKEN", "")

    if not account_id or not api_token:
        _log("WARN", "CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN not set — skipping CF phase, using direct fallback")
        # Fall through to direct httpx fallback for all URLs
        async with httpx.AsyncClient() as client:
            tasks = [_fallback_selectolax(u, client) for u in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            url: (res if isinstance(res, dict) else {})
            for url, res in zip(urls, results)
        }

    _log("INFO", f"Starting CF crawl for {len(urls)} URLs")
    rate_limiter = RateLimiter(
        max_calls=MAX_SUBMISSIONS_PER_MINUTE,
        window_seconds=RATE_WINDOW_SECS,
    )

    async with httpx.AsyncClient() as client:
        # ── Step 1: Submit all jobs ──────────────────────────────────────────
        submit_tasks = [
            submit_crawl(url, client, account_id, api_token, rate_limiter)
            for url in urls
        ]
        job_ids: List[Optional[str]] = await asyncio.gather(
            *submit_tasks, return_exceptions=False
        )

        job_to_url: Dict[str, str] = {}
        fallback_urls: List[str]   = []

        for url, job_id in zip(urls, job_ids):
            if job_id:
                job_to_url[job_id] = url
            else:
                fallback_urls.append(url)

        run_summary.record("cf_jobs_submitted", len(job_to_url))
        log_cf.info(f"Submission complete — {len(job_to_url)} jobs queued, {len(fallback_urls)} failed submission → direct fallback")

        # ── Step 2: Poll all jobs concurrently ───────────────────────────────
        poll_tasks = [
            poll_job(job_id, client, account_id, api_token)
            for job_id in job_to_url
        ]
        poll_results = await asyncio.gather(*poll_tasks, return_exceptions=False)

        # ── Step 3: Extract fields + apply fallback chain ────────────────────
        output: Dict[str, Dict] = {}
        further_fallback: List[str] = list(fallback_urls)

        cf_succeeded = 0
        cf_failed    = 0
        for job_id, poll_result in zip(job_to_url.keys(), poll_results):
            url = job_to_url[job_id]

            if poll_result is None:
                log_cf.warning(f"Job {job_id} returned no result — queuing for fallback")
                further_fallback.append(url)
                cf_failed += 1
                continue

            fields = _extract_fields_from_job_result(poll_result, url)
            if fields:
                output[url] = fields
                log_cf.debug(f"CF extracted {len(fields)} fields — {url[:60]}")
                cf_succeeded += 1
            else:
                log_cf.debug(f"CF returned no fields — queuing fallback for {url[:60]}")
                further_fallback.append(url)
                cf_failed += 1

        run_summary.record("cf_jobs_succeeded", cf_succeeded)
        run_summary.record("cf_jobs_failed", cf_failed)
        log_cf.info(f"Poll results — succeeded={cf_succeeded}, need_fallback={len(further_fallback)}")

        # ── Step 4: Direct httpx fallback for remaining URLs ─────────────────
        if further_fallback:
            log_cf.info(f"Direct-fetch fallback for {len(further_fallback)} URLs...")
            run_summary.record("cf_fallback_used", len(further_fallback))
            fallback_tasks = [
                _fallback_selectolax(u, client) for u in further_fallback
            ]
            fallback_results = await asyncio.gather(
                *fallback_tasks, return_exceptions=False
            )
            fallback_ok = 0
            for url, fields in zip(further_fallback, fallback_results):
                if isinstance(fields, dict) and fields:
                    output[url] = fields
                    fallback_ok += 1
                    log_cf.debug(f"Fallback extracted {len(fields)} fields — {url[:60]}")
                else:
                    output[url] = {}
                    log_cf.warning(f"No fields extractable (CF + fallback both failed) — {url[:60]}")
            log_cf.info(f"Fallback complete — {fallback_ok}/{len(further_fallback)} recovered")

        # Ensure every input URL has an entry
        for url in urls:
            if url not in output:
                output[url] = {}

    total_with_data = sum(1 for v in output.values() if v)
    log_cf.success(f"CF phase complete — {total_with_data}/{len(urls)} URLs had extractable data")
    return output


# ── Streaming detail page crawler ─────────────────────────────────────────────

async def _poll_with_id(
    job_id: str,
    client: httpx.AsyncClient,
    account_id: str,
    api_token: str,
) -> Tuple[str, Optional[Dict]]:
    """Wrap poll_job to carry job_id through asyncio.as_completed()."""
    result = await poll_job(job_id, client, account_id, api_token)
    return job_id, result


async def crawl_detail_pages_stream(
    url_to_ref: Dict[str, str],
) -> AsyncIterator[Tuple[str, str, Dict]]:
    """
    Streaming version of crawl_detail_pages.

    Submits all detail URLs as Cloudflare jobs (rate-limited), then yields
    (url, ref_no, fields) as each individual job completes — using
    asyncio.as_completed() instead of asyncio.gather().

    This means callers can write each result to Supabase immediately as it
    arrives, rather than buffering everything in memory until the last job
    finishes. A process crash after 40 of 50 jobs still saves 40 tenders.

    Args:
        url_to_ref: Dict mapping detail page URL → stable ref_no.
                    Using ref_no (not URL) as the Supabase update key avoids
                    relying on volatile sp= session token URLs.

    Yields:
        (url, ref_no, fields) — fields is {} if extraction failed entirely.
    """
    if not url_to_ref:
        return

    urls       = list(url_to_ref.keys())
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    api_token  = os.getenv("CLOUDFLARE_API_TOKEN", "")

    if not account_id or not api_token:
        log_cf.warning("CF credentials missing — streaming fallback via direct httpx")
        async with httpx.AsyncClient() as client:
            for url in urls:
                fields = await _fallback_selectolax(url, client)
                yield url, url_to_ref[url], fields or {}
        return

    rate_limiter = RateLimiter(
        max_calls=MAX_SUBMISSIONS_PER_MINUTE,
        window_seconds=RATE_WINDOW_SECS,
    )

    async with httpx.AsyncClient() as client:
        # ── Submit all jobs (rate-limited) ───────────────────────────────────
        submit_tasks = [
            submit_crawl(url, client, account_id, api_token, rate_limiter)
            for url in urls
        ]
        job_ids: List[Optional[str]] = await asyncio.gather(*submit_tasks)

        job_to_url: Dict[str, str] = {}
        fallback_urls: List[str]   = []
        for url, job_id in zip(urls, job_ids):
            if job_id:
                job_to_url[job_id] = url
            else:
                fallback_urls.append(url)

        log_cf.info(
            f"Stream: {len(job_to_url)} jobs submitted, "
            f"{len(fallback_urls)} direct-fallback, "
            f"yielding results as each completes..."
        )

        # ── Poll concurrently, yield as each finishes ────────────────────────
        poll_tasks = [
            _poll_with_id(job_id, client, account_id, api_token)
            for job_id in job_to_url
        ]

        for coro in asyncio.as_completed(poll_tasks):
            job_id, poll_result = await coro
            url    = job_to_url[job_id]
            ref_no = url_to_ref[url]

            if poll_result is None:
                log_cf.warning(f"Stream: job {job_id} failed — falling back for {url[:60]}")
                fields = await _fallback_selectolax(url, client)
                yield url, ref_no, fields or {}
                continue

            fields = _extract_fields_from_job_result(poll_result, url)
            if fields:
                log_cf.debug(f"Stream: yielding {len(fields)} fields for {ref_no}")
                yield url, ref_no, fields
            else:
                log_cf.debug(f"Stream: no CF fields for {url[:60]} — falling back")
                fields = await _fallback_selectolax(url, client)
                yield url, ref_no, fields or {}

        # ── Direct fallback for submission-failed URLs ────────────────────────
        for url in fallback_urls:
            ref_no = url_to_ref[url]
            fields = await _fallback_selectolax(url, client)
            yield url, ref_no, fields or {}


# ── CPPP listing crawler ───────────────────────────────────────────────────────

def _parse_cppp_records(result: Dict) -> List[Dict]:
    """
    Extract flat tender dicts from a completed CPPP crawl job result.

    Each record in the job result represents one listing page (e.g. ?page=0).
    The AI extracts a 'tenders' array from each page. We flatten all arrays
    into a single list, normalise detail_path → full URL, and deduplicate by
    tender_id.

    Returns:
        List of flat tender dicts with keys matching TenderItem / Supabase schema.
    """
    seen_ids: set = set()
    tenders: List[Dict] = []

    for record in result.get("records") or []:
        if record.get("status") != "completed":
            continue

        json_data = record.get("json") or {}
        rows = json_data.get("tenders") or []

        for row in rows:
            if not isinstance(row, dict):
                continue

            tender_id = (row.get("tender_id") or "").strip()
            title     = (row.get("title") or "").strip()

            # Skip empty/header rows
            if not tender_id and not title:
                continue

            # Deduplicate by tender_id
            dedup_key = tender_id or title
            if dedup_key in seen_ids:
                continue
            seen_ids.add(dedup_key)

            # Resolve detail_path → full URL
            detail_path = (row.get("detail_path") or "").strip()
            full_url    = (
                f"{CPPP_BASE_URL}{detail_path}"
                if detail_path and not detail_path.startswith("http")
                else detail_path
            )

            tenders.append({
                "source":         "cppp",
                "ref_no":         tender_id or "UNKNOWN",
                "title":          title,
                "organisation":   (row.get("organisation") or "").strip() or None,
                "published_date": (row.get("published_date") or "").strip() or None,
                "closing_date":   (row.get("closing_date") or "").strip() or None,
                "opening_date":   (row.get("opening_date") or "").strip() or None,
                "url":            full_url or None,
                "tender_id":      tender_id or None,
            })

    return tenders


async def crawl_cppp_listing() -> Dict:
    """
    Crawl the CPPP "Latest Active Tenders - Central" listing page and all
    paginated pages using Cloudflare /crawl.

    Source: https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata
    - ~3,096 listing pages, 10 tenders per page, ~30,954 tenders total
    - render=false (server-rendered HTML — no JavaScript needed, free during beta)
    - Cloudflare auto-discovers ?page=N pagination links and crawls all pages
    - AI extracts an array of tenders from each page via _CPPP_JSON_OPTIONS schema

    Returns:
        {
            "success":  bool,
            "tenders":  List[Dict],   # flat tender dicts, deduplicated by tender_id
            "total":    int,
            "error":    Optional[str],
        }
    """
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    api_token  = os.getenv("CLOUDFLARE_API_TOKEN", "")

    if not account_id or not api_token:
        return {
            "success": False,
            "error":   "CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN not set",
            "tenders": [],
            "total":   0,
        }

    endpoint = CF_CRAWL_URL.format(account_id=account_id)
    payload  = {
        "url":         CPPP_LISTING_URL,
        "render":      False,      # server-rendered HTML — free during beta
        "limit":       CPPP_PAGE_LIMIT,
        "formats":     ["json"],
        "jsonOptions": _CPPP_JSON_OPTIONS,
    }

    _log("INFO", f"Submitting CPPP crawl job (limit={CPPP_PAGE_LIMIT}) ...")

    # ── Submit ───────────────────────────────────────────────────────────────
    job_id: Optional[str] = None
    async with httpx.AsyncClient() as client:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.post(
                    endpoint,
                    headers=_cf_headers(api_token),
                    json=payload,
                    timeout=30.0,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                _log("WARN", f"CPPP submit error (attempt {attempt+1}): {exc} — retry in {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 200:
                job_id = (resp.json().get("result") or {}).get("id")
                if job_id:
                    _log("OK", f"CPPP crawl job submitted → {job_id}")
                    break
                _log("ERR", f"CPPP submit 200 but no job_id: {resp.text[:200]}")
                return {"success": False, "error": "No job_id returned", "tenders": [], "total": 0}

            if resp.status_code == 429 or resp.status_code >= 500:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                _log("WARN", f"CPPP submit HTTP {resp.status_code} (attempt {attempt+1}) — retry in {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            _log("ERR", f"CPPP submit HTTP {resp.status_code}: {resp.text[:200]}")
            return {"success": False, "error": f"HTTP {resp.status_code}", "tenders": [], "total": 0}

        if not job_id:
            return {"success": False, "error": "Exhausted retries on CPPP submission", "tenders": [], "total": 0}

        # ── Poll ─────────────────────────────────────────────────────────────
        _log("INFO", f"Polling CPPP crawl job {job_id} (may take up to {CPPP_POLL_MAX//60} min)...")
        poll_endpoint = CF_POLL_URL.format(account_id=account_id, job_id=job_id)
        headers       = {"Authorization": f"Bearer {api_token}"}

        wait    = 15    # first poll after 15s (large job)
        elapsed = 0.0

        while elapsed < CPPP_POLL_MAX:
            await asyncio.sleep(wait)
            elapsed += wait

            try:
                resp = await client.get(poll_endpoint, headers=headers, timeout=30.0)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                _log("WARN", f"CPPP poll network error: {exc}")
                wait = min(wait * 2, 120)
                continue

            if resp.status_code != 200:
                wait = min(wait * 2, 120)
                continue

            result = resp.json().get("result") or resp.json()
            status = result.get("status", "")
            crawled = result.get("total", 0)

            _log("INFO", f"CPPP job status={status} pages_crawled={crawled} elapsed={elapsed:.0f}s")

            if status in ("complete", "completed"):
                tenders = _parse_cppp_records(result)
                _log("OK", f"CPPP crawl complete — {len(tenders)} tenders from {crawled} pages")
                return {"success": True, "tenders": tenders, "total": len(tenders)}

            if status in ("failed", "error", "cancelled_due_to_timeout",
                          "cancelled_due_to_limits", "cancelled_by_user"):
                _log("ERR", f"CPPP job terminal: {status}")
                return {"success": False, "error": f"Job status: {status}", "tenders": [], "total": 0}

            # Adaptive backoff: slow down polling for a big job
            wait = min(wait * 1.5, 120)

    _log("ERR", f"CPPP crawl timed out after {elapsed:.0f}s")
    return {"success": False, "error": "Poll timeout", "tenders": [], "total": 0}
