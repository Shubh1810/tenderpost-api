"""
scraper.py — httpx + selectolax listing and detail page scraper.

Sources:
  Central: https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata
  State:   https://eprocure.gov.in/cppp/latestactivetendersnew/mmpdata

Detail pages:
  Central: https://eprocure.gov.in/cppp/tendersfullview/[token]
  State:   https://eprocure.gov.in/cppp/tendersfullviewmmp/[token]

Pagination: ?page=N (10 records/page, 0-indexed).
No CAPTCHA, no browser, no cloud rendering — all data is in page source.
Detail URL tokens are session-bound; fetch using the same AsyncClient that
loaded the listing so session cookies are preserved automatically.
"""

import asyncio
import base64
import re
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

import httpx
from selectolax.parser import HTMLParser

from logger import log_scraper, run_summary

# ── URLs ──────────────────────────────────────────────────────────────────────

CENTRAL_LISTING_URL = "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"
STATE_LISTING_URL   = "https://eprocure.gov.in/cppp/latestactivetendersnew/mmpdata"
CPPP_BASE_URL       = "https://eprocure.gov.in"

SOURCES = {
    "central": CENTRAL_LISTING_URL,
    "state":   STATE_LISTING_URL,
}

# ── HTTP config ───────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

LISTING_PAGE_CONCURRENCY = 10   # concurrent listing page fetches (don't hammer gov server)
DETAIL_CONCURRENCY       = 20   # concurrent detail page fetches
REQUEST_TIMEOUT          = 25.0
REQUEST_RETRIES          = 3    # per-page retries before giving up


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    val = val.replace("\xa0", " ").strip()
    if not val or val.upper() in ("NA", "N/A", "-", "--", "NIL"):
        return None
    return val


def _parse_amount(val: Optional[str]) -> Optional[float]:
    if not val:
        return None
    cleaned = re.sub(r"[₹,\s]", "", val)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(val: Optional[str]) -> Optional[int]:
    if not val:
        return None
    m = re.search(r"\d+", val)
    return int(m.group()) if m else None


