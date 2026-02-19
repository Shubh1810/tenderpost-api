"""
scraper.py — Main scraping pipeline using Playwright + 2Captcha + Selectolax.

Architecture (unchanged):
- Playwright : Browser automation, navigation, CAPTCHA, pagination
- 2Captcha   : CAPTCHA solving
- Selectolax : Fast HTML parsing

What's new (additive only):
- After extracting each tender from the list page, Playwright clicks into the
  tender detail page within the SAME session/tab, scrapes rich fields, then
  clicks the Back anchor to return to the list page.
- No new browser, no new session, no new CAPTCHA.

Key HTML facts confirmed from source inspection:
- Detail page: all data lives in <table class="tablebg"> — NOT in the nav sidebar.
  The left sidebar also contains <script> tags and nav links — scoping to
  tablebg avoids all contamination.
- Labels: <td class="td_caption">, values: <td class="td_field">
- Multiple label+value pairs share a single <tr>:
    <tr>
      <td class="td_caption">Tender Value in ₹</td><td class="td_field">3,86,15,626</td>
      <td class="td_caption">Product Category</td><td class="td_field">Electrical Works</td>
      <td class="td_caption">Sub category</td><td class="td_field">NA</td>
    </tr>
- Back button is an <a> anchor, confirmed:
    <a id="DirectLink_11" title="Back" class="customButton_link" value="Back" href="...">Back</a>
- List page tender links: id="DirectLink_0", "DirectLink_0_0" etc.
  with href containing sp= session token unique per tender.
"""

import asyncio
import os
import re
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Page, async_playwright
from selectolax.parser import HTMLParser

from captcha.screenshot import capture_captcha_screenshot, detect_captcha_presence
from captcha.solver import solve_captcha_2captcha


# ── Target URLs ───────────────────────────────────────────────────────────────
ADVANCED_SEARCH_URL = "https://eprocure.gov.in/eprocure/app?page=FrontEndAdvancedSearch&service=page"
LATEST_TENDERS_URL  = "https://eprocure.gov.in/eprocure/app?page=FrontEndLatestActiveTenders&service=page"

# ── Form selectors (unchanged) ────────────────────────────────────────────────
TENDER_TYPE_SELECTORS = [
    'select[name="TenderType"]',
    'select[name="tenderType"]',
    'select[id*="TenderType"]',
    'select:has(option[value="1"])',
]
CAPTCHA_INPUT_SELECTOR = 'input[name="captchaText"]'
SUBMIT_BUTTON_SELECTOR = 'input[type="submit"][value="Search"]'
RESULTS_TABLE_SELECTOR = "table.list tbody tr"

# ── Limits ────────────────────────────────────────────────────────────────────
MAX_PAGES    = int(os.getenv("MAX_PAGES", "200"))
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "30000"))

# Set SCRAPE_DETAILS=false in .env to disable detail scraping (fast debug mode)
SCRAPE_DETAILS = os.getenv("SCRAPE_DETAILS", "true").lower() == "true"


# ── Logging ───────────────────────────────────────────────────────────────────

def log_step(step: str, status: str, details: Dict) -> None:
    emoji = {"success": "✅", "error": "❌", "warning": "⚠️",
             "initiated": "🔵", "processing": "⏳"}.get(status, "📌")
    print(f"{emoji} [{step}] {status.upper()}")
    for k, v in details.items():
        print(f"   • {k}: {v}")


# ── Data model ────────────────────────────────────────────────────────────────

