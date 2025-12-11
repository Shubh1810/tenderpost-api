"""
Main scraping pipeline using Playwright + 2Captcha.

Architecture:
- Playwright: Browser automation for navigation, CAPTCHA handling, and content extraction
- 2Captcha: CAPTCHA solving service
- Selectolax: Fast HTML parsing
"""

import asyncio
import os
import re
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Page, async_playwright
from selectolax.parser import HTMLParser

from captcha.screenshot import capture_captcha_screenshot, detect_captcha_presence
from captcha.solver import solve_captcha_2captcha


# Target URLs
ADVANCED_SEARCH_URL = "https://eprocure.gov.in/eprocure/app?page=FrontEndAdvancedSearch&service=page"
LATEST_TENDERS_URL = "https://eprocure.gov.in/eprocure/app?page=FrontEndLatestActiveTenders&service=page"

# Form selectors
TENDER_TYPE_SELECTORS = [
    'select[name="TenderType"]',
    'select[name="tenderType"]',
    'select[id*="TenderType"]',
    'select:has(option[value="1"])',
]
CAPTCHA_INPUT_SELECTOR = 'input[name="captchaText"]'
SUBMIT_BUTTON_SELECTOR = 'input[type="submit"][value="Search"]'

# Results selectors
RESULTS_TABLE_SELECTOR = "table.list tbody tr"
TENDER_DETAIL_LINK_SELECTORS = ['a[href*="TenderDetails"]', 'a[href*="FrontEndTenderDetails"]']

# Pagination
MAX_PAGES = int(os.getenv("MAX_PAGES", "200"))
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "30000"))


def log_step(step: str, status: str, details: Dict[str, any]) -> None:
    """Print detailed logs for each pipeline step."""
    status_emoji = {
        "success": "✅",
        "error": "❌",
        "warning": "⚠️",
        "initiated": "🔵",
        "processing": "⏳",
    }.get(status, "📌")

    print(f"{status_emoji} [{step}] {status.upper()}")
    for key, value in details.items():
        print(f"   • {key}: {value}")


class TenderItem:
    """Data model for a tender item."""

    def __init__(
        self,
        title: str,
        ref_no: Optional[str] = None,
        closing_date: Optional[str] = None,
        opening_date: Optional[str] = None,
        published_date: Optional[str] = None,
        organisation: Optional[str] = None,
        url: Optional[str] = None,
    ):
        self.title = title
        self.ref_no = ref_no
        self.closing_date = closing_date
        self.opening_date = opening_date
        self.published_date = published_date
        self.organisation = organisation
        self.url = url

    def to_dict(self) -> Dict[str, Optional[str]]:
        """Convert to dictionary for JSON serialization."""
        return {
            "title": self.title,
            "ref_no": self.ref_no,
            "closing_date": self.closing_date,
            "opening_date": self.opening_date,
            "published_date": self.published_date,
            "organisation": self.organisation,
            "url": self.url,
        }


def parse_title_and_ref(title_and_ref: str) -> Tuple[str, str]:
    """
    Parse combined field: [Title] [Ref.No.] [TenderID]

    Returns:
        (title, ref_no)
    """
    parts = title_and_ref.split("]")
    if len(parts) >= 3:
        title = parts[0].lstrip("[").strip()
        ref_no = parts[1].lstrip("[").strip()
        return title, ref_no
    return title_and_ref.strip(), ""


def is_valid_tender(
    title: str, closing_date: str, opening_date: str, published_date: str, organisation: str
) -> bool:
    """
    Validate tender row to exclude headers/separators.

    Returns:
        True if valid tender data, False if header/separator
    """
    # Exclusion patterns
    header_patterns = [
        "search",
        "|",
        "eprocurement system",
        "s.no",
        "serial",
        "government of india",
        "tender id",
        "ref.no",
        "organisation chain",
        "closing date",
        "opening date",
        "published date",
        "e-published date",
        "bid closing",
        "bid opening",
    ]

    # Check if title looks like a header
    title_lower = title.lower()
    if any(pattern in title_lower for pattern in header_patterns):
        return False

    # Check if date fields contain header text
    if closing_date.lower() in ["closing date", "close date", "bid closing", "deadline"]:
        return False
    if opening_date.lower() in ["opening date", "open date", "bid opening"]:
        return False
    if published_date.lower() in ["published date", "e-published date", "publication"]:
        return False
    if organisation.lower() in ["organisation chain", "organization", "department", "ministry"]:
        return False

    # Validate title content
    if len(title) <= 20:
        return False
    if not any(char.isalpha() for char in title):
        return False

    # Check for tender-related keywords OR detailed description
    tender_keywords = [
        "supply",
        "procurement",
        "tender",
        "contract",
        "services",
        "work",
        "construction",
        "equipment",
        "purchase",
        "hiring",
        "repair",
        "maintenance",
        "installation",
        "consultancy",
    ]

    if any(keyword in title_lower for keyword in tender_keywords) or len(title.split()) > 5:
        return True

    return False


