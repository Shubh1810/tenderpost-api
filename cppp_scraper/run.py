"""
run.py — Entry point for the CPPP scraper.

Usage:
    python run.py                   # full run: scrape all pages + sync to Supabase
    python run.py --pages 5         # test run: only first 5 listing pages
    python run.py --sync-only       # skip scraping, push unsynced SQLite rows to Supabase
    python run.py --export          # dump SQLite → tenders_export.csv
    python run.py --resume          # same as default (dedup is always on via SQLite)
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as  `python run.py`  from within the cppp_scraper dir
# or  `python -m cppp_scraper.run`  from the parent dir.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

# Load .env early — before any module that reads env vars
from dotenv import load_dotenv
load_dotenv(_HERE / ".env", override=False)              # cppp_scraper/.env
load_dotenv(_HERE.parent / ".env", override=False)       # tenderpost-bk/.env

from loguru import logger

from cppp_scraper.config import LOG_FILE


def _setup_logging() -> None:
    logger.remove()
    # Console: INFO and above
    logger.add(sys.stderr, level="INFO", colorize=True)
    # File: DEBUG, rotating 10 MB, keep 3 backups
    logger.add(
        LOG_FILE,
        level="DEBUG",
        rotation="10 MB",
        retention=3,
        encoding="utf-8",
        enqueue=True,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CPPP tender scraper — eprocure.gov.in/cppp"
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=None,
        metavar="N",
        help="Only scrape the first N listing pages (default: auto-detect all)",
    )
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="Skip scraping; only push unsynced SQLite rows to Supabase",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Dump SQLite to tenders_export.csv and exit",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume interrupted run (default behaviour — "
            "already-scraped tender_ids are always skipped)"
        ),
    )
    parser.add_argument(
        "--print-migration",
        action="store_true",
        help="Print the Supabase SQL migration and exit",
    )
    return parser.parse_args()


async def _main() -> None:
    _setup_logging()
    args = _parse_args()

    if args.print_migration:
        from cppp_scraper.supabase_sync import MIGRATION_SQL
        print(MIGRATION_SQL)
        return

    if args.export:
        from cppp_scraper.export import export_to_csv
        await export_to_csv()
        return

    if args.sync_only:
        from cppp_scraper.supabase_sync import sync_to_supabase
        await sync_to_supabase()
        return

    # ── Full scrape ───────────────────────────────────────────────────────────
    from cppp_scraper.pipeline import run_pipeline
    await run_pipeline(pages=args.pages)

    # ── Auto-sync to Supabase after scrape ────────────────────────────────────
    try:
        from cppp_scraper.supabase_sync import sync_to_supabase
        logger.info("[run] starting post-scrape Supabase sync …")
        await sync_to_supabase()
    except RuntimeError as exc:
        logger.warning(f"[run] Supabase sync skipped: {exc}")


if __name__ == "__main__":
    asyncio.run(_main())
