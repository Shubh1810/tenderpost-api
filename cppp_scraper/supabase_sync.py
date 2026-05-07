"""
supabase_sync.py — Push SQLite rows into the Supabase `tenders` table.

Schema target (actual table):
    tenders (id uuid PK, ref_no text NOT NULL, source text, title text NOT NULL,
             url text NOT NULL, tender_id text, organisation text,
             published_date timestamptz, closing_date timestamptz,
             opening_date timestamptz, tender_value numeric, emd_amount numeric,
             tender_type text, tender_category text, product_category text,
             sub_category text, location text, pincode text,
             work_description text, contract_type text,
             period_of_work_days int, bid_validity_days int,
             detail_scraped bool, raw_data jsonb, is_active bool,
             created_at timestamptz, updated_at timestamptz)
    UNIQUE (ref_no, source)

For CPPP tenders:
    ref_no = tender_id  (decoded stable ID like '2026_CEUCZ_1129415_1')
    source = 'cppp'
This guarantees uniqueness and matches the existing constraint.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from loguru import logger
from supabase import create_client, Client

from .config import DB_PATH, SUPABASE_BATCH
from .db import get_stats, get_unsynced, init_db, mark_synced

load_dotenv(Path(__file__).parent / ".env", override=False)
load_dotenv(Path(__file__).parent.parent / ".env", override=False)


def _get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must be set in cppp_scraper/.env"
        )
    return create_client(url, key)


def _parse_numeric(raw: Optional[str]) -> Optional[float]:
    """Strip ₹/Rs/commas and return a float, or None."""
    if not raw:
        return None
    cleaned = re.sub(r"[₹Rs.,\s]", "", raw.strip())
    m = re.match(r"[\d.]+", cleaned)
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return None


def _parse_date(raw: Optional[str]) -> Optional[str]:
    """
    Convert Indian date strings to ISO-8601.
    Handles '28-Mar-2026', '28-Mar-2026 06:55 PM', '28/03/2026', etc.
    Returns ISO string or None.
    """
    if not raw:
        return None
    raw = raw.strip()
    # Extract 'DD-Mon-YYYY' from strings like '28-Mar-2026 06:55 PM'
    m = re.search(r"(\d{1,2}-\w{3}-\d{4})", raw)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d-%b-%Y").strftime("%Y-%m-%dT00:00:00+00:00")
        except ValueError:
            pass
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).strftime("%Y-%m-%dT00:00:00+00:00")
        except ValueError:
            continue
    return None


def _build_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map a joined SQLite row (tenders + tender_details) to the
    Supabase tenders table schema.
    """
    raw: Dict[str, Any] = {}
    if row.get("raw_json"):
        try:
            raw = json.loads(row["raw_json"])
        except Exception:
            pass

    tender_id = row["tender_id"]
    title = (row.get("title") or "").strip() or "Untitled"
    detail_url = row.get("detail_url") or ""

    record: Dict[str, Any] = {
        # ── conflict key ──────────────────────────────────────────────────────
        # ref_no = tender_id for CPPP (unique, stable, matches (ref_no,source) UK)
        "ref_no":   tender_id,
        "source":   "cppp",
        # ── required NOT NULL fields ──────────────────────────────────────────
        "title":    title,
        "url":      detail_url or f"https://eprocure.gov.in/cppp/tendersfullviewmmp/{tender_id}",
        # ── identifiers ───────────────────────────────────────────────────────
        "tender_id":   tender_id,
        "organisation": (row.get("organisation_name") or "").strip() or None,
        # ── dates ─────────────────────────────────────────────────────────────
        "published_date": _parse_date(row.get("published_date")),
        "closing_date":   _parse_date(row.get("bid_close_date")),
        "opening_date":   _parse_date(row.get("open_date")),
        # ── financials ────────────────────────────────────────────────────────
        "tender_value": _parse_numeric(row.get("tender_value")),
        "emd_amount":   _parse_numeric(row.get("emd_amount")),
        # ── classification ────────────────────────────────────────────────────
        "tender_type":     row.get("tender_type"),
        "tender_category": raw.get("Tender Category"),
        "product_category": row.get("product_category"),
        "location":  row.get("location"),
        "pincode":   row.get("pincode"),
        # ── description ───────────────────────────────────────────────────────
        "work_description": raw.get("Work Description") or raw.get("Tender Title"),
        # ── flags ─────────────────────────────────────────────────────────────
        "is_active":     True,
        "detail_scraped": bool(
            row.get("tender_type")
            or row.get("product_category")
            or raw.get("Work Description")
        ),
        # ── overflow JSON ─────────────────────────────────────────────────────
        "raw_data": {
            "state":         row.get("state"),
            "corrigendum":   row.get("corrigendum"),
            "org_type":      raw.get("Organisation Type"),
            "tender_fee":    raw.get("Tender Fee"),
            "bid_open_date": raw.get("Bid Opening Date"),
            "epublished":    raw.get("ePublished Date"),
            "inviting_name": raw.get("Name"),
            "inviting_addr": raw.get("Address"),
        },
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }

    # Drop None values — Supabase won't overwrite good existing data with nulls
    return {k: v for k, v in record.items() if v is not None}


async def sync_to_supabase(batch_limit: int = 5000) -> Dict[str, int]:
    """
    Push all unsynced rows to the Supabase `tenders` table.
    Upserts on (ref_no, source) — no schema migration required.
    Safe to re-run: only processes synced_to_supabase=0 rows.
    """
    supabase = _get_supabase_client()
    db_conn = await init_db(DB_PATH)
    total_synced = 0
    total_failed = 0

    logger.info("[sync] starting Supabase sync → tenders table …")

    while True:
        rows = await get_unsynced(db_conn, limit=batch_limit)
        if not rows:
            break

        for i in range(0, len(rows), SUPABASE_BATCH):
            chunk = rows[i : i + SUPABASE_BATCH]
            payload = [_build_row(r) for r in chunk]
            tender_ids = [r["tender_id"] for r in chunk]

            try:
                supabase.table("tenders").upsert(
                    payload,
                    on_conflict="ref_no,source",
                ).execute()
                await mark_synced(db_conn, tender_ids)
                total_synced += len(chunk)
                logger.info(f"[sync] upserted {len(chunk)} (total {total_synced:,})")
            except Exception as exc:
                total_failed += len(chunk)
                logger.error(f"[sync] batch failed: {exc}")

        if len(rows) < batch_limit:
            break

    await db_conn.close()
    print(f"\n[sync] complete — synced: {total_synced:,}  failed: {total_failed:,}")
    return {"synced": total_synced, "failed": total_failed}


MIGRATION_SQL = """
-- ================================================================
-- Run this ONCE in your Supabase SQL Editor before the first scrape.
-- ================================================================

-- Your tenders table already exists with the right schema.
-- The cppp_scraper uses the existing UNIQUE (ref_no, source) constraint.
-- For CPPP rows: ref_no = tender_id (e.g. '2026_CEUCZ_1129415_1')
--                source = 'cppp'

-- Only two additions needed:

-- 1. Faster lookup by tender_id (optional but recommended)
CREATE INDEX IF NOT EXISTS idx_tenders_tender_id
    ON public.tenders (tender_id)
    WHERE tender_id IS NOT NULL;

-- 2. Index on source for filtering CPPP vs eprocure rows
CREATE INDEX IF NOT EXISTS idx_tenders_source
    ON public.tenders (source);

-- That's it — no new tables, no column changes needed.
"""