class TenderItem:
    """
    Holds both list-page fields AND detail-page fields in one object.
    Detail fields default to None — populated when SCRAPE_DETAILS=true.
    """

    def __init__(
        self,
        title: str,
        ref_no: Optional[str]                     = None,
        closing_date: Optional[str]               = None,
        opening_date: Optional[str]               = None,
        published_date: Optional[str]             = None,
        organisation: Optional[str]               = None,
        url: Optional[str]                        = None,
        # ── detail-page fields ──
        tender_id: Optional[str]                  = None,
        tender_type: Optional[str]                = None,
        tender_category: Optional[str]            = None,
        contract_type: Optional[str]              = None,
        work_description: Optional[str]           = None,
        product_category: Optional[str]           = None,
        sub_category: Optional[str]               = None,
        location: Optional[str]                   = None,
        pincode: Optional[str]                    = None,
        tender_value: Optional[float]             = None,
        emd_amount: Optional[float]               = None,
        period_of_work_days: Optional[int]        = None,
        bid_validity_days: Optional[int]          = None,
        pre_bid_meeting_date: Optional[str]       = None,
        inviting_authority_name: Optional[str]    = None,
        inviting_authority_address: Optional[str] = None,
    ):
        self.title                      = title
        self.ref_no                     = ref_no
        self.closing_date               = closing_date
        self.opening_date               = opening_date
        self.published_date             = published_date
        self.organisation               = organisation
        self.url                        = url
        self.tender_id                  = tender_id
        self.tender_type                = tender_type
        self.tender_category            = tender_category
        self.contract_type              = contract_type
        self.work_description           = work_description
        self.product_category           = product_category
        self.sub_category               = sub_category
        self.location                   = location
        self.pincode                    = pincode
        self.tender_value               = tender_value
        self.emd_amount                 = emd_amount
        self.period_of_work_days        = period_of_work_days
        self.bid_validity_days          = bid_validity_days
        self.pre_bid_meeting_date       = pre_bid_meeting_date
        self.inviting_authority_name    = inviting_authority_name
        self.inviting_authority_address = inviting_authority_address

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}


# ── Unchanged helpers ─────────────────────────────────────────────────────────

def parse_title_and_ref(title_and_ref: str) -> Tuple[str, str]:
    parts = title_and_ref.split("]")
    if len(parts) >= 3:
        title  = parts[0].lstrip("[").strip()
        ref_no = parts[1].lstrip("[").strip()
        return title, ref_no
    return title_and_ref.strip(), ""


def is_valid_tender(title, closing_date, opening_date, published_date, organisation) -> bool:
    header_patterns = [
        "search", "|", "eprocurement system", "s.no", "serial",
        "government of india", "tender id", "ref.no", "organisation chain",
        "closing date", "opening date", "published date", "e-published date",
        "bid closing", "bid opening",
    ]
    title_lower = title.lower()
    if any(p in title_lower for p in header_patterns):
        return False
    if closing_date.lower()   in ["closing date", "close date", "bid closing", "deadline"]:
        return False
    if opening_date.lower()   in ["opening date", "open date", "bid opening"]:
        return False
    if published_date.lower() in ["published date", "e-published date", "publication"]:
        return False
    if organisation.lower()   in ["organisation chain", "organization", "department", "ministry"]:
        return False
    if len(title) <= 20 or not any(c.isalpha() for c in title):
        return False
    tender_keywords = [
        "supply", "procurement", "tender", "contract", "services", "work",
        "construction", "equipment", "purchase", "hiring", "repair",
        "maintenance", "installation", "consultancy",
    ]
    return any(kw in title_lower for kw in tender_keywords) or len(title.split()) > 5


async def handle_captcha_if_present(page: Page) -> bool:
    log_step("CAPTCHA Handler", "initiated", {"action": "Checking for CAPTCHA"})
    try:
        if not await detect_captcha_presence(page):
            log_step("CAPTCHA Handler", "success", {"status": "No CAPTCHA detected"})
            return True

        log_step("CAPTCHA Handler", "processing", {"status": "CAPTCHA detected, capturing..."})
        captcha_base64 = await capture_captcha_screenshot(page, enhance=True)
        if not captcha_base64:
            log_step("CAPTCHA Handler", "error", {"error": "Failed to capture CAPTCHA image"})
            return False

        solution_result = await solve_captcha_2captcha(captcha_base64)
        if not solution_result.get("success"):
            log_step("CAPTCHA Handler", "error",
                     {"error": solution_result.get("error", "Unknown")})
            return False

        solution_text = solution_result.get("solution", "")
        log_step("CAPTCHA Handler", "success",
                 {"solution": solution_text,
                  "elapsed": f"{solution_result.get('elapsed_time', 0):.1f}s"})
        await page.fill(CAPTCHA_INPUT_SELECTOR, solution_text)
        return True

    except Exception as e:
        log_step("CAPTCHA Handler", "error", {"error": str(e)})
        return False