async def handle_captcha_if_present(page: Page) -> bool:
    """
    Detect, capture, solve, and submit CAPTCHA using Playwright.

    Args:
        page: Playwright Page object

    Returns:
        True if CAPTCHA was solved successfully or not present, False if failed
    """
    log_step("CAPTCHA Handler", "initiated", {"action": "Checking for CAPTCHA"})

    try:
        # Check if CAPTCHA exists
        captcha_present = await detect_captcha_presence(page)
        if not captcha_present:
            log_step("CAPTCHA Handler", "success", {"status": "No CAPTCHA detected"})
            return True

        log_step("CAPTCHA Handler", "processing", {"status": "CAPTCHA detected, capturing..."})

        # Capture CAPTCHA image
        captcha_base64 = await capture_captcha_screenshot(page, enhance=True)
        if not captcha_base64:
            log_step("CAPTCHA Handler", "error", {"error": "Failed to capture CAPTCHA image"})
            return False

        # Solve using 2Captcha
        solution_result = await solve_captcha_2captcha(captcha_base64)

        if not solution_result.get("success"):
            log_step(
                "CAPTCHA Handler",
                "error",
                {"error": solution_result.get("error", "Unknown error")},
            )
            return False

        solution_text = solution_result.get("solution", "")
        log_step(
            "CAPTCHA Handler",
            "success",
            {
                "solution": solution_text,
                "elapsed": f"{solution_result.get('elapsed_time', 0):.1f}s",
            },
        )

        # Fill CAPTCHA input
        await page.fill(CAPTCHA_INPUT_SELECTOR, solution_text)
        log_step("CAPTCHA Handler", "success", {"action": "CAPTCHA solution filled"})

        return True

    except Exception as e:
        log_step("CAPTCHA Handler", "error", {"error": str(e)})
        return False


async def find_exact_next_button(page: Page) -> Optional[any]:
    """
    Find the EXACT ">" button (not ">>").

    Returns:
        Element handle or None if not found
    """
    try:
        all_links = await page.query_selector_all("a")

        for link in all_links:
            try:
                link_text = await link.inner_text()
                # STRICT: Must be EXACTLY ">"
                if link_text.strip() == ">":
                    # Double-check it's not part of ">>"
                    link_html = await link.evaluate("el => el.outerHTML")
                    if ">>" not in link_html and link_text.count(">") == 1:
                        return link
            except Exception:
                continue

        return None
    except Exception as e:
        log_step("Pagination", "error", {"error": f"Failed to find next button: {str(e)}"})
        return None


async def extract_live_tenders_count(page: Page) -> Optional[int]:
    """
    Extract total live tenders count from last page.
    Uses S.No from last valid tender row.

    Returns:
        Total count or None if not found
    """
    try:
        rows = await page.query_selector_all("table tr:has(td)")

        # Iterate backwards to find last valid row
        for row in reversed(rows):
            try:
                cells = await row.query_selector_all("td")
                if len(cells) >= 5:
                    s_no_text = await cells[0].inner_text()
                    s_no_cleaned = s_no_text.strip().rstrip(".")  # Remove trailing period

                    if s_no_cleaned.isdigit():
                        return int(s_no_cleaned)
            except Exception:
                continue

        return None
    except Exception as e:
        log_step("Live Tenders Count", "warning", {"error": str(e)})
        return None


