"""
run_embed.py — Standalone embedding cron. Decoupled from the scrape pipeline.

Picks up tenders with embedding=NULL and generates OpenAI vectors for them.
Runs independently of run_cron.py so scraping and embedding never block each other.

Render Cron Start Command (every 15 min):
    python run_embed.py

Exits 0 on success, 1 on failure.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from supabase import create_client

from logger import log_embed, run_summary, timer
from embeddings import embed_tender

load_dotenv()

EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "100"))


def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise ValueError("Missing Supabase credentials")
    return create_client(url, key)


async def main() -> int:
    log_embed.info("=" * 58)
    log_embed.info("  TENDERPOST — Embedding Cron Starting")
    log_embed.info(f"  EMBED_BATCH_SIZE = {EMBED_BATCH_SIZE}")
    log_embed.info("=" * 58)

    try:
        client = get_supabase()
    except ValueError as e:
        log_embed.critical(f"Supabase init failed: {e}")
        return 1

    # ── Fetch unembedded tenders — detail-scraped first (richer text) ──────────
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
        source_label = "basic (no detail fields yet)"

    if not tenders:
        log_embed.success("All tenders already have embeddings — nothing to do")
        return 0

    log_embed.info(f"Found {len(tenders)} tenders to embed (source: {source_label})")

    embedded = 0
    failed   = 0
    import time
    t_start  = time.perf_counter()

    for i, tender in enumerate(tenders, 1):
        ref_no = tender.get("ref_no", "?")[:40]
        fields_present = [
            f for f in ("title", "organisation", "product_category",
                        "work_description", "location", "tender_type", "tender_category")
            if tender.get(f)
        ]
        log_embed.debug(f"[{i}/{len(tenders)}] Embedding {ref_no} — fields: {fields_present}")

        t_tender  = time.perf_counter()
        embedding = await embed_tender(tender)
        elapsed   = time.perf_counter() - t_tender

        if embedding is None:
            log_embed.warning(f"[{i}/{len(tenders)}] Returned None for {ref_no} ({elapsed:.2f}s)")
            failed += 1
            continue

        try:
            client.table("tenders").update({"embedding": embedding}).eq("id", tender["id"]).execute()
            embedded += 1
            log_embed.debug(f"[{i}/{len(tenders)}] ✓ {ref_no} ({elapsed:.2f}s, {len(embedding)} dims)")
        except Exception as e:
            log_embed.error(f"[{i}/{len(tenders)}] Store failed for {ref_no}: {e}")
            failed += 1

    total_time = time.perf_counter() - t_start
    avg_time   = total_time / len(tenders) if tenders else 0

    run_summary.record("embed_attempted", len(tenders))
    run_summary.record("embed_succeeded", embedded)
    run_summary.record("embed_failed", failed)
    run_summary.record("embed_total_time_s", round(total_time, 1))
    run_summary.record("embed_avg_time_s", round(avg_time, 2))

    log_embed.success(
        f"Done — {embedded}/{len(tenders)} embedded, {failed} failed, "
        f"total={total_time:.1f}s avg={avg_time:.2f}s/tender"
    )
    run_summary.print()

    return 0 if embedded > 0 or failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