async def find_exact_next_button(page: Page) -> Optional[object]:
    try:
        for link in await page.query_selector_all("a"):
            try:
                text = await link.inner_text()
                if text.strip() == ">":
                    html = await link.evaluate("el => el.outerHTML")
                    if ">>" not in html and text.count(">") == 1:
                        return link
            except Exception:
                continue
        return None
    except Exception as e:
        log_step("Pagination", "error", {"error": str(e)})
        return None


async def extract_live_tenders_count(page: Page) -> Optional[int]:
    try:
        for row in reversed(await page.query_selector_all("table tr:has(td)")):
            try:
                cells = await row.query_selector_all("td")
                if len(cells) >= 5:
                    s_no = (await cells[0].inner_text()).strip().rstrip(".")
                    if s_no.isdigit():
                        return int(s_no)
            except Exception:
                continue
        return None
    except Exception:
        return None


def extract_tenders_from_html(html_content: str) -> List[TenderItem]:
    """Unchanged — parses list-page HTML into TenderItem list."""
    tenders = []
    try:
        tree = HTMLParser(html_content)
        # The list page uses id="table" on the results table (confirmed from HTML source)
        rows = tree.css("table#table tr")
        if not rows:
            rows = tree.css("table.list_table tr")
        if not rows:
            rows = tree.css("table.list tbody tr")
        if not rows:
            log_step("Extraction", "warning", {"message": "No table rows found"})
            return tenders

        header_keywords = [
            "s.no", "serial", "published", "closing", "opening",
            "title", "ref.no", "tender id", "organisation chain",
        ]
        start_index = 0
        for idx in range(min(5, len(rows))):
            if any(kw in rows[idx].text().lower() for kw in header_keywords):
                start_index = idx + 1
            else:
                break

        for row in rows[start_index:]:
            row_text = row.text()
            if not row_text or len(row_text.strip()) < 20:
                continue
            cells = row.css("td")
            if len(cells) < 5:
                continue

            published_date = cells[1].text().strip()
            closing_date   = cells[2].text().strip()
            opening_date   = cells[3].text().strip()
            title_and_ref  = cells[4].text().strip()
            organisation   = cells[5].text().strip() if len(cells) > 5 else ""

            title, ref_no = parse_title_and_ref(title_and_ref)

            tender_url = None
            link = cells[4].css_first("a")
            if link:
                href = link.attributes.get("href", "")
                if href:
                    tender_url = (
                        f"https://eprocure.gov.in{href}"
                        if not href.startswith("http") else href
                    )

            if not is_valid_tender(title, closing_date, opening_date,
                                   published_date, organisation):
                continue

            tenders.append(TenderItem(
                title=title,
                ref_no=ref_no,
                closing_date=closing_date,
                opening_date=opening_date,
                published_date=published_date,
                organisation=organisation,
                url=tender_url,
            ))

    except Exception as e:
        log_step("Extraction", "error", {"error": str(e)})

    return tenders


# ── Detail page parsing (completely rewritten, precision-targeted) ─────────────

def _clean(val: Optional[str]) -> Optional[str]:
    """Normalise whitespace, reject empty/NA/JS-looking values."""
    if not val:
        return None
    val = val.replace("\xa0", " ").strip()
    if not val or val in ("NA", "N/A", "-", "&nbsp;"):
        return None
    # Safety net: reject anything that looks like JS leaked through
    if "function" in val or "window.open" in val or len(val) > 600:
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


def _sibling_value(caption_node, offset: int = 1) -> Optional[str]:
    """
    Given a <td class="td_caption"> Selectolax node, return the cleaned text
    of the td that is `offset` siblings after it within the same <tr>.

    This works because the site always lays out rows as:
        [caption][field][caption][field][caption][field]
    where each pair shares one <tr>. The field we want is always offset=1
    from its caption, regardless of how many pairs are in the row.
    """
    parent = caption_node.parent  # the <tr>
    if not parent:
        return None
    all_tds = parent.css("td")
    for i, td in enumerate(all_tds):
        # Node identity comparison via their HTML
        if td.html == caption_node.html:
            target = i + offset
            if target < len(all_tds):
                return _clean(all_tds[target].text(strip=True))
    return None