def extract_tenders_from_html(html_content: str) -> List[TenderItem]:
    """
    Extract tender data from HTML using selectolax (fast parser).

    Args:
        html_content: Raw HTML content

    Returns:
        List of TenderItem objects
    """
    tenders = []

    try:
        tree = HTMLParser(html_content)
        rows = tree.css("table.list tbody tr")

        if not rows:
            log_step("Extraction", "warning", {"message": "No table rows found"})
            return tenders

        # Skip header rows (first 1-5 rows may be headers)
        start_index = 0
        header_keywords = [
            "s.no",
            "serial",
            "published",
            "closing",
            "opening",
            "title",
            "ref.no",
            "tender id",
            "organisation chain",
        ]

        for idx in range(min(5, len(rows))):
            row_text = rows[idx].text().lower()
            if any(keyword in row_text for keyword in header_keywords):
                start_index = idx + 1
            else:
                break

        # Extract data rows
        for row in rows[start_index:]:
            try:
                row_text = row.text()

                # Skip empty/separator rows
                if not row_text or len(row_text.strip()) < 20:
                    continue

                cells = row.css("td")
                if len(cells) < 5:
                    continue

                # Parse cells based on table structure
                # Column 0: S.No
                # Column 1: e-Published Date
                # Column 2: Closing Date
                # Column 3: Opening Date
                # Column 4: Title and Ref.No./Tender ID
                # Column 5: Organisation Chain

                s_no = cells[0].text().strip()
                published_date = cells[1].text().strip()
                closing_date = cells[2].text().strip()
                opening_date = cells[3].text().strip()
                title_and_ref = cells[4].text().strip()
                organisation = cells[5].text().strip() if len(cells) > 5 else ""

                # Parse combined "Title and Ref.No./Tender ID" field
                title, ref_no = parse_title_and_ref(title_and_ref)

                # Get tender detail URL
                tender_url = None
                link = cells[4].css_first("a")
                if link:
                    href = link.attributes.get("href", "")
                    if href:
                        tender_url = (
                            f"https://eprocure.gov.in{href}"
                            if not href.startswith("http")
                            else href
                        )

                # Validation
                if not is_valid_tender(title, closing_date, opening_date, published_date, organisation):
                    continue

                tenders.append(
                    TenderItem(
                        title=title,
                        ref_no=ref_no,
                        closing_date=closing_date,
                        opening_date=opening_date,
                        published_date=published_date,
                        organisation=organisation,
                        url=tender_url,
                    )
                )

            except Exception as e:
                # Skip problematic rows
                continue

        log_step("Extraction", "success", {"tenders_extracted": len(tenders)})

    except Exception as e:
        log_step("Extraction", "error", {"error": str(e)})

    return tenders


