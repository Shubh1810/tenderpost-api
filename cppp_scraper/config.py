"""
config.py — All constants for the CPPP scraper.
"""
from pathlib import Path

# ── URLs ─────────────────────────────────────────────────────────────────────
BASE_URL = "https://eprocure.gov.in/cppp/latestactivetendersnew/mmpdata"
DETAIL_BASE = "https://eprocure.gov.in"

# ── Pagination ────────────────────────────────────────────────────────────────
# ~44,614 tenders / 10 per page = 4,462 pages; add a buffer
DEFAULT_PAGE_COUNT = 4500

# ── Concurrency ───────────────────────────────────────────────────────────────
WORKERS = 3
QUEUE_MAXSIZE = 500

# ── Rate-limiting ─────────────────────────────────────────────────────────────
MIN_DELAY = 1.0   # seconds between requests
MAX_DELAY = 2.0

# Tenacity retry config
RETRY_ATTEMPTS = 5
RETRY_MIN_WAIT = 10   # seconds
RETRY_MAX_WAIT = 80

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
# Inside Docker the volume is mounted at /app/cppp_scraper/data/
# Locally it falls back to the cppp_scraper/ directory itself
_DATA_DIR = BASE_DIR / "data"
_DATA_DIR.mkdir(exist_ok=True)
DB_PATH = _DATA_DIR / "cppp_tenders.db"
LOG_FILE = _DATA_DIR / "cppp_scraper.log"
CSV_EXPORT_PATH = _DATA_DIR / "tenders_export.csv"
CACHE_DIR = _DATA_DIR / ".hishel_cache"

# ── Supabase batch size ───────────────────────────────────────────────────────
SUPABASE_BATCH = 100

# ── User-Agent pool ───────────────────────────────────────────────────────────
USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
]

# ── Base HTTP headers ─────────────────────────────────────────────────────────
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