def _find_caption_in_content(tree: HTMLParser, label: str) -> Optional[object]:
    """
    Find the first <td class="td_caption"> whose text contains `label`,
    searching ONLY inside <table class="tablebg"> elements.

    Why tablebg? The detail page has:
      - Left sidebar <td class="navigation"> with a <script> popup() function
      - Content area tables that all carry class="tablebg"
    Scoping to tablebg completely eliminates sidebar/script contamination.
    """
    label_lower = label.lower().strip()
    for table in tree.css("table.tablebg"):
        for td in table.css("td.td_caption"):
            if label_lower in td.text(strip=True).lower():
                return td
    return None


def parse_detail_page(html: str) -> Dict:
    """
    Parse a tender detail page. Returns dict of all non-None scraped fields.

    All lookups are scoped to table.tablebg to avoid sidebar contamination.
    """
    tree   = HTMLParser(html)
    result = {}

    def get(label: str, offset: int = 1) -> Optional[str]:
        td = _find_caption_in_content(tree, label)
        return _sibling_value(td, offset) if td else None

    # ── Basic Details section ─────────────────────────────────────────────────
    result["tender_id"]       = get("Tender ID")
    result["tender_type"]     = get("Tender Type")
    result["tender_category"] = get("Tender Category")
    # Confirmed label in source: "Form Of Contract" (capital O)
    result["contract_type"]   = get("Form Of Contract") or get("Form of contract")

    # ── Work Item Details section ─────────────────────────────────────────────
    result["work_description"] = get("Work Description")

    # Tender Value row (label contains "Tender Value in" + ₹ entity)
    # Confirmed structure: td_caption="Tender Value in ₹ " → td_field="3,86,15,626"
    # We match on "tender value in" prefix to handle encoding differences
    tv_td = _find_caption_in_content(tree, "Tender Value in")
    if tv_td:
        result["tender_value"] = _parse_amount(_sibling_value(tv_td, 1))

    # Product Category, Sub category — same row as Tender Value
    result["product_category"] = get("Product Category")
    result["sub_category"]     = get("Sub category")

    # Contract Type / Bid Validity / Period of Work — same row
    result["bid_validity_days"]   = _parse_int(get("Bid Validity"))
    result["period_of_work_days"] = _parse_int(get("Period Of Work"))

    # Location / Pincode / Pre Bid Meeting Place — same row
    result["location"] = get("Location")
    result["pincode"]  = get("Pincode")

    # Pre Bid Meeting Date
    result["pre_bid_meeting_date"] = get("Pre Bid Meeting Date")

    # ── EMD Fee Details section ───────────────────────────────────────────────
    # Label: "EMD Amount in ₹" — match on prefix
    emd_td = _find_caption_in_content(tree, "EMD Amount in")
    if emd_td:
        result["emd_amount"] = _parse_amount(_sibling_value(emd_td, 1))

    # ── Tender Inviting Authority section ─────────────────────────────────────
    # "Name" and "Address" labels exist here — both are <td class="td_caption">
    # inside a tablebg table at the bottom of the page
    result["inviting_authority_name"]    = get("Name")
    result["inviting_authority_address"] = get("Address")

    # Return only fields that have a real value
    return {k: v for k, v in result.items() if v is not None}


# ── Detail scraping with Playwright ──────────────────────────────────────────