def _parse_total_count(html: str) -> Optional[int]:
    """Extract 'Total Tenders : 34233' from the listing page."""
    tree = HTMLParser(html)
    for node in tree.css("div, span, p, td, strong"):
        text = node.text(strip=True)
        m = re.search(r"Total\s+Tenders\s*[:\-]\s*([\d,]+)", text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def _extract_cppp_ref_no(url: str) -> Optional[str]:
    """
    Extract stable numeric tender ID from a CPPP detail URL token.
    The token encodes multiple fields separated by 'A13h1'; the first
    segment is a base64-encoded numeric database ID for the tender.
    """
    try:
        parts = re.split(r"/cppp/tendersfullview(?:mmp)?/", url)
        if len(parts) < 2:
            return None
        token = parts[1]
        first = token.split("A13h1")[0]
        padding = (4 - len(first) % 4) % 4
        decoded = base64.b64decode(first + "=" * padding).decode("ascii", errors="ignore")
        m = re.search(r"\d{4,}", decoded)
        return m.group() if m else None
    except Exception:
        return None


# ── Listing page parser ───────────────────────────────────────────────────────

def parse_listing_page(html: str, source: str) -> List[Dict]:
    """
    Parse one page of a CPPP listing.

    Expected columns:
      Sl.No | e-Published Date | Bid Closing Date | Opening Date |
      Title/Ref.No./Tender Id | Organisation (central) / State (state) | Corrigendum

    Returns list of tender dicts.
    """
    tree = HTMLParser(html)
    tenders = []

    # Find the main data table — must have at least 6 columns and header keywords
    target_table = None
    for table in tree.css("table"):
        header_text = table.text(strip=True).lower()
        if "published" in header_text and (
            "closing" in header_text
            or "organisation" in header_text
            or "state" in header_text
        ):
            target_table = table
            break

    if not target_table:
        log_scraper.warning(f"[{source}] No listing table found on page")
        return []

    rows = target_table.css("tbody tr") or target_table.css("tr")
    for row in rows:
        cells = row.css("td")
        if len(cells) < 5:
            continue

        published_date = _clean(cells[1].text(strip=True))
        closing_date   = _clean(cells[2].text(strip=True))
        opening_date   = _clean(cells[3].text(strip=True))

        title_cell = cells[4]
        link       = title_cell.css_first("a")

        detail_url = None
        title_text = None

        if link:
            href = link.attributes.get("href", "")
            if href:
                detail_url = (
                    f"{CPPP_BASE_URL}{href}" if not href.startswith("http") else href
                )
            title_text = _clean(link.text(strip=True))

        if not title_text:
            title_text = _clean(title_cell.text(strip=True))
        if not title_text:
            continue

        # Skip header rows
        if any(kw in title_text.lower() for kw in
               ["title", "tender id", "ref.no", "s.no", "serial no"]):
            continue

        org_or_state = _clean(cells[5].text(strip=True)) if len(cells) > 5 else None

        # Derive stable ref_no from the detail URL token
        ref_no = None
        if detail_url:
            ref_no = _extract_cppp_ref_no(detail_url)
        if not ref_no:
            # Fall back to title text — it is typically the official file/tender number
            ref_no = title_text

        tenders.append({
            "source":         source,
            "title":          title_text,
            "ref_no":         ref_no,
            "published_date": published_date,
            "closing_date":   closing_date,
            "opening_date":   opening_date,
            "organisation":   org_or_state,
            "url":            detail_url,
        })

    return tenders


# ── Detail page parser ────────────────────────────────────────────────────────

def parse_detail_page(html: str) -> Dict:
    """
    Parse a CPPP tender detail page.
    Builds a label→value map from all table rows (th/td and td/td patterns)
    then maps known field names to our schema.
    Returns dict of non-None fields only.
    """
    tree = HTMLParser(html)

    # Abort on error pages
    body = tree.css_first("body")
    if body and "invalid url" in body.text(strip=True).lower():
        return {}

    label_map: Dict[str, str] = {}

    for table in tree.css("table"):
        for row in table.css("tr"):
            ths = row.css("th")
            tds = row.css("td")

            # th→td pairs
            if ths and tds:
                for i, th in enumerate(ths):
                    label = _clean(th.text(strip=True))
                    if label and i < len(tds):
                        val = _clean(tds[i].text(strip=True))
                        if val:
                            label_map[label.lower()] = val

            # td→td pairs where first td looks like a label
            if len(tds) >= 2:
                for i in range(0, len(tds) - 1, 2):
                    cls = (tds[i].attributes.get("class") or "").lower()
                    if any(k in cls for k in ("label", "caption", "key", "head")):
                        label = _clean(tds[i].text(strip=True))
                        val   = _clean(tds[i + 1].text(strip=True))
                        if label and val:
                            label_map[label.lower()] = val

    def get(*searches: str) -> Optional[str]:
        for s in searches:
            s_lower = s.lower()
            for key, val in label_map.items():
                if s_lower in key:
                    return val
        return None

    result: Dict = {}
    result["tender_id"]          = get("tender id", "tender no", "nit no", "ref no")
    result["tender_type"]        = get("tender type")
    result["tender_category"]    = get("tender category", "category")
    result["contract_type"]      = get("form of contract", "contract type")
    result["work_description"]   = get("work description", "description of work", "name of work")
    result["product_category"]   = get("product category")
    result["sub_category"]       = get("sub category", "sub-category")
    result["location"]           = get("location", "place of work", "district")
    result["pincode"]            = get("pincode", "pin code")
    result["organisation"]       = get("organisation", "organization", "department", "ministry")
    result["state"]              = get("state name", "state")
    result["tender_value"]       = _parse_amount(get("tender value", "estimated cost", "nit amount"))
    result["emd_amount"]         = _parse_amount(get("emd amount", "earnest money", "bid security"))
    result["period_of_work_days"] = _parse_int(get("period of work", "completion period"))
    result["bid_validity_days"]   = _parse_int(get("bid validity"))
    result["pre_bid_meeting_date"]        = get("pre bid meeting date", "pre-bid meeting")
    result["inviting_authority_name"]     = get("inviting authority", "name")
    result["inviting_authority_address"]  = get("address")

    return {k: v for k, v in result.items() if v is not None}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _get_page(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """
    Fetch URL with retry + exponential backoff.
    Handles server disconnects, timeouts, and 429 rate-limits.
    Returns HTML text or None after all retries are exhausted.
    """
    for attempt in range(REQUEST_RETRIES):
        try:
            resp = await client.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)

            if resp.status_code == 200:
                return resp.text

            if resp.status_code == 429:
                delay = 10 * (2 ** attempt)
                log_scraper.warning(f"429 rate-limited — backing off {delay}s ({url[:60]})")
                await asyncio.sleep(delay)
                continue

            log_scraper.warning(f"HTTP {resp.status_code} for {url[:70]}")
            return None

        except (httpx.TimeoutException, httpx.NetworkError,
                httpx.RemoteProtocolError, httpx.ConnectError) as exc:
            if attempt < REQUEST_RETRIES - 1:
                delay = 3 * (2 ** attempt)   # 3s, 6s, 12s
                log_scraper.warning(
                    f"Connection error (attempt {attempt + 1}/{REQUEST_RETRIES}), "
                    f"retry in {delay}s — {type(exc).__name__} on {url[:60]}"
                )
                await asyncio.sleep(delay)
            else:
                log_scraper.warning(
                    f"Gave up after {REQUEST_RETRIES} attempts — {url[:60]}"
                )
                return None

    return None


# ── Helpers for live tender logging ──────────────────────────────────────────

def _log_tenders(tenders: List[Dict], source: str, known: Set[str]) -> None:
    """Log one line per tender as it arrives — visible in real-time Docker logs."""
    for t in tenders:
        is_new = not known or t.get("ref_no") not in known
        flag   = "NEW" if is_new else "   "
        title  = (t.get("title") or "")[:55]
        org    = (t.get("organisation") or "—")[:28]
        closes = t.get("closing_date") or "?"
        log_scraper.debug(f"[{source}] {flag}  {title:<55}  {org:<28}  closes {closes}")


# ── Listing scraper ───────────────────────────────────────────────────────────

async def scrape_listing(
    source: str,
    client: httpx.AsyncClient,
    known_ref_nos: Optional[Set[str]] = None,
    early_stop_pages: int = 3,
    on_page: Optional[Callable[[List[Dict]], Awaitable[None]]] = None,
) -> Dict:
    """
    Scrape all pages of a CPPP listing in parallel.

    Page 0 is fetched first to discover the total page count, then all
    remaining pages are fanned out concurrently (LISTING_PAGE_CONCURRENCY).
    Results are processed via asyncio.as_completed() so checkpoint writes
    and live logs happen as each page lands — not after all pages finish.

    Early stopping still works: a sorted buffer tracks consecutive pages
    where every ref_no is already in the DB; once `early_stop_pages`
    consecutive pages are fully known, remaining fetches are cancelled.

    Args:
        source:           "central" or "state"
        client:           Shared httpx.AsyncClient (also used for detail pages)
        known_ref_nos:    ref_nos already in DB
        early_stop_pages: Cancel remaining fetches after this many consecutive
                          all-known pages (in page-number order)
        on_page:          Async checkpoint callback per page

    Returns:
        {"success": bool, "tenders": List[Dict], "total_pages": int,
         "total_count": Optional[int], "error": Optional[str]}
    """
    base_url = SOURCES[source]
    known    = known_ref_nos or set()

    # ── Page 0: discover total page count ────────────────────────────────────
    html = await _get_page(f"{base_url}?page=0", client)
    if not html:
        return {
            "success": False,
            "error":   f"Failed to fetch {source} listing page 0",
            "tenders": [], "total_pages": 0,
        }

    total_count   = _parse_total_count(html)
    page_0_tender = parse_listing_page(html, source)

    _log_tenders(page_0_tender, source, known)
    if on_page and page_0_tender:
        try:
            await on_page(page_0_tender)
        except Exception as e:
            log_scraper.warning(f"[{source}] on_page failed (page 0): {e}")

    if not total_count:
        # Rare: can't determine total — fall back to sequential
        log_scraper.warning(f"[{source}] Total count unknown — using sequential fallback")
        return await _scrape_sequential(
            source, client, known, early_stop_pages, on_page, page_0_tender
        )

    total_pages = (total_count + 9) // 10
    log_scraper.info(
        f"[{source}] {total_count:,} tenders, ~{total_pages} pages — "
        f"parallel fetch (concurrency={LISTING_PAGE_CONCURRENCY})"
    )

    # ── Fan out remaining pages ───────────────────────────────────────────────
    sem        = asyncio.Semaphore(LISTING_PAGE_CONCURRENCY)
    stop_event = asyncio.Event()

    async def _fetch(pn: int) -> Tuple[int, List[Dict]]:
        if stop_event.is_set():
            return pn, []
        async with sem:
            if stop_event.is_set():
                return pn, []
            try:
                html = await _get_page(f"{base_url}?page={pn}", client)
                return pn, (parse_listing_page(html, source) if html else [])
            except Exception as exc:
                log_scraper.warning(f"[{source}] Page {pn} unexpected error: {exc}")
                return pn, []

    tasks = [asyncio.create_task(_fetch(pn)) for pn in range(1, total_pages)]

    all_tenders  = list(page_0_tender)
    pages_done   = 0
    active       = 0   # tasks currently inside the semaphore
    # Buffer for ordered early-stop check (pages arrive out of order)
    buf: Dict[int, List[Dict]] = {}
    next_check = 1
    streak     = 0

    for coro in asyncio.as_completed(tasks):
        try:
            pn, tenders = await coro
        except Exception as exc:
            log_scraper.warning(f"[{source}] Task raised unexpectedly: {exc}")
            pages_done += 1
            continue

        pages_done += 1
        # active = tasks queued or running (not yet returned)
        active = len(tasks) - pages_done

        if tenders:
            _log_tenders(tenders, source, known)
            if on_page:
                try:
                    await on_page(tenders)
                except Exception as e:
                    log_scraper.warning(f"[{source}] on_page failed (page {pn}): {e}")
            all_tenders.extend(tenders)

        # Progress line every 25 pages
        if pages_done % 25 == 0 or pages_done == total_pages - 1:
            pct = pages_done / max(total_pages - 1, 1) * 100
            log_scraper.info(
                f"[{source}]  page {pn:>5}  |  {pages_done}/{total_pages - 1} done "
                f"({pct:.0f}%)  |  {active} in-flight  |  "
                f"{len(all_tenders):,} tenders collected"
            )

        # Early-stop: drain the ordered buffer to check consecutive known pages
        if known:
            buf[pn] = tenders
            while next_check in buf:
                pt    = buf.pop(next_check)
                valid = [t for t in pt if t.get("ref_no") and t["ref_no"] != "UNKNOWN"]
                if valid and all(t["ref_no"] in known for t in valid):
                    streak += 1
                else:
                    streak = 0

                if streak >= early_stop_pages:
                    log_scraper.warning(
                        f"[{source}] Early stop — {early_stop_pages} consecutive "
                        f"all-known pages at page {next_check} "
                        f"({len(all_tenders):,} tenders)"
                    )
                    stop_event.set()
                    run_summary.record(f"early_stop_{source}", True)
                    break
                next_check += 1

    for t in tasks:
        t.cancel()

    log_scraper.success(
        f"[{source}] Done — {len(all_tenders):,} tenders, {pages_done} pages fetched"
    )
    run_summary.record(f"listing_tenders_{source}", len(all_tenders))
    run_summary.record(f"listing_pages_{source}", pages_done)

    return {
        "success":     True,
        "tenders":     all_tenders,
        "total_pages": pages_done,
        "total_count": total_count,
    }


async def _scrape_sequential(
    source: str,
    client: httpx.AsyncClient,
    known: Set[str],
    early_stop_pages: int,
    on_page: Optional[Callable],
    initial_tenders: List[Dict],
) -> Dict:
    """Sequential fallback used when total_count is unavailable from page 0."""
    all_tenders = list(initial_tenders)
    streak  = 0
    page_num = 1

    while True:
        html = await _get_page(f"{SOURCES[source]}?page={page_num}", client)
        if not html:
            break

        tenders = parse_listing_page(html, source)
        if not tenders:
            break

        _log_tenders(tenders, source, known)
        if on_page:
            try:
                await on_page(tenders)
            except Exception as e:
                log_scraper.warning(f"[{source}] on_page failed (page {page_num}): {e}")

        all_tenders.extend(tenders)

        if known:
            valid = [t for t in tenders if t.get("ref_no") and t["ref_no"] != "UNKNOWN"]
            if valid and all(t["ref_no"] in known for t in valid):
                streak += 1
                if streak >= early_stop_pages:
                    run_summary.record(f"early_stop_{source}", True)
                    break
            else:
                streak = 0

        page_num += 1

    run_summary.record(f"listing_tenders_{source}", len(all_tenders))
    run_summary.record(f"listing_pages_{source}", page_num)
    return {
        "success": True, "tenders": all_tenders,
        "total_pages": page_num, "total_count": None,
    }


# ── Detail page fetcher ───────────────────────────────────────────────────────

async def _fetch_one_detail(
    url: str,
    ref_no: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> Tuple[str, str, Dict]:
    """Fetch and parse one detail page. Returns (url, ref_no, fields)."""
    async with sem:
        html = await _get_page(url, client)
        if not html:
            return url, ref_no, {}
        fields = parse_detail_page(html)
        if fields:
            val = fields.get("tender_value")
            val_str = f"₹{val:,.0f}" if val else "value=?"
            cat = (fields.get("product_category") or fields.get("tender_category") or "—")[:22]
            loc = (fields.get("location") or "—")[:20]
            log_scraper.debug(
                f"[detail]  {ref_no[:38]:<38}  {val_str:<14}  {cat:<22}  {loc}"
            )
        else:
            log_scraper.debug(f"[detail]  {ref_no[:38]}  (no fields)")
        return url, ref_no, fields


async def fetch_all_details(
    url_to_ref: Dict[str, str],
    client: httpx.AsyncClient,
) -> Dict[str, Dict]:
    """
    Concurrently fetch all detail pages using the same AsyncClient.
    Session cookies from the listing scrape are preserved automatically.

    Returns: {ref_no: fields_dict}
    """
    if not url_to_ref:
        return {}

    sem   = asyncio.Semaphore(DETAIL_CONCURRENCY)
    tasks = [
        _fetch_one_detail(url, ref_no, client, sem)
        for url, ref_no in url_to_ref.items()
    ]

    results: Dict[str, Dict] = {}
    for coro in asyncio.as_completed(tasks):
        _, ref_no, fields = await coro
        results[ref_no] = fields

    total_with_data = sum(1 for v in results.values() if v)
    log_scraper.success(
        f"Detail fetch done — {total_with_data}/{len(url_to_ref)} had data"
    )
    run_summary.record("detail_pages_fetched", len(url_to_ref))
    run_summary.record("detail_pages_with_data", total_with_data)
    return results