async def scrape_tenders_crawl4ai_playwright() -> Dict[str, any]:
    """
    Complete pipeline using Playwright + 2Captcha.

    Architecture Flow:
    1. Use Playwright for navigation to search page
    2. Use Playwright for CAPTCHA detection and solving (via 2Captcha)
    3. Use Playwright for form filling and submission
    4. Extract HTML directly from Playwright page
    5. Parse HTML with Selectolax for fast extraction
    6. Paginate through all pages using Playwright

    Returns:
        {
            "tenders": List[Dict],
            "total_pages": int,
            "live_tenders": Optional[int],
            "success": bool
        }
    """
    log_step("Scraper Pipeline", "initiated", {"framework": "Playwright + 2Captcha + Selectolax"})

    tenders_list: List[TenderItem] = []
    current_page = 0
    total_live_tenders = None

    try:
        # Use Playwright for CAPTCHA handling and form submission
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
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = await context.new_page()

            # STEP 1: Navigate to Advanced Search (Playwright)
            log_step("Navigation", "processing", {"url": ADVANCED_SEARCH_URL})
            await page.goto(ADVANCED_SEARCH_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT)

            # STEP 2: Wait for form to load
            await page.wait_for_selector(CAPTCHA_INPUT_SELECTOR, timeout=10000)
            log_step("Navigation", "success", {"status": "Form loaded"})

            # STEP 3: Handle CAPTCHA if present
            captcha_solved = await handle_captcha_if_present(page)
            if not captcha_solved:
                log_step("Pipeline", "error", {"error": "CAPTCHA solving failed"})
                await browser.close()
                return {"success": False, "error": "CAPTCHA solving failed", "tenders": []}

            # STEP 4: Fill form - Select "Open Tender" (value="1")
            log_step("Form Filling", "processing", {"action": "Selecting Tender Type"})

            tender_type_selector = None
            for selector in TENDER_TYPE_SELECTORS:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        tender_type_selector = selector
                        break
                except Exception:
                    continue

            if tender_type_selector:
                await page.select_option(tender_type_selector, value="1")
                log_step("Form Filling", "success", {"tender_type": "Open Tender (value=1)"})
            else:
                log_step("Form Filling", "warning", {"message": "Tender type dropdown not found"})

            # STEP 5: Submit form
            log_step("Form Submission", "processing", {"action": "Clicking submit button"})
            await page.click(SUBMIT_BUTTON_SELECTOR)
            await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
            log_step("Form Submission", "success", {"status": "Results page loaded"})

            # STEP 6: Extract HTML directly from Playwright page (no need to re-navigate)
            log_step("HTML Extraction", "initiated", {"method": "Direct from Playwright"})
            
            # Get HTML content directly from the current page
            html_content = await page.content()
            
            page_tenders = extract_tenders_from_html(html_content)
            tenders_list.extend(page_tenders)
            current_page = 1

            log_step(
                "Page Extraction",
                "success",
                {"page": current_page, "tenders": len(page_tenders), "total": len(tenders_list)},
            )

            # STEP 7: Paginate using Playwright
            while current_page < MAX_PAGES:
                # Use Playwright for navigation
                next_button = await find_exact_next_button(page)
                if not next_button:
                    log_step(
                        "Pagination",
                        "success",
                        {"status": "No more pages", "total_pages": current_page},
                    )
                    break

                # Click next page
                await next_button.click()
                await asyncio.sleep(3)  # Wait for page load
                await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
                current_page += 1

                # Extract HTML directly from Playwright
                html_content = await page.content()
                page_tenders = extract_tenders_from_html(html_content)
                tenders_list.extend(page_tenders)

                log_step(
                    "Page Extraction",
                    "success",
                    {
                        "page": current_page,
                        "tenders": len(page_tenders),
                        "total": len(tenders_list),
                    },
                )

                # Small delay to avoid rate limiting
                await asyncio.sleep(2)

            # STEP 8: Extract total live tenders count from last page
            total_live_tenders = await extract_live_tenders_count(page)
            if total_live_tenders:
                log_step("Live Tenders Count", "success", {"count": total_live_tenders})

            await browser.close()

        log_step(
            "Scraper Pipeline",
            "success",
            {
                "total_tenders": len(tenders_list),
                "total_pages": current_page,
                "live_tenders": total_live_tenders or "N/A",
            },
        )

        return {
            "success": True,
            "tenders": [t.to_dict() for t in tenders_list],
            "total_pages": current_page,
            "live_tenders": total_live_tenders,
        }

    except Exception as e:
        log_step("Scraper Pipeline", "error", {"error": str(e)})
        return {
            "success": False,
            "error": str(e),
            "tenders": [t.to_dict() for t in tenders_list],
            "total_pages": current_page,
        }


async def scrape_latest_active_tenders() -> Dict[str, any]:
    """
    Scrape latest active tenders page (no CAPTCHA, simpler extraction).

    Returns:
        {
            "tenders": List[Dict],
            "total_pages": int,
            "success": bool
        }
    """
    log_step("Latest Tenders Scraper", "initiated", {"url": LATEST_TENDERS_URL})

    tenders_list: List[TenderItem] = []
    current_page = 0

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            )
            page = await context.new_page()

            # Navigate to latest tenders
            await page.goto(LATEST_TENDERS_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT)
            current_page = 1

            # Extract first page directly from Playwright
            html_content = await page.content()
            page_tenders = extract_tenders_from_html(html_content)
            tenders_list.extend(page_tenders)

            log_step(
                "Page Extraction",
                "success",
                {"page": current_page, "tenders": len(page_tenders)},
            )

            # Paginate
            while current_page < MAX_PAGES:
                next_button = await find_exact_next_button(page)
                if not next_button:
                    break

                await next_button.click()
                await asyncio.sleep(3)
                await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
                current_page += 1

                # Extract HTML directly
                html_content = await page.content()
                page_tenders = extract_tenders_from_html(html_content)
                tenders_list.extend(page_tenders)

                log_step(
                    "Page Extraction",
                    "success",
                    {"page": current_page, "tenders": len(page_tenders), "total": len(tenders_list)},
                )

                await asyncio.sleep(2)

            await browser.close()

        log_step(
            "Latest Tenders Scraper",
            "success",
            {"total_tenders": len(tenders_list), "total_pages": current_page},
        )

        return {
            "success": True,
            "tenders": [t.to_dict() for t in tenders_list],
            "total_pages": current_page,
        }

    except Exception as e:
        log_step("Latest Tenders Scraper", "error", {"error": str(e)})
        return {
            "success": False,
            "error": str(e),
            "tenders": [t.to_dict() for t in tenders_list],
            "total_pages": current_page,
        }

