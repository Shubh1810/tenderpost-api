"""
run_cron.py — Hybrid scraping pipeline entrypoint for Render cron.

Pipeline:
1. Pre-fetch known ref_nos from Supabase (already_detailed / exists_no_detail)
2. Playwright listing scrape (CAPTCHA + pagination, early-stop on known pages)
3. Cloudflare /crawl: submit ONLY new/undetailed detail URLs (skip already-complete)
4. Poll Cloudflare jobs concurrently until complete
5. Merge detail fields into tender dicts in-place
6a. Two-tier upsert:
      - Full upsert  (new + undetailed): all fields + detail_scraped flag
      - Listing-only upsert (already-complete): dates/title/is_active only
6b. Mark tenders absent from today's listing as is_active=False
6c. Generate OpenAI embeddings for tenders missing them

Set USE_CLOUDFLARE=false in .env to skip Phase 3 (listing-only fast mode).

Render Cron Start Command:
    python run_cron.py

Exits 0 on success, 1 on failure.
"""

import asyncio
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from supabase import create_client

from cloudflare_crawl import crawl_cppp_listing, crawl_detail_pages, crawl_detail_pages_stream
from embeddings import embed_tender
from logger import log, log_supabase, run_summary, timer
from scraper import scrape_listing_pages
from supabase_client import save_to_supabase

load_dotenv()

USE_CLOUDFLARE      = os.getenv("USE_CLOUDFLARE", "true").lower() == "true"
SCRAPE_CPPP         = os.getenv("SCRAPE_CPPP", "true").lower() == "true"
EMBED_BATCH_SIZE    = 100
_PAGE_SIZE          = 1000  # Supabase pagination chunk


def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise ValueError("Missing Supabase credentials")
    return create_client(url, key)


# ── Pre-fetch known DB state ──────────────────────────────────────────────────

def fetch_known_ref_nos(client) -> Tuple[Set[str], Set[str]]:
    """
    Return (already_detailed, exists_no_detail) for eprocure tenders.

    Paginates through all rows because Supabase returns max 1000 per call.
    Skips ref_no='UNKNOWN' — those can't be used for deduplication.
    """
    already_detailed: Set[str] = set()
    exists_no_detail: Set[str] = set()

    for detail_scraped in (True, False):
        label = "detail_scraped=True" if detail_scraped else "detail_scraped=False"
        offset = 0
        page = 0
        while True:
            page += 1
            result = (
                client.table("tenders")
                .select("ref_no")
                .eq("source", "eprocure")
                .eq("detail_scraped", detail_scraped)
                .neq("ref_no", "UNKNOWN")
                .range(offset, offset + _PAGE_SIZE - 1)
                .execute()
            )
            batch = result.data or []
            if not batch:
                break
            if detail_scraped:
                already_detailed.update(r["ref_no"] for r in batch)
            else:
                exists_no_detail.update(r["ref_no"] for r in batch)
            log_supabase.debug(f"  [{label}] page {page}: +{len(batch)} ref_nos (running total: {len(already_detailed) + len(exists_no_detail)})")
            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

    log_supabase.info(f"DB state loaded — already_detailed={len(already_detailed):,}  exists_no_detail={len(exists_no_detail):,}")
    run_summary.record("known_already_detailed", len(already_detailed))
    run_summary.record("known_exists_no_detail", len(exists_no_detail))
    return already_detailed, exists_no_detail


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

