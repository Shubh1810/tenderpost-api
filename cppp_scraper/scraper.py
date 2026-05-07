"""
scraper.py — HTTP fetch + HTML parse logic for CPPP listing and detail pages.

Session design: one shared httpx.AsyncClient for all requests.
Detail page tokens are session-scoped (Drupal CSRF nonce embedded in href).
Using a single client ensures session cookies from the listing fetch are
automatically sent with every subsequent detail fetch.

Hishel is used for in-memory caching within a single run only.
Disk caching of listing pages is intentionally avoided — cached pages from
a previous session would contain stale tokens that Drupal would reject.
"""
import asyncio
import random
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import hishel
import httpx
from bs4 import BeautifulSoup
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import (
    BASE_HEADERS,
    BASE_URL,
    DETAIL_BASE,
    MAX_DELAY,
    MIN_DELAY,
    RETRY_ATTEMPTS,
    RETRY_MAX_WAIT,
    RETRY_MIN_WAIT,
    USER_AGENTS,
)

# ── Exceptions that trigger retry ─────────────────────────────────────────────
_RETRYABLE = (httpx.TransportError, httpx.TimeoutException)


class _RetryableError(Exception):
    """Raised to trigger tenacity retry on 429/503."""


# ── Client factory ────────────────────────────────────────────────────────────

def build_client() -> httpx.AsyncClient:
    """
    Single shared AsyncClient for ALL requests (listing + detail).

    Sharing one client ensures the Drupal session cookie established during
    the listing-page fetch is automatically included in detail-page requests.
    Detail URLs embed a Drupal CSRF nonce that is only valid within the
    same session; using a different client (different cookie jar) will result
    in 'Invalid Url' responses from the server.

    Hishel in-memory caching is applied for within-run dedup of listing
    pages (e.g., if the same page is somehow requested twice during a run).
    Disk caching is intentionally NOT used to prevent stale tokens across
    different scraping sessions.
    """
    storage = hishel.AsyncInMemoryStorage()
    controller = hishel.Controller(
        cacheable_methods=["GET"],
        cacheable_status_codes=[200],
        allow_stale=False,
    )
    transport = hishel.AsyncCacheTransport(
        transport=httpx.AsyncHTTPTransport(retries=0),
        storage=storage,
        controller=controller,
    )
    return httpx.AsyncClient(
        transport=transport,
        headers={
            **BASE_HEADERS,
            "User-Agent": random.choice(USER_AGENTS),
            # Broad Accept mimics real browser — required by some Drupal middlewares
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
        },
        timeout=httpx.Timeout(30.0, connect=15.0),
        follow_redirects=True,
    )


# ── Rate-limit helper ─────────────────────────────────────────────────────────

async def _jitter_sleep() -> None:
    await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


# ── Fetch helpers ─────────────────────────────────────────────────────────────

async def fetch_listing_page(
    client: httpx.AsyncClient, page: int
) -> Optional[str]:
    """
    Fetch one listing page with retry on 429/503/transport errors.
    Returns HTML text or None on unrecoverable failure.
    """
    url = f"{BASE_URL}?page={page}"
    client.headers["User-Agent"] = random.choice(USER_AGENTS)
    await _jitter_sleep()

    try:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((*_RETRYABLE, _RetryableError)),
            wait=wait_exponential(multiplier=1, min=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            reraise=True,
        ):
            with attempt:
                resp = await client.get(url)
                if resp.status_code in (429, 503, 502, 504):
                    logger.warning(
                        f"[listing p{page}] HTTP {resp.status_code} — will retry"
                    )
                    raise _RetryableError(resp.status_code)
                resp.raise_for_status()
                return resp.text
    except Exception as exc:
        logger.error(f"[listing p{page}] unrecoverable: {exc}")
        return None


async def fetch_detail(
    client: httpx.AsyncClient, detail_url: str, listing_page_url: str
) -> Optional[str]:
    """
    Fetch a detail page.
    - Sets Referer to the listing page (required by Drupal's token validation).
    - Uses the same client (same session cookie) that fetched the listing page.
    - Returns HTML text, None on unrecoverable HTTP failure, or '' if
      Drupal reports the token as invalid ('Invalid Url.Please Check').
    """
    full_url = (
        detail_url
        if detail_url.startswith("http")
        else urljoin(DETAIL_BASE, detail_url)
    )
    client.headers["User-Agent"] = random.choice(USER_AGENTS)
    await _jitter_sleep()

    try:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((*_RETRYABLE, _RetryableError)),
            wait=wait_exponential(multiplier=1, min=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            reraise=True,
        ):
            with attempt:
                resp = await client.get(
                    full_url, headers={"Referer": listing_page_url}
                )
                if resp.status_code in (429, 503, 502, 504):
                    logger.warning(
                        f"[detail] HTTP {resp.status_code} → {full_url[:80]} — retry"
                    )
                    raise _RetryableError(resp.status_code)
                resp.raise_for_status()
                text = resp.text
                if (
                    "Invalid Url" in text
                    or "invalid url" in text.lower()
                    or "Invalid parameter" in text
                ):
                    logger.debug(f"[detail] token invalid: {full_url[:80]}")
                    return ""   # sentinel: token_expired
                return text
    except Exception as exc:
        logger.error(f"[detail] unrecoverable {full_url[:80]}: {exc}")
        return None