async def scrape_detail_page_playwright(page: Page, tender: TenderItem) -> None:
    """
    Navigate into a tender detail page within the SAME Playwright session,
    parse fields with Selectolax, then click the Back anchor to return.

    Navigation strategy:
    1. Extract the sp= token from the tender's stored URL.
    2. Find the matching <a href="...sp=TOKEN..."> on the current list page.
    3. Click it — keeps the Tapestry session perfectly intact.
    4. Fallback to page.goto(url) if the link isn't found.

    Back navigation (confirmed from HTML):
      <a id="DirectLink_11" title="Back" class="customButton_link" href="...">Back</a>
    """
    if not tender.url:
        return

    ref_short = (tender.ref_no or "")[:35]
    log_step("Detail Scrape", "initiated", {"ref": ref_short})

    try:
        # ── Navigate in ──────────────────────────────────────────────────────
        sp_match  = re.search(r"sp=([^&]+)", tender.url)
        navigated = False

        if sp_match:
            sp_token = sp_match.group(1)
            link_el  = await page.query_selector(f'a[href*="sp={sp_token}"]')
            if link_el:
                await link_el.click()
                navigated = True

        if not navigated:
            # Fallback: direct navigation, session cookie still intact
            await page.goto(tender.url, wait_until="domcontentloaded", timeout=15000)

        # Wait for the data tables to appear
        try:
            await page.wait_for_selector("table.tablebg", timeout=10000)
        except Exception:
            pass

        await asyncio.sleep(0.8)

        # ── Parse ────────────────────────────────────────────────────────────
        html   = await page.content()
        fields = parse_detail_page(html)

        for key, val in fields.items():
            if hasattr(tender, key):
                setattr(tender, key, val)

        log_step("Detail Scrape", "success", {
            "ref":          ref_short,
            "fields_found": len(fields),
            "category":     fields.get("product_category", "N/A"),
            "value":        fields.get("tender_value", "N/A"),
            "location":     fields.get("location", "N/A"),
        })

        # ── Click Back anchor ─────────────────────────────────────────────────
        # Confirmed: <a title="Back" class="customButton_link" ...>Back</a>
        # NOT an <input type="button"> — it's an anchor.
        back_clicked = False

        # Primary selector — most reliable, matches the confirmed HTML exactly
        back_el = await page.query_selector('a[title="Back"]')
        if back_el:
            await back_el.click()
            back_clicked = True

        # Secondary: class match
        if not back_clicked:
            for a in await page.query_selector_all("a.customButton_link"):
                try:
                    if (await a.inner_text()).strip().lower() == "back":
                        await a.click()
                        back_clicked = True
                        break
                except Exception:
                    continue

        # Tertiary: text match on any anchor
        if not back_clicked:
            for a in await page.query_selector_all("a"):
                try:
                    if (await a.inner_text()).strip() == "Back":
                        await a.click()
                        back_clicked = True
                        break
                except Exception:
                    continue

        if not back_clicked:
            log_step("Detail Scrape", "warning",
                     {"action": "Back anchor not found, using page.go_back()"})
            await page.go_back()

        # Wait for list table to reappear before continuing
        try:
            await page.wait_for_selector("table#table, table.list_table", timeout=10000)
        except Exception:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass

        await asyncio.sleep(1.0)

    except Exception as e:
        log_step("Detail Scrape", "error", {"ref": ref_short, "error": str(e)})
        try:
            await page.go_back()
            await asyncio.sleep(1.5)
        except Exception:
            pass


# ── Main scraping pipeline ────────────────────────────────────────────────────

