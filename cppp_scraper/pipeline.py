"""
pipeline.py — Producer / consumer queue orchestration.

Change-detection for efficient twice-daily cron:
  Producer loads {tender_id: content_hash} from SQLite at startup.
  For each tender on a listing page:
    - hash unchanged → skip (no HTTP request, no DB write, no Supabase sync)
    - hash changed   → re-fetch detail, update DB, re-queue for Supabase
    - new tender     → full fetch, insert DB, queue for Supabase

This means a re-run when 99% of tenders are unchanged will only touch
the small fraction that changed (new closing dates, corrigenda, new tenders).
"""
import asyncio
from typing import Any, Dict, Optional, Set

from loguru import logger
from tqdm.asyncio import tqdm

from .config import BASE_URL, DEFAULT_PAGE_COUNT, QUEUE_MAXSIZE, WORKERS
from .db import (
    compute_hash,
    get_scraped_hashes,
    get_stats,
    init_db,
    mark_fetch_error,
    mark_token_expired,
    save_tender,
    save_tender_detail,
)
from .scraper import (
    build_client,
    fetch_detail,
    fetch_listing_page,
    get_total_count,
    parse_detail_html,
    parse_listing_html,
)

_STOP = None


async def _producer(
    queue: asyncio.Queue,
    client,
    pages: int,
    scraped_hashes: Dict[str, str],
    pbar: tqdm,
) -> Dict[str, int]:
    counts = {"new": 0, "updated": 0, "unchanged": 0}

    for page in range(pages):
        html = await fetch_listing_page(client, page)
        if html is None:
            logger.error(f"[producer] page {page} fetch failed — skipping")
            pbar.update(1)
            continue

        tenders = parse_listing_html(html, page)
        if not tenders:
            logger.info(f"[producer] page {page} empty — stopping early")
            pbar.update(1)
            break

        for t in tenders:
            tid = t["tender_id"]
            new_hash = t["content_hash"]
            stored_hash = scraped_hashes.get(tid)

            if stored_hash is None:
                # Brand new tender
                counts["new"] += 1
                await queue.put(t)
            elif stored_hash != new_hash:
                # Something changed on the listing page (date extension, corrigendum)
                counts["updated"] += 1
                t["_is_update"] = True
                await queue.put(t)
            else:
                # Identical to what we have — skip entirely
                counts["unchanged"] += 1

        pbar.update(1)
        pbar.set_postfix(
            new=counts["new"],
            upd=counts["updated"],
            skip=counts["unchanged"],
        )

    logger.info(
        f"[producer] done — new={counts['new']} "
        f"updated={counts['updated']} unchanged={counts['unchanged']}"
    )
    return counts


async def _worker(
    worker_id: int,
    queue: asyncio.Queue,
    client,
    db_conn,
    pbar: tqdm,
) -> None:
    while True:
        item: Optional[Dict[str, Any]] = await queue.get()
        if item is _STOP:
            queue.task_done()
            break

        tender_id = item["tender_id"]
        detail_url = item["detail_url"]
        listing_url = item.pop("_listing_url", f"{BASE_URL}?page=0")
        item.pop("_is_update", None)  # internal flag, not stored

        try:
            await save_tender(db_conn, item)

            html = await fetch_detail(client, detail_url, listing_url)

            if html is None:
                await mark_fetch_error(db_conn, tender_id)
                logger.warning(f"[w{worker_id}] fetch error: {tender_id}")
            elif html == "":
                await mark_token_expired(db_conn, tender_id)
                logger.debug(f"[w{worker_id}] token expired: {tender_id}")
            else:
                detail = parse_detail_html(html)
                await save_tender_detail(db_conn, tender_id, detail)
                logger.debug(f"[w{worker_id}] saved: {item.get('title', '')[:60]}")

        except Exception as exc:
            logger.exception(f"[w{worker_id}] error on {tender_id}: {exc}")
            try:
                await mark_fetch_error(db_conn, tender_id)
            except Exception:
                pass
        finally:
            queue.task_done()
            pbar.update(1)


async def run_pipeline(pages: Optional[int] = None) -> None:
    db_conn = await init_db()
    # Load existing hashes for change-detection
    scraped_hashes = await get_scraped_hashes(db_conn)

    client = build_client()

    # Auto-detect total pages from live count
    total_pages = pages or DEFAULT_PAGE_COUNT
    probe_html = await fetch_listing_page(client, 0)
    if probe_html:
        total_count = get_total_count(probe_html)
        if total_count and pages is None:
            total_pages = (total_count + 9) // 10 + 10
            logger.info(f"[startup] live count: {total_count:,} → {total_pages} pages")

    existing = len(scraped_hashes)
    print(
        f"\n{'='*60}\n"
        f"  CPPP Scraper\n"
        f"{'='*60}\n"
        f"  Pages          : {total_pages:,}\n"
        f"  In SQLite      : {existing:,}\n"
        f"  Mode           : {'resume/update' if existing else 'first run'}\n"
        f"  Change detect  : hash(title|close_date|open_date|corrigendum)\n"
        f"  Workers        : {WORKERS}\n"
        f"{'='*60}\n"
    )

    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)

    pbar_listing = tqdm(total=total_pages, desc="Listing pages", unit="pg", position=0)
    pbar_detail  = tqdm(total=None,        desc="Processed    ", unit="t",  position=1)

    worker_tasks = [
        asyncio.create_task(
            _worker(i, queue, client, db_conn, pbar_detail),
            name=f"worker-{i}",
        )
        for i in range(WORKERS)
    ]

    prod_counts = await _producer(queue, client, total_pages, scraped_hashes, pbar_listing)

    await queue.join()
    for _ in range(WORKERS):
        await queue.put(_STOP)
    await asyncio.gather(*worker_tasks)

    pbar_listing.close()
    pbar_detail.close()
    await client.aclose()

    stats = await get_stats(db_conn)
    await db_conn.close()

    print(
        f"\n{'='*60}\n"
        f"  CPPP Scraper — Complete\n"
        f"{'='*60}\n"
        f"  New tenders    : {prod_counts['new']:,}\n"
        f"  Updated        : {prod_counts['updated']:,}\n"
        f"  Unchanged/skip : {prod_counts['unchanged']:,}\n"
        f"  Total in DB    : {stats['total']:,}\n"
        f"  Detail fetched : {stats['detail_fetched']:,}\n"
        f"  Token expired  : {stats['token_expired']:,}\n"
        f"  Fetch errors   : {stats['fetch_error']:,}\n"
        f"  Pending sync   : {stats['pending_sync']:,}\n"
        f"  Synced         : {stats['synced']:,}\n"
        f"{'='*60}\n"
    )
