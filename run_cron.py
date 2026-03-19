"""
run_cron.py — Hybrid scraping pipeline entrypoint for Render cron.

Pipeline:
1. Playwright listing scrape (CAPTCHA + pagination, no detail clicks)
2. Cloudflare /crawl: submit all detail URLs immediately after Phase 1
3. Poll Cloudflare jobs concurrently until complete
4. Merge detail fields into tender dicts in-place
5a. Upsert all tenders to Supabase tenders table
5b. Generate OpenAI embeddings for tenders missing them

Set USE_CLOUDFLARE=false in .env to skip Phase 2 (listing-only fast mode).

Render Cron Start Command:
    python run_cron.py

Exits 0 on success, 1 on failure.
"""

import asyncio
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from supabase import create_client

from cloudflare_crawl import crawl_cppp_listing, crawl_detail_pages
from embeddings import embed_tender
from scraper import scrape_listing_pages
from supabase_client import save_to_supabase

load_dotenv()

USE_CLOUDFLARE      = os.getenv("USE_CLOUDFLARE", "true").lower() == "true"
SCRAPE_CPPP         = os.getenv("SCRAPE_CPPP", "true").lower() == "true"
EMBED_BATCH_SIZE    = 100


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


# ── Step 1: Playwright listing scrape ─────────────────────────────────────────

async def run_listing_scrape() -> Tuple[List[Dict], Optional[int]]:
    """
    Run Phase 1: Playwright listing scraper (CAPTCHA + pagination).

    Returns (tenders_list, live_tenders_count).
    tenders_list contains TenderItem dicts with only list-page fields populated.
    """
    print("\n🔵 Step 1: Playwright listing scrape...")
    result = await scrape_listing_pages()

    if not result.get("success"):
        print(f"  ❌ Listing scrape failed: {result.get('error')}")
        return [], None

    tenders = result.get("tenders", [])
    print(f"  ✅ Scraped {len(tenders)} tenders from {result.get('total_pages')} pages")
    return tenders, result.get("live_tenders")


# ── Steps 2-4: Cloudflare detail extraction + merge ──────────────────────────

async def run_cloudflare_detail_fetch(tenders: List[Dict]) -> List[Dict]:
    """
    Phase 2: Submit all detail URLs to Cloudflare /crawl, poll concurrently,
    merge extracted fields back into the tender dicts in-place.

    IMPORTANT: Called immediately after run_listing_scrape() to minimise
    sp= session token expiry risk.

    Args:
        tenders: List of TenderItem dicts with url field from Phase 1.

    Returns:
        Same list with detail fields merged in where extraction succeeded.
    """
    urls = [t["url"] for t in tenders if t.get("url")]
    if not urls:
        print("  ⚠️  No detail URLs to fetch")
        return tenders

    print(f"\n🔵 Steps 2-4: Cloudflare detail extraction for {len(urls)} URLs...")
    detail_map = await crawl_detail_pages(urls)

    merged = 0
    for t in tenders:
        url = t.get("url")
        if url and url in detail_map and detail_map[url]:
            t.update(detail_map[url])
            merged += 1

    print(f"  ✅ Merged detail fields for {merged}/{len(urls)} tenders")
    return tenders


# ── CPPP source: Cloudflare listing crawl ─────────────────────────────────────

async def run_cppp_crawl() -> List[Dict]:
    """
    Crawl the CPPP "Latest Active Tenders - Central" portal using Cloudflare /crawl.

    Source: https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata
    ~30,954 tenders across ~3,096 pages. No CAPTCHA. render=false (free during beta).

    Returns list of flat tender dicts (source="cppp").
    """
    print("\n🔵 CPPP: Cloudflare listing crawl (~3,096 pages, ~30K tenders)...")
    result = await crawl_cppp_listing()

    if not result.get("success"):
        print(f"  ❌ CPPP crawl failed: {result.get('error')}")
        return []

    tenders = result.get("tenders", [])
    print(f"  ✅ CPPP: {len(tenders)} tenders extracted")
    return tenders


# ── Step 5a: Upsert to Supabase ───────────────────────────────────────────────

async def run_upsert(
    client,
    tenders: List[Dict],
    live_tenders: Optional[int],
) -> int:
    """
    Upsert tenders to the Supabase tenders table in batches of 100.

    Builds each row from the merged tender dict (list fields + detail fields).
    Sets detail_scraped=True for tenders that have at least one detail field.
    """
    print(f"\n🔵 Step 5a: Upserting {len(tenders)} tenders to Supabase...")

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
                # ── detail fields (populated by Cloudflare phase) ──
                "tender_id":           t.get("tender_id"),
                "tender_type":         t.get("tender_type"),
                "tender_category":     t.get("tender_category"),
                "contract_type":       t.get("contract_type"),
                "work_description":    t.get("work_description"),
                "product_category":    t.get("product_category"),
                "sub_category":        t.get("sub_category"),
                "location":            t.get("location"),
                "pincode":             t.get("pincode"),
                "tender_value":        t.get("tender_value"),
                "emd_amount":          t.get("emd_amount"),
                "period_of_work_days": t.get("period_of_work_days"),
                "bid_validity_days":   t.get("bid_validity_days"),
                # mark detail_scraped=True if we got any detail fields
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

        # Omit None values so Supabase doesn't overwrite existing good data with nulls
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
        live_tenders=live_tenders,
    )

    print(f"  ✅ Upserted {inserted} tenders")
    return inserted


# ── Step 5b: Generate embeddings ──────────────────────────────────────────────

async def run_embed_tenders(client) -> int:
    """
    Embed tenders that have no embedding yet.
    Prefers detail-scraped tenders first (richer text = better matches).
    """
    print(f"\n🔵 Step 5b: Generating embeddings (batch of {EMBED_BATCH_SIZE})...")

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
    print("🕐 Cron: Starting hybrid pipeline...")
    print(f"   USE_CLOUDFLARE = {USE_CLOUDFLARE}")
    print(f"   SCRAPE_CPPP    = {SCRAPE_CPPP}")
    print("=" * 50)

    try:
        client = get_supabase()
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    all_tenders: List[Dict] = []

    # ── Source A: CPPP portal (Cloudflare, no CAPTCHA, ~30K tenders) ──────────
    if SCRAPE_CPPP:
        cppp_tenders = await run_cppp_crawl()
        all_tenders.extend(cppp_tenders)

    # ── Source B: eprocure.gov.in Advanced Search (Playwright + CAPTCHA) ──────
    eprocure_tenders, live_tenders = await run_listing_scrape()

    # Steps 2-4: Submit Cloudflare detail jobs IMMEDIATELY (sp= token expiry risk)
    if USE_CLOUDFLARE and eprocure_tenders:
        eprocure_tenders = await run_cloudflare_detail_fetch(eprocure_tenders)

    all_tenders.extend(eprocure_tenders)

    if not all_tenders:
        print("❌ No tenders scraped from any source")
        return 1

    # ── Upsert all sources ────────────────────────────────────────────────────
    upserted = await run_upsert(client, all_tenders, live_tenders)

    # ── Embed ─────────────────────────────────────────────────────────────────
    embedded = await run_embed_tenders(client)

    print("\n" + "=" * 50)
    print(f"✅ Cron complete — total: {len(all_tenders)}, upserted: {upserted}, embedded: {embedded}")
    return 0 if upserted > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
