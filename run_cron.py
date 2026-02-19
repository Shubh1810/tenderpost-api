"""
run_cron.py — Cron entrypoint for Render.

Pipeline (simplified now that detail scraping is inline):
1. Scrape list + detail pages in one session  → upsert into tenders table
2. Generate embeddings for tenders missing them
3. Update legacy latest_snapshot blob

Render Cron Start Command:
    python run_cron.py

Exits 0 on success, 1 on failure.
"""

import asyncio
import os
import re
import sys
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client

from embeddings import embed_tender
from scraper import scrape_tenders_crawl4ai_playwright
from supabase_client import save_to_supabase

load_dotenv()

EMBED_BATCH_SIZE = 100


def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise ValueError("Missing Supabase credentials")
    return create_client(url, key)


def _parse_date(date_str) -> Optional[str]:
    if not date_str or not str(date_str).strip():
        return None
    date_str = str(date_str).strip()
    for fmt in ["%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M",
                "%d-%b-%Y", "%d/%m/%Y"]:
        try:
            return datetime.strptime(date_str, fmt).isoformat()
        except ValueError:
            continue
    return None


# ── Step 1: Scrape and upsert ─────────────────────────────────────────────────

async def run_scrape_and_upsert(client) -> int:
    """
    Run the full scraper (list + detail pages in one session),
    then upsert every tender as its own row in the tenders table.
    """
    print("\n🔵 Step 1: Scraping (list + detail pages)...")
    result = await scrape_tenders_crawl4ai_playwright()

    if not result.get("success"):
        print(f"  ❌ Scrape failed: {result.get('error')}")
        return 0

    tenders = result.get("tenders", [])
    if not tenders:
        print("  ⚠️  No tenders scraped")
        return 0

    print(f"  ✅ Scraped {len(tenders)} tenders from "
          f"{result.get('total_pages')} pages")

    inserted = 0
    BATCH    = 100

    for i in range(0, len(tenders), BATCH):
        batch = tenders[i:i + BATCH]
        rows  = []

        for t in batch:
            rows.append({
                # ── list-page fields ──
                "ref_no":         (t.get("ref_no") or "UNKNOWN").strip(),
                "source":         "eprocure",
                "title":          (t.get("title") or "").strip(),
                "organisation":   (t.get("organisation") or "").strip() or None,
                "url":            (t.get("url") or "").strip(),
                "published_date": _parse_date(t.get("published_date")),
                "closing_date":   _parse_date(t.get("closing_date")),
                "opening_date":   _parse_date(t.get("opening_date")),
                "is_active":      True,
                # ── detail-page fields (populated when SCRAPE_DETAILS=true) ──
                "tender_id":          t.get("tender_id"),
                "tender_type":        t.get("tender_type"),
                "tender_category":    t.get("tender_category"),
                "contract_type":      t.get("contract_type"),
                "work_description":   t.get("work_description"),
                "product_category":   t.get("product_category"),
                "sub_category":       t.get("sub_category"),
                "location":           t.get("location"),
                "pincode":            t.get("pincode"),
                "tender_value":       t.get("tender_value"),
                "emd_amount":         t.get("emd_amount"),
                "period_of_work_days":t.get("period_of_work_days"),
                "bid_validity_days":  t.get("bid_validity_days"),
                # mark detail_scraped=true if we got any detail fields
                "detail_scraped": any([
                    t.get("tender_type"),
                    t.get("product_category"),
                    t.get("work_description"),
                    t.get("tender_value"),
                ]),
                "raw_data": {
                    "pre_bid_meeting_date":       t.get("pre_bid_meeting_date"),
                    "inviting_authority_name":    t.get("inviting_authority_name"),
                    "inviting_authority_address": t.get("inviting_authority_address"),
                },
            })

        # Remove None values so Supabase doesn't overwrite good data with nulls
        rows = [{k: v for k, v in row.items() if v is not None} for row in rows]

        try:
            client.table("tenders").upsert(
                rows,
                on_conflict="ref_no,source"
            ).execute()
            inserted += len(rows)
            print(f"  Upserted batch {i//BATCH + 1} "
                  f"({len(rows)} rows, running total: {inserted})")
        except Exception as e:
            print(f"  ⚠️  Batch failed: {e}")

    # Update legacy snapshot blob
    save_to_supabase(
        tenders=tenders,
        source="eprocure.gov.in/AdvancedSearch",
        live_tenders=result.get("live_tenders"),
    )

    print(f"  ✅ Upserted {inserted} tenders")
    return inserted


# ── Step 2: Embed tenders missing embeddings ──────────────────────────────────

async def run_embed_tenders(client) -> int:
    """
    Embed tenders that have no embedding yet.
    Prefers detail-scraped tenders first (richer text = better matches).
    """
    print(f"\n🔵 Step 2: Generating embeddings (batch of {EMBED_BATCH_SIZE})...")

    # Try detail-scraped first
    result = (
        client.table("tenders")
        .select("id, ref_no, title, organisation, product_category, "
                "work_description, location, tender_type, tender_category")
        .is_("embedding", "null")
        .eq("detail_scraped", True)
        .limit(EMBED_BATCH_SIZE)
        .execute()
    )
    tenders = result.data or []

    # Fall back to basic tenders if no detail-scraped ones left
    if not tenders:
        result = (
            client.table("tenders")
            .select("id, ref_no, title, organisation, product_category, "
                    "work_description, location, tender_type, tender_category")
            .is_("embedding", "null")
            .limit(EMBED_BATCH_SIZE)
            .execute()
        )
        tenders = result.data or []

    if not tenders:
        print("  ✅ All tenders already have embeddings")
        return 0

    print(f"  Found {len(tenders)} tenders to embed")
    embedded = 0

    for tender in tenders:
        embedding = await embed_tender(tender)
        if embedding is None:
            continue
        try:
            client.table("tenders").update(
                {"embedding": embedding}
            ).eq("id", tender["id"]).execute()
            embedded += 1
        except Exception as e:
            print(f"  ❌ Failed to store embedding for {tender['ref_no']}: {e}")

    print(f"  ✅ Embedded {embedded} tenders")
    return embedded


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    print("🕐 Cron: Starting pipeline...")
    print("=" * 50)

    try:
        client = get_supabase()
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    scraped  = await run_scrape_and_upsert(client)
    embedded = await run_embed_tenders(client)

    print("\n" + "=" * 50)
    print(f"✅ Cron complete — scraped: {scraped}, embedded: {embedded}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))