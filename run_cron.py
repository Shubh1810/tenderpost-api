"""
Cron entrypoint: run the main scraping pipeline and save to Supabase.

Designed for Render Cron Job (or any scheduler). Use Start Command:
  python run_cron.py

No curl/wget needed. Exits 0 on success, 1 on failure.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

from scraper import scrape_tenders_crawl4ai_playwright
from supabase_client import save_to_supabase

load_dotenv()


async def main() -> int:
    """Run full scrape pipeline and save to Supabase. Returns exit code."""
    print("🕐 Cron: Starting scrape pipeline...")
    result = await scrape_tenders_crawl4ai_playwright()

    if not result.get("success"):
        print(f"❌ Cron: Scrape failed — {result.get('error', 'Unknown error')}")
        return 1

    tenders = result.get("tenders", [])
    if not tenders:
        print("⚠️ Cron: No tenders extracted (success but empty)")
        return 0

    supabase_result = save_to_supabase(
        tenders=tenders,
        source="eprocure.gov.in/AdvancedSearch",
        live_tenders=result.get("live_tenders"),
    )

    if supabase_result.get("success"):
        print(f"✅ Cron: Saved {supabase_result.get('count')} tenders to Supabase")
        return 0
    print(f"⚠️ Cron: Supabase save failed — {supabase_result.get('error')}")
    return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