async def run_listing_scrape(
    client,
    already_detailed: Set[str],
) -> Tuple[List[Dict], Optional[int]]:
    """
    Run Phase 1: Playwright listing scraper (CAPTCHA + pagination).

    Passes already_detailed for early stopping and a per-page checkpoint
    callback that immediately upserts listing-only rows to Supabase.
    A crash mid-scrape preserves all pages already written.

    Returns (tenders_list, live_tenders_count).
    tenders_list contains TenderItem dicts with only list-page fields populated.
    """
    pages_written = 0

    async def _checkpoint(page_tenders: List[Dict]) -> None:
        nonlocal pages_written
        rows = [_build_listing_row(t) for t in page_tenders
                if (t.get("ref_no") or "UNKNOWN") != "UNKNOWN"]
        rows = [{k: v for k, v in r.items() if v is not None} for r in rows]
        if not rows:
            return
        try:
            client.table("tenders").upsert(rows, on_conflict="ref_no,source").execute()
            pages_written += 1
            log_supabase.debug(f"Checkpoint page {pages_written}: wrote {len(rows)} listing rows")
        except Exception as e:
            log_supabase.warning(f"Checkpoint write failed (page {pages_written}): {e} — continuing")

    log.info("Step 1 — Playwright listing scrape starting (with per-page checkpoint writes)...")
    with timer("listing_scrape", stage="SCRAPER"):
        result = await scrape_listing_pages(
            already_detailed=already_detailed,
            on_page=_checkpoint,
        )

    if not result.get("success"):
        log.error(f"Listing scrape failed: {result.get('error')}")
        return [], None

    tenders      = result.get("tenders", [])
    total_pages  = result.get("total_pages", 0)
    live_tenders = result.get("live_tenders")

    run_summary.record("listing_pages_scraped", total_pages)
    run_summary.record("listing_tenders_found", len(tenders))
    run_summary.record("early_stop_triggered", result.get("early_stop_triggered", False))

    log.success(
        f"Listing scrape done — {len(tenders)} tenders across {total_pages} pages "
        f"(checkpoint_pages_written={pages_written}, live_count={live_tenders}, "
        f"early_stop={result.get('early_stop_triggered', False)})"
    )
    return tenders, live_tenders


# ── Steps 2-4: Cloudflare detail extraction + merge ──────────────────────────

async def run_cloudflare_detail_fetch(
    client,
    tenders: List[Dict],
    already_detailed: Set[str],
) -> List[Dict]:
    """
    Phase 2: Submit detail URLs to Cloudflare for new/undetailed tenders only,
    then stream results — writing each tender's detail fields to Supabase
    immediately as its Cloudflare job completes (not after all jobs finish).

    Merge key is ref_no (stable), not URL (volatile sp= token).

    Args:
        client:           Supabase client for streaming writes.
        tenders:          List of TenderItem dicts from Phase 1.
        already_detailed: ref_nos with detail_scraped=True — skip these.

    Returns:
        Same list with detail fields merged in-memory for downstream use.
    """
    to_fetch = [
        t for t in tenders
        if (t.get("ref_no") or "UNKNOWN") not in already_detailed
        and t.get("url")
        and t.get("ref_no")
    ]
    skipped = len(tenders) - len(to_fetch)
    if skipped:
        log.info(f"Skipping Cloudflare for {skipped} already-complete tenders")

    if not to_fetch:
        log.warning("No detail URLs to fetch — all tenders already complete or missing URL/ref_no")
        run_summary.record("cf_jobs_submitted", 0)
        run_summary.record("cf_jobs_skipped", skipped)
        return tenders

    # Build url→ref_no map (stable key for Supabase updates)
    url_to_ref: Dict[str, str] = {
        t["url"]: t["ref_no"] for t in to_fetch
    }
    # Keep an in-memory index for fast lookups during merge
    ref_to_tender: Dict[str, Dict] = {
        t["ref_no"]: t for t in to_fetch
    }

    log.info(f"Steps 2-4 — Cloudflare streaming detail fetch for {len(url_to_ref)} URLs ({skipped} skipped)")
    run_summary.record("cf_jobs_skipped", skipped)

    streamed = 0
    no_fields = 0

    with timer("cloudflare_detail_fetch_stream", stage="CLOUDFLARE"):
        async for url, ref_no, fields in crawl_detail_pages_stream(url_to_ref):
            if not fields:
                no_fields += 1
                continue

            # ── Immediate Supabase write ──────────────────────────────────────
            row = {
                **{k: v for k, v in fields.items() if v is not None},
                "detail_scraped": True,
            }
            try:
                client.table("tenders").update(row).eq(
                    "ref_no", ref_no
                ).eq("source", "eprocure").execute()
                log_supabase.debug(f"Streamed detail write — {ref_no} ({len(fields)} fields)")
            except Exception as e:
                log_supabase.error(f"Stream write failed for {ref_no}: {e}")

            # ── Also merge in-memory so run_upsert sees the full picture ──────
            if ref_no in ref_to_tender:
                ref_to_tender[ref_no].update(fields)

            streamed += 1
            run_summary.increment("cf_fields_extracted")

    run_summary.record("cf_jobs_skipped", skipped)
    log.success(
        f"Streaming detail fetch done — {streamed} written to Supabase live, "
        f"{no_fields} got no fields"
    )
    return tenders


# ── CPPP source: Cloudflare listing crawl ─────────────────────────────────────

async def run_cppp_crawl() -> List[Dict]:
    """
    Crawl the CPPP "Latest Active Tenders - Central" portal using Cloudflare /crawl.

    Source: https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata
    ~30,954 tenders across ~3,096 pages. No CAPTCHA. render=false (free during beta).

    Returns list of flat tender dicts (source="cppp").
    """
    log.info("CPPP — Cloudflare listing crawl starting (~3,096 pages, ~30K tenders)...")
    with timer("cppp_listing_crawl", stage="CPPP"):
        result = await crawl_cppp_listing()

    if not result.get("success"):
        log.error(f"CPPP crawl failed: {result.get('error')}")
        return []

    tenders = result.get("tenders", [])
    log.success(f"CPPP crawl complete — {len(tenders):,} tenders extracted")
    return tenders


# ── Step 5a: Upsert to Supabase ───────────────────────────────────────────────

def _build_full_row(t: Dict) -> Dict:
    """Build a complete upsert row (Buckets A + B: new or undetailed tenders)."""
    return {
        "ref_no":         (t.get("ref_no") or "UNKNOWN").strip(),
        "source":         "eprocure",
        "title":          (t.get("title") or "").strip(),
        "organisation":   (t.get("organisation") or "").strip() or None,
        "url":            (t.get("url") or "").strip(),
        "published_date": _parse_date(t.get("published_date")),
        "closing_date":   _parse_date(t.get("closing_date")),
        "opening_date":   _parse_date(t.get("opening_date")),
        "is_active":      True,
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
    }


def _build_listing_row(t: Dict) -> Dict:
    """
    Build a listing-only upsert row (Bucket C: already-complete tenders).

    Only refreshes listing fields — never touches detail fields or
    detail_scraped, so existing detail data in Supabase is preserved.
    """
    return {
        "ref_no":         (t.get("ref_no") or "UNKNOWN").strip(),
        "source":         "eprocure",
        "title":          (t.get("title") or "").strip(),
        "organisation":   (t.get("organisation") or "").strip() or None,
        "url":            (t.get("url") or "").strip(),
        "published_date": _parse_date(t.get("published_date")),
        "closing_date":   _parse_date(t.get("closing_date")),
        "opening_date":   _parse_date(t.get("opening_date")),
        "is_active":      True,
    }


def _upsert_rows(client, rows: List[Dict], label: str, batch_size: int = 100) -> int:
    """Upsert rows in batches, return count upserted."""
    total = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        # Omit None values so Supabase doesn't overwrite existing good data
        clean = [{k: v for k, v in r.items() if v is not None} for r in chunk]
        try:
            client.table("tenders").upsert(
                clean,
                on_conflict="ref_no,source",
            ).execute()
            total += len(clean)
            print(f"  [{label}] batch {i // batch_size + 1} — {len(clean)} rows (total {total})")
        except Exception as e:
            print(f"  ⚠️  [{label}] batch failed: {e}")
    return total


async def run_upsert(
    client,
    tenders: List[Dict],
    live_tenders: Optional[int],
    already_detailed: Set[str],
) -> int:
    """
    Two-tier upsert to Supabase tenders table.

    Tier 1 — Full upsert (new + undetailed tenders):
        Writes all fields including detail fields and detail_scraped flag.

    Tier 2 — Listing-only upsert (already-complete tenders):
        Writes only listing fields (dates, title, is_active).
        Leaves detail fields and detail_scraped untouched in Supabase.
    """
    log_supabase.info(f"Step 5a — Two-tier upsert for {len(tenders)} tenders...")

    full_tenders    = [t for t in tenders if (t.get("ref_no") or "UNKNOWN") not in already_detailed]
    listing_tenders = [t for t in tenders if (t.get("ref_no") or "UNKNOWN") in already_detailed]

    log_supabase.info(f"  Tier 1 (full upsert)   : {len(full_tenders):,} tenders — new or previously undetailed")
    log_supabase.info(f"  Tier 2 (listing only)  : {len(listing_tenders):,} tenders — already complete, refreshing dates only")

    inserted = 0

    if full_tenders:
        log_supabase.debug(f"  Upserting {len(full_tenders)} full rows in batches of 100...")
        rows = [_build_full_row(t) for t in full_tenders]
        with timer("upsert_full", stage="SUPABASE"):
            inserted += _upsert_rows(client, rows, "full")

    if listing_tenders:
        log_supabase.debug(f"  Upserting {len(listing_tenders)} listing-only rows in batches of 100...")
        rows = [_build_listing_row(t) for t in listing_tenders]
        with timer("upsert_listing_only", stage="SUPABASE"):
            inserted += _upsert_rows(client, rows, "listing")

    run_summary.record("upsert_full", len(full_tenders))
    run_summary.record("upsert_listing_only", len(listing_tenders))
    run_summary.record("upsert_total", inserted)

    # Update legacy snapshot blob
    save_to_supabase(
        tenders=tenders,
        source="eprocure.gov.in/AdvancedSearch",
        live_tenders=live_tenders,
    )

    log_supabase.success(f"Upsert complete — {inserted:,} rows ({len(full_tenders)} full, {len(listing_tenders)} listing-only)")
    return inserted


# ── Step 5b: Mark inactive ────────────────────────────────────────────────────

def mark_inactive_tenders(
    client,
    current_ref_nos: Set[str],
    already_detailed: Set[str],
) -> int:
    """
    Flip is_active=False for eprocure tenders that vanished from today's listing.

    Only checks tenders we already have full details for — if a ref_no was in
    already_detailed but isn't in today's scrape, the tender is no longer active.
    """
    gone = already_detailed - current_ref_nos
    if not gone:
        log_supabase.info("No tenders to mark inactive — all known tenders seen in today's listing")
        run_summary.record("marked_inactive", 0)
        return 0

    log_supabase.info(f"Marking {len(gone)} tenders inactive (absent from today's listing)...")
    CHUNK = 500
    gone_list = list(gone)
    marked = 0
    for i in range(0, len(gone_list), CHUNK):
        chunk = gone_list[i:i + CHUNK]
        try:
            client.table("tenders").update({"is_active": False}).eq(
                "source", "eprocure"
            ).in_("ref_no", chunk).execute()
            marked += len(chunk)
            log_supabase.debug(f"  marked_inactive batch: {len(chunk)} (total so far: {marked})")
        except Exception as e:
            log_supabase.error(f"mark_inactive batch failed: {e}")

    run_summary.record("marked_inactive", marked)
    log_supabase.warning(f"Marked {marked} tenders as is_active=False")
    return marked


# ── Step 5b: Generate embeddings ──────────────────────────────────────────────

async def run_embed_tenders(client) -> int:
    """
    Embed tenders that have no embedding yet.
    Prefers detail-scraped tenders first (richer text = better matches).
    """
    log_embed = __import__("logger").log_embed
    log_embed.info(f"Step 5b — Embedding (batch size: {EMBED_BATCH_SIZE})...")

    # Try detail-scraped first (richer text = better search quality)
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
    source_label = "detail_scraped=True"

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
        source_label = "basic (no detail)"

    if not tenders:
        log_embed.success("All tenders already have embeddings — nothing to do")
        run_summary.record("embed_attempted", 0)
        run_summary.record("embed_succeeded", 0)
        return 0

    log_embed.info(f"Found {len(tenders)} tenders to embed (source: {source_label})")
    embedded = 0
    failed   = 0
    t_start  = time.perf_counter()

    for i, tender in enumerate(tenders, 1):
        ref_no = tender.get("ref_no", "?")[:40]
        fields_present = [
            f for f in ("title", "organisation", "product_category",
                        "work_description", "location", "tender_type", "tender_category")
            if tender.get(f)
        ]
        log_embed.debug(f"  [{i}/{len(tenders)}] Embedding {ref_no} — fields: {fields_present}")

        t_tender = time.perf_counter()
        embedding = await embed_tender(tender)
        elapsed   = time.perf_counter() - t_tender

        if embedding is None:
            log_embed.warning(f"  [{i}/{len(tenders)}] Embedding returned None for {ref_no} ({elapsed:.2f}s)")
            failed += 1
            continue

        try:
            client.table("tenders").update({"embedding": embedding}).eq("id", tender["id"]).execute()
            embedded += 1
            log_embed.debug(f"  [{i}/{len(tenders)}] ✓ Stored embedding for {ref_no} ({elapsed:.2f}s, {len(embedding)} dims)")
        except Exception as e:
            log_embed.error(f"  [{i}/{len(tenders)}] Failed to store embedding for {ref_no}: {e}")
            failed += 1

    total_time = time.perf_counter() - t_start
    avg_time   = total_time / len(tenders) if tenders else 0

    run_summary.record("embed_attempted", len(tenders))
    run_summary.record("embed_succeeded", embedded)
    run_summary.record("embed_failed", failed)
    run_summary.record("embed_total_time_s", round(total_time, 1))
    run_summary.record("embed_avg_time_s", round(avg_time, 2))

    log_embed.success(
        f"Embedding done — {embedded}/{len(tenders)} succeeded, {failed} failed, "
        f"total={total_time:.1f}s, avg={avg_time:.2f}s/tender"
    )
    return embedded


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    log.info("=" * 58)
    log.info("  TENDERPOST — Hybrid Pipeline Starting")
    log.info(f"  USE_CLOUDFLARE = {USE_CLOUDFLARE}")
    log.info(f"  SCRAPE_CPPP    = {SCRAPE_CPPP}")
    log.info("=" * 58)

    try:
        client = get_supabase()
        log_supabase.info("Supabase client initialised")
    except ValueError as e:
        log.critical(f"Supabase init failed: {e}")
        return 1

    # ── Step 0: Pre-fetch known eprocure state from Supabase ──────────────────
    log.info("Step 0 — Loading known ref_nos from Supabase...")
    with timer("fetch_known_ref_nos", stage="SUPABASE"):
        already_detailed, exists_no_detail = fetch_known_ref_nos(client)

    all_tenders: List[Dict] = []

    # ── Source A: CPPP portal (Cloudflare, no CAPTCHA, ~30K tenders) ──────────
    if SCRAPE_CPPP:
        cppp_tenders = await run_cppp_crawl()
        all_tenders.extend(cppp_tenders)
    else:
        log.info("CPPP scrape disabled (SCRAPE_CPPP=false)")

    # ── Source B: eprocure.gov.in Advanced Search (Playwright + CAPTCHA) ──────
    eprocure_tenders, live_tenders = await run_listing_scrape(client, already_detailed)

    # Steps 2-4: Submit Cloudflare detail jobs IMMEDIATELY (sp= token expiry risk)
    # Streams results back — each tender is written to Supabase as its job completes.
    if USE_CLOUDFLARE and eprocure_tenders:
        eprocure_tenders = await run_cloudflare_detail_fetch(
            client, eprocure_tenders, already_detailed
        )
    elif not USE_CLOUDFLARE:
        log.info("Cloudflare detail phase disabled (USE_CLOUDFLARE=false)")

    all_tenders.extend(eprocure_tenders)

    if not all_tenders:
        log.critical("No tenders scraped from any source — aborting")
        return 1

    log.info(f"Total tenders across all sources: {len(all_tenders):,} (eprocure={len(eprocure_tenders)}, cppp={len(all_tenders)-len(eprocure_tenders)})")

    # ── Two-tier upsert ───────────────────────────────────────────────────────
    upserted = await run_upsert(client, all_tenders, live_tenders, already_detailed)

    # ── Mark tenders absent from today's listing as inactive ──────────────────
    current_eprocure_ref_nos = {
        (t.get("ref_no") or "UNKNOWN") for t in eprocure_tenders
        if (t.get("ref_no") or "UNKNOWN") != "UNKNOWN"
    }
    with timer("mark_inactive", stage="SUPABASE"):
        mark_inactive_tenders(client, current_eprocure_ref_nos, already_detailed)

    # ── Embeddings run as a separate cron (run_embed.py) ──────────────────────
    log.info("Embedding is handled by run_embed.py (separate cron) — skipping here")

    # ── Final run summary ─────────────────────────────────────────────────────
    run_summary.print()

    return 0 if upserted > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