# ── HTML parsers ──────────────────────────────────────────────────────────────

def _extract_token(detail_url: str) -> str:
    """
    Decode the stable Tender ID from a CPPP detail URL.

    URL path token format: {base64_node_id}A13h1{...}A13h1{base64_tender_id}
    The LAST A13h1-separated segment decodes to the actual Tender ID like
    '2026_CEUCZ_1129415_1', which is stable across sessions.

    Falls back to the raw last-path-segment if decoding fails.
    """
    import base64
    raw_token = detail_url.rstrip("/").split("/")[-1]
    # Split on the session-nonce separator
    parts = raw_token.split("A13h1")
    # Try to decode the last segment — it's the tender ID
    for segment in reversed(parts):
        if not segment:
            continue
        # Pad to valid base64 length
        padded = segment + "=" * (-len(segment) % 4)
        try:
            decoded = base64.b64decode(padded).decode("utf-8", errors="strict")
            # Valid tender IDs look like '2026_DEPT_12345_1' or similar printable ASCII
            if decoded and decoded.isprintable() and len(decoded) < 80:
                return decoded
        except Exception:
            pass
    # Fallback: return the raw token (opaque but unique per URL)
    return raw_token


def get_total_count(html: str) -> Optional[int]:
    """
    Parse the total tender count from the listing page header.
    Selector: div#edit-data4 > div[style*="font-weight: bold"]
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("div#edit-data4")
    if not container:
        return None
    bold = container.select_one('div[style*="font-weight: bold"]')
    if not bold:
        return None
    text = bold.get_text(" ", strip=True)
    m = re.search(r"[\d,]+", text)
    if m:
        return int(m.group().replace(",", ""))
    return None


def parse_listing_html(html: str, page: int) -> List[Dict[str, Any]]:
    """
    Parse one listing page into a list of tender dicts.

    Columns (0-indexed td):
      0 → sl_no
      1 → published_date
      2 → bid_close_date
      3 → open_date
      4 → title (a.text), detail_url (a[href]), ref_no (remaining text)
      5 → state
      6 → corrigendum
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table#table.list_table") or soup.select_one(
        "table.list_table"
    )
    if not table:
        logger.warning(f"[p{page}] list_table not found — page may be empty")
        return []

    rows = table.select("tbody tr")
    tenders: List[Dict[str, Any]] = []

    for row in rows:
        cells = row.find_all("td", recursive=False)
        if len(cells) < 6:
            continue

        col4 = cells[4]
        link = col4.find("a")
        if not link:
            continue

        title = link.get_text(strip=True)
        detail_url: str = link.get("href", "")
        if not detail_url:
            continue

        full_text = col4.get_text(" ", strip=True)
        ref_no = full_text.replace(title, "").strip(" /|-")

        tender_id = _extract_token(detail_url)
        if not tender_id:
            continue

        bid_close = cells[2].get_text(strip=True)
        open_date = cells[3].get_text(strip=True)
        corrigendum = cells[6].get_text(strip=True) if len(cells) > 6 else ""

        t: Dict[str, Any] = {
            "tender_id": tender_id,
            "title": title,
            # ref_no = tender_id for CPPP source — guaranteed unique,
            # matches the (ref_no, source) unique constraint on Supabase
            "ref_no": tender_id,
            "published_date": cells[1].get_text(strip=True),
            "bid_close_date": bid_close,
            "open_date": open_date,
            "state": cells[5].get_text(strip=True) if len(cells) > 5 else "",
            "corrigendum": corrigendum,
            "detail_url": (
                detail_url
                if detail_url.startswith("http")
                else urljoin(DETAIL_BASE, detail_url)
            ),
            "_listing_url": f"{BASE_URL}?page={page}",
        }
        # Attach content_hash for change-detection in producer
        from .db import compute_hash
        t["content_hash"] = compute_hash(t)
        tenders.append(t)

    return tenders


def _cell_value(td) -> str:
    """
    Extract clean text from a detail page value cell.
    Prefers the inner div.event-dtl if present (used for long values like org names).
    """
    inner = td.select_one("div.event-dtl")
    if inner:
        return inner.get_text(" ", strip=True)
    return td.get_text(" ", strip=True)


def parse_detail_html(html: str) -> Dict[str, str]:
    """
    Parse a CPPP tender detail page into a flat key→value dict.

    Actual page structure (3-column and 6-column row variants):
      3-col: <td>Label</td><td>:</td><td>Value</td>
      6-col: <td>Label</td><td>:</td><td>Value</td>
             <td>Label</td><td>:</td><td>Value</td>
    td[1] and td[4] are colon separators and are skipped.
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("form.tenderfullview-tenders") or soup

    detail: Dict[str, str] = {}

    for tr in container.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        n = len(tds)

        if n == 3:
            # label | : | value
            key = tds[0].get_text(strip=True).rstrip(":")
            val = _cell_value(tds[2])
            if key and val:
                detail[key] = val

        elif n == 6:
            # label | : | value | label | : | value
            for label_i, val_i in [(0, 2), (3, 5)]:
                key = tds[label_i].get_text(strip=True).rstrip(":")
                val = _cell_value(tds[val_i])
                if key and val:
                    detail[key] = val

    return detail
