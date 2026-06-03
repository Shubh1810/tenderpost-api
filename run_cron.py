"""
run_cron.py — Scraping pipeline entrypoint for Render cron.

Pipeline:
1. Pre-fetch known ref_nos from Supabase
2. Scrape central CPPP listing (cpppdata) — per-page checkpoint writes
3. Scrape state CPPP listing (mmpdata) — per-page checkpoint writes
4. Fetch detail pages concurrently (same httpx session — cookies preserved)
5. Stream detail fields into Supabase as each completes
6. Mark tenders absent from today's scrape as is_active=False
7. Print run summary

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

import httpx
from dotenv import load_dotenv
from supabase import create_client

from logger import log, log_supabase, run_summary, timer
from scraper import HEADERS, fetch_all_details, scrape_listing
from supabase_client import save_to_supabase

load_dotenv()

_PAGE_SIZE = 1000  # Supabase pagination chunk size


def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(url, key)


# ── Step 0: Pre-fetch known DB state ─────────────────────────────────────────

def fetch_known_ref_nos(
    client, source: str
) -> Tuple[Set[str], Set[str]]:
    """
    Return (already_detailed, exists_no_detail) for the given source.

    Single-pass query (ref_no + detail_scraped together) to halve round-trips.
    On statement timeout (Supabase free tier, no index yet) gracefully returns
    empty sets so the pipeline continues — upsert on_conflict still prevents
    duplicates, early-stop just won't fire this run.
    Add indexes to fix the timeout permanently:
        CREATE INDEX ON tenders (source);
        CREATE INDEX ON tenders (source, detail_scraped);
    """
    already_detailed: Set[str] = set()
    exists_no_detail: Set[str] = set()
    offset = 0
    page   = 0

    try:
        while True:
            page += 1
            result = (
                client.table("tenders")
                .select("ref_no, detail_scraped")
                .eq("source", source)
                .neq("ref_no", "UNKNOWN")
                .range(offset, offset + _PAGE_SIZE - 1)
                .execute()
            )
            batch = result.data or []
            if not batch:
                break
            for r in batch:
                if r.get("detail_scraped"):
                    already_detailed.add(r["ref_no"])
                else:
                    exists_no_detail.add(r["ref_no"])
            log_supabase.debug(
                f"[{source}] DB page {page}: +{len(batch)} "
                f"(detailed={len(already_detailed):,}  no_detail={len(exists_no_detail):,})"
            )
            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

    except Exception as e:
        if "57014" in str(e) or "timeout" in str(e).lower():
            log_supabase.warning(
                f"[{source}] DB pre-fetch timed out after {page} pages "
                f"({len(already_detailed)+len(exists_no_detail):,} rows loaded) — "
                f"continuing without full dedup. Add indexes to fix this permanently."
            )
            # Return what we managed to load — partial dedup is better than none
            return already_detailed, exists_no_detail
        raise

    log_supabase.info(
        f"[{source}] DB state — already_detailed={len(already_detailed):,}  "
        f"exists_no_detail={len(exists_no_detail):,}"
    )
    return already_detailed, exists_no_detail


# ── Date normalisation ────────────────────────────────────────────────────────

def _parse_date(date_str) -> Optional[str]:
    if not date_str or not str(date_str).strip():
        return None
    s = str(date_str).strip()
    for fmt in [
        "%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M",
        "%d-%b-%Y",          "%d/%m/%Y",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    ]:
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


# ── Supabase upsert helpers ───────────────────────────────────────────────────

def _build_listing_row(t: Dict) -> Dict:
    return {
        "ref_no":         (t.get("ref_no") or "UNKNOWN").strip(),
        "source":         t.get("source", "central"),
        "title":          (t.get("title") or "").strip(),
        "organisation":   _clean_str(t.get("organisation")),
        "url":            _clean_str(t.get("url")),
        "published_date": _parse_date(t.get("published_date")),
        "closing_date":   _parse_date(t.get("closing_date")),
        "opening_date":   _parse_date(t.get("opening_date")),
        "is_active":      True,
    }


def _build_full_row(t: Dict) -> Dict:
    row = _build_listing_row(t)
    row.update({
        "tender_id":              t.get("tender_id"),
        "tender_type":            t.get("tender_type"),
        "tender_category":        t.get("tender_category"),
        "contract_type":          t.get("contract_type"),
        "work_description":       t.get("work_description"),
        "product_category":       t.get("product_category"),
        "sub_category":           t.get("sub_category"),
        "location":               t.get("location"),
        "pincode":                t.get("pincode"),
        "tender_value":           t.get("tender_value"),
        "emd_amount":             t.get("emd_amount"),
        "period_of_work_days":    t.get("period_of_work_days"),
        "bid_validity_days":      t.get("bid_validity_days"),
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
            "state":                      t.get("state"),
        },
    })
    return row


def _clean_str(val) -> Optional[str]:
    if not val:
        return None
    s = str(val).strip()
    return s or None


def _upsert_rows(client, rows: List[Dict], label: str, batch_size: int = 100) -> int:
    # Deduplicate within batch — Supabase throws on duplicate (ref_no, source)
    seen: Dict[tuple, Dict] = {}
    for r in rows:
        seen[(r.get("ref_no"), r.get("source"))] = r
    deduped = list(seen.values())

    total = 0
    for i in range(0, len(deduped), batch_size):
        chunk = [{k: v for k, v in r.items() if v is not None}
                 for r in deduped[i:i + batch_size]]
        try:
            client.table("tenders").upsert(chunk, on_conflict="ref_no,source").execute()
            total += len(chunk)
        except Exception as e:
            log_supabase.error(f"[{label}] batch {i // batch_size + 1} failed: {e}")
    return total


# ── Scrape + checkpoint one source ───────────────────────────────────────────

async def run_source_listing(
    source: str,
    client,  # Supabase client
    http_client: httpx.AsyncClient,
    already_detailed: Set[str],
    exists_no_detail: Set[str],
) -> List[Dict]:
    """
    Scrape listing for one source, writing each page to Supabase immediately.
    Returns list of all tender dicts with listing fields populated.
    """
    known_all = already_detailed | exists_no_detail
    pages_written = 0

    async def _checkpoint(page_tenders: List[Dict]) -> None:
        nonlocal pages_written
        rows = [
            _build_listing_row(t) for t in page_tenders
            if (t.get("ref_no") or "UNKNOWN") != "UNKNOWN"
            and t.get("ref_no") not in known_all
        ]
        rows = [{k: v for k, v in r.items() if v is not None} for r in rows]
        if not rows:
            return
        try:
            client.table("tenders").upsert(
                rows, on_conflict="ref_no,source"
            ).execute()
            pages_written += 1
        except Exception as e:
            log_supabase.warning(f"[{source}] Checkpoint write failed (page {pages_written}): {e}")

    log.info(f"Listing scrape — source={source}")
    with timer(f"listing_{source}", stage="SCRAPER"):
        result = await scrape_listing(
            source=source,
            client=http_client,
            known_ref_nos=known_all,
            early_stop_pages=3,
            on_page=_checkpoint,
        )

    if not result.get("success"):
        log.error(f"[{source}] Listing scrape failed: {result.get('error')}")
        return []

    tenders = result.get("tenders", [])
    log.success(
        f"[{source}] {len(tenders):,} tenders across {result['total_pages']} pages "
        f"(checkpoint pages written: {pages_written})"
    )
    return tenders


# ── Detail fetch + stream write ───────────────────────────────────────────────

async def run_detail_fetch(
    client,  # Supabase client
    http_client: httpx.AsyncClient,
    all_tenders: List[Dict],
    already_detailed: Set[str],
) -> List[Dict]:
    """
    Fetch detail pages for tenders not yet fully scraped.
    Writes each result to Supabase as it completes (streaming).
    Returns tenders list with detail fields merged in-memory.
    """
    to_fetch = [
        t for t in all_tenders
        if t.get("url")
        and t.get("ref_no")
        and t["ref_no"] != "UNKNOWN"
        and t["ref_no"] not in already_detailed
    ]

    skipped = len(all_tenders) - len(to_fetch)
    if skipped:
        log.info(f"Skipping detail fetch for {skipped} already-complete tenders")

    if not to_fetch:
        log.warning("No new detail URLs to fetch")
        return all_tenders

    url_to_ref: Dict[str, str] = {t["url"]: t["ref_no"] for t in to_fetch}
    ref_to_tender: Dict[str, Dict] = {t["ref_no"]: t for t in to_fetch}

    log.info(f"Fetching detail pages for {len(url_to_ref)} tenders...")

    with timer("detail_fetch", stage="SCRAPER"):
        ref_to_fields = await fetch_all_details(url_to_ref, http_client)

    # Stream-write to Supabase and merge in-memory
    written = 0
    no_fields = 0
    for ref_no, fields in ref_to_fields.items():
        if not fields:
            no_fields += 1
            continue

        row = {**{k: v for k, v in fields.items() if v is not None}, "detail_scraped": True}
        try:
            client.table("tenders").update(row).eq(
                "ref_no", ref_no
            ).eq("source", ref_to_tender[ref_no]["source"]).execute()
            written += 1
            log_supabase.debug(f"Detail written — {ref_no[:40]} ({len(fields)} fields)")
        except Exception as e:
            log_supabase.error(f"Detail write failed for {ref_no}: {e}")

        if ref_no in ref_to_tender:
            ref_to_tender[ref_no].update(fields)

    log.success(
        f"Detail phase done — {written} written, {no_fields} got no fields"
    )
    run_summary.record("detail_written", written)
    run_summary.record("detail_no_fields", no_fields)
    return all_tenders


# ── Full upsert ───────────────────────────────────────────────────────────────

def run_upsert(
    client,
    all_tenders: List[Dict],
    already_detailed: Set[str],
) -> int:
    """Two-tier upsert: full rows for new/undetailed, listing-only for complete."""
    full_rows    = [
        _build_full_row(t) for t in all_tenders
        if (t.get("ref_no") or "UNKNOWN") not in already_detailed
    ]
    listing_rows = [
        _build_listing_row(t) for t in all_tenders
        if (t.get("ref_no") or "UNKNOWN") in already_detailed
    ]

    log_supabase.info(
        f"Upsert — full={len(full_rows):,}, listing-only={len(listing_rows):,}"
    )

    total = 0
    if full_rows:
        with timer("upsert_full", stage="SUPABASE"):
            total += _upsert_rows(client, full_rows, "full")
    if listing_rows:
        with timer("upsert_listing", stage="SUPABASE"):
            total += _upsert_rows(client, listing_rows, "listing")

    run_summary.record("upsert_full", len(full_rows))
    run_summary.record("upsert_listing_only", len(listing_rows))
    run_summary.record("upsert_total", total)
    log_supabase.success(f"Upsert complete — {total:,} rows")
    return total


# ── Mark inactive ─────────────────────────────────────────────────────────────

def mark_inactive(client, current_ref_nos: Set[str], already_detailed: Set[str]) -> int:
    """Flip is_active=False for detailed tenders absent from today's scrape."""
    gone = already_detailed - current_ref_nos
    if not gone:
        run_summary.record("marked_inactive", 0)
        return 0

    log_supabase.info(f"Marking {len(gone)} tenders inactive...")
    gone_list = list(gone)
    marked = 0
    for i in range(0, len(gone_list), 500):
        chunk = gone_list[i:i + 500]
        try:
            client.table("tenders").update({"is_active": False}).in_(
                "ref_no", chunk
            ).execute()
            marked += len(chunk)
        except Exception as e:
            log_supabase.error(f"mark_inactive batch failed: {e}")

    run_summary.record("marked_inactive", marked)
    log_supabase.warning(f"Marked {marked} tenders inactive")
    return marked


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    log.info("=" * 58)
    log.info("  TENDERPOST — Pipeline Starting")
    log.info("  Sources: cpppdata (central) + mmpdata (state)")
    log.info("=" * 58)

    try:
        sb = get_supabase()
    except ValueError as e:
        log.critical(f"Supabase init failed: {e}")
        return 1

    # ── Step 0: Load known ref_nos ────────────────────────────────────────────
    log.info("Step 0 — Loading known ref_nos from Supabase...")
    with timer("fetch_known_ref_nos", stage="SUPABASE"):
        central_detailed, central_no_detail = fetch_known_ref_nos(sb, "central")
        state_detailed,   state_no_detail   = fetch_known_ref_nos(sb, "state")

    already_detailed_all = central_detailed | state_detailed

    run_summary.record("known_central_detailed",   len(central_detailed))
    run_summary.record("known_central_no_detail",  len(central_no_detail))
    run_summary.record("known_state_detailed",     len(state_detailed))
    run_summary.record("known_state_no_detail",    len(state_no_detail))

    all_tenders: List[Dict] = []

    # ── Steps 1-3: Scrape + detail fetch in one shared HTTP session ───────────
    async with httpx.AsyncClient(headers=HEADERS) as http_client:

        # Step 1: Central listing
        central_tenders = await run_source_listing(
            "central", sb, http_client,
            central_detailed, central_no_detail,
        )
        all_tenders.extend(central_tenders)

        # Step 2: State listing
        state_tenders = await run_source_listing(
            "state", sb, http_client,
            state_detailed, state_no_detail,
        )
        all_tenders.extend(state_tenders)

        if not all_tenders:
            log.critical("No tenders scraped — aborting")
            return 1

        log.info(
            f"Total tenders scraped: {len(all_tenders):,} "
            f"(central={len(central_tenders)}, state={len(state_tenders)})"
        )

        # Step 3: Detail pages (same http_client preserves session cookies)
        all_tenders = await run_detail_fetch(
            sb, http_client, all_tenders, already_detailed_all
        )

    # ── Step 4: Full upsert ───────────────────────────────────────────────────
    upserted = run_upsert(sb, all_tenders, already_detailed_all)

    # Legacy snapshot blob (kept for backward compat with other consumers)
    save_to_supabase(
        tenders=all_tenders,
        source="eprocure.gov.in/cppp",
    )

    # ── Step 5: Mark inactive tenders ────────────────────────────────────────
    current_ref_nos = {
        t["ref_no"] for t in all_tenders
        if t.get("ref_no") and t["ref_no"] != "UNKNOWN"
    }
    with timer("mark_inactive", stage="SUPABASE"):
        mark_inactive(sb, current_ref_nos, already_detailed_all)

    # ── Summary ───────────────────────────────────────────────────────────────
    run_summary.print()

    return 0 if upserted > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