async def scrape_tenders_crawl4ai_playwright() -> Dict:
    """
    Complete pipeline: list scrape + inline detail scrape in one browser session.

    Returns:
        {
            "success":      bool,
            "tenders":      List[Dict],
            "total_pages":  int,
            "live_tenders": Optional[int],
        }
    """
    log_step("Scraper Pipeline", "initiated",
             {"framework": "Playwright + 2Captcha + Selectolax",
              "detail_scraping": SCRAPE_DETAILS})

    tenders_list: List[TenderItem] = []
    current_page = 0
    total_live   = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            # STEP 1: Load search form
            log_step("Navigation", "processing", {"url": ADVANCED_SEARCH_URL})
            await page.goto(ADVANCED_SEARCH_URL, wait_until="networkidle",
                            timeout=PAGE_TIMEOUT)
            await page.wait_for_selector(CAPTCHA_INPUT_SELECTOR, timeout=10000)
            log_step("Navigation", "success", {"status": "Form loaded"})

            # STEP 2: Solve CAPTCHA
            if not await handle_captcha_if_present(page):
                await browser.close()
                return {"success": False, "error": "CAPTCHA solving failed",
                        "tenders": []}

            # STEP 3: Select tender type and submit
            for sel in TENDER_TYPE_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await page.select_option(sel, value="1")
                        log_step("Form Filling", "success",
                                 {"tender_type": "Open Tender"})
                        break
                except Exception:
                    continue

            await page.click(SUBMIT_BUTTON_SELECTOR)
            await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
            log_step("Form Submission", "success", {"status": "Results loaded"})

            # STEP 4: Paginate, extract, detail-scrape
            while True:
                current_page += 1
                html         = await page.content()
                page_tenders = extract_tenders_from_html(html)

                log_step("Page Extraction", "success", {
                    "page":    current_page,
                    "tenders": len(page_tenders),
                    "running": len(tenders_list) + len(page_tenders),
                })

                if SCRAPE_DETAILS:
                    for i, tender in enumerate(page_tenders):
                        if tender.url:
                            log_step("Detail Progress", "processing", {
                                "progress": f"{i+1}/{len(page_tenders)} on page {current_page}",
                                "ref":      (tender.ref_no or "")[:35],
                            })
                            await scrape_detail_page_playwright(page, tender)
                            await asyncio.sleep(1.2)

                tenders_list.extend(page_tenders)

                if current_page >= MAX_PAGES:
                    log_step("Pagination", "success",
                             {"status": f"Reached MAX_PAGES={MAX_PAGES}"})
                    break

                next_btn = await find_exact_next_button(page)
                if not next_btn:
                    log_step("Pagination", "success",
                             {"status": "No more pages", "total": current_page})
                    break

                await next_btn.click()
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    try:
                        await page.wait_for_selector(
                            "table#table, table.list_table", timeout=15000)
                    except Exception:
                        pass

                await asyncio.sleep(2)

            # STEP 5: Count live tenders
            total_live = await extract_live_tenders_count(page)
            if total_live:
                log_step("Live Tenders Count", "success", {"count": total_live})

            await browser.close()

        log_step("Scraper Pipeline", "success", {
            "total_tenders":  len(tenders_list),
            "total_pages":    current_page,
            "live_tenders":   total_live or "N/A",
            "detail_scraped": SCRAPE_DETAILS,
        })

        return {
            "success":      True,
            "tenders":      [t.to_dict() for t in tenders_list],
            "total_pages":  current_page,
            "live_tenders": total_live,
        }

    except Exception as e:
        log_step("Scraper Pipeline", "error", {"error": str(e)})
        return {
            "success":     False,
            "error":       str(e),
            "tenders":     [t.to_dict() for t in tenders_list],
            "total_pages": current_page,
        }


# ── Latest active tenders (same inline detail scraping) ──────────────────────

async def scrape_latest_active_tenders() -> Dict:
    """Scrape latest active tenders page (no CAPTCHA required)."""
    log_step("Latest Tenders Scraper", "initiated", {"url": LATEST_TENDERS_URL})

    tenders_list: List[TenderItem] = []
    current_page = 0

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            )
            page    = await context.new_page()

            await page.goto(LATEST_TENDERS_URL, wait_until="networkidle",
                            timeout=PAGE_TIMEOUT)
            current_page = 1

            while True:
                html         = await page.content()
                page_tenders = extract_tenders_from_html(html)

                log_step("Page Extraction", "success", {
                    "page": current_page, "tenders": len(page_tenders),
                })

                if SCRAPE_DETAILS:
                    for i, tender in enumerate(page_tenders):
                        if tender.url:
                            log_step("Detail Progress", "processing", {
                                "progress": f"{i+1}/{len(page_tenders)} on page {current_page}",
                                "ref":      (tender.ref_no or "")[:35],
                            })
                            await scrape_detail_page_playwright(page, tender)
                            await asyncio.sleep(1.2)

                tenders_list.extend(page_tenders)

                if current_page >= MAX_PAGES:
                    break

                next_btn = await find_exact_next_button(page)
                if not next_btn:
                    break

                await next_btn.click()
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    try:
                        await page.wait_for_selector(
                            "table#table, table.list_table", timeout=15000)
                    except Exception:
                        pass

                await asyncio.sleep(2)
                current_page += 1

            await browser.close()

        log_step("Latest Tenders Scraper", "success", {
            "total_tenders": len(tenders_list),
            "total_pages":   current_page,
        })

        return {
            "success":     True,
            "tenders":     [t.to_dict() for t in tenders_list],
            "total_pages": current_page,
        }

    except Exception as e:
        log_step("Latest Tenders Scraper", "error", {"error": str(e)})
        return {
            "success":     False,
            "error":       str(e),
            "tenders":     [t.to_dict() for t in tenders_list],
            "total_pages": current_page,
        }