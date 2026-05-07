"""
db.py — All aiosqlite operations: init, save, query, mark sync status.

Change-detection strategy (for efficient twice-daily cron):
  Each tender row stores a content_hash = sha256 of mutable listing fields
  (title, bid_close_date, open_date, corrigendum).
  On re-scrape the producer compares new vs stored hash:
    - hash unchanged → skip entirely (no detail fetch, no Supabase upsert)
    - hash changed   → re-fetch detail + push update to Supabase
    - new tender     → full fetch + insert to Supabase
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiosqlite

from .config import DB_PATH

# ── Schema DDL ────────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS tenders (
    tender_id        TEXT PRIMARY KEY,
    title            TEXT,
    ref_no           TEXT,
    state            TEXT,
    published_date   TEXT,
    bid_close_date   TEXT,
    open_date        TEXT,
    corrigendum      TEXT,
    detail_url       TEXT,
    scraped_at       TEXT,
    content_hash     TEXT,
    detail_fetched   INTEGER DEFAULT 0,
    token_expired    INTEGER DEFAULT 0,
    fetch_error      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tender_details (
    tender_id          TEXT PRIMARY KEY REFERENCES tenders(tender_id),
    organisation_name  TEXT,
    tender_value       TEXT,
    emd_amount         TEXT,
    tender_type        TEXT,
    product_category   TEXT,
    pincode            TEXT,
    location           TEXT,
    raw_json           TEXT,
    synced_to_supabase INTEGER DEFAULT 0
);
"""

# Migration: add content_hash to existing DBs that predate this column
_MIGRATE = """
ALTER TABLE tenders ADD COLUMN content_hash TEXT;
"""


def compute_hash(tender: Dict[str, Any]) -> str:
    """
    Short SHA-256 hash of the mutable listing fields we care about.
    If any of these change (e.g. corrigendum extends closing date),
    the tender is treated as updated and re-scraped.
    """
    key = "|".join([
        tender.get("title", ""),
        tender.get("bid_close_date", ""),
        tender.get("open_date", ""),
        tender.get("corrigendum", ""),
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def init_db(db_path: Path = DB_PATH) -> aiosqlite.Connection:
    """Open (or create) the SQLite database and ensure schema exists."""
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_DDL)
    await conn.commit()
    # Add content_hash column to existing DBs (safe to run multiple times)
    try:
        await conn.execute(_MIGRATE)
        await conn.commit()
    except Exception:
        pass  # column already exists
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    return conn


async def get_scraped_hashes(conn: aiosqlite.Connection) -> Dict[str, str]:
    """
    Return {tender_id: content_hash} for all rows already in the DB.
    Used by the producer to skip unchanged tenders on re-scrape.
    A missing hash (NULL) is treated as '' — the new hash will always differ,
    forcing a re-scrape for rows inserted before hashing was added.
    """
    async with conn.execute(
        "SELECT tender_id, COALESCE(content_hash, '') AS content_hash FROM tenders"
    ) as cur:
        rows = await cur.fetchall()
    return {r["tender_id"]: r["content_hash"] for r in rows}


async def save_tender(conn: aiosqlite.Connection, tender: Dict[str, Any]) -> None:
    """
    Upsert a listing-page tender row, updating mutable fields and content_hash.
    Uses INSERT OR REPLACE so updates (changed hash) overwrite the old row
    while preserving detail_fetched / token_expired / fetch_error flags via
    a DO UPDATE clause.
    """
    now = datetime.now(timezone.utc).isoformat()
    content_hash = tender.get("content_hash") or compute_hash(tender)
    await conn.execute(
        """
        INSERT INTO tenders
            (tender_id, title, ref_no, state, published_date,
             bid_close_date, open_date, corrigendum, detail_url,
             scraped_at, content_hash)
        VALUES
            (:tender_id, :title, :ref_no, :state, :published_date,
             :bid_close_date, :open_date, :corrigendum, :detail_url,
             :scraped_at, :content_hash)
        ON CONFLICT(tender_id) DO UPDATE SET
            title          = excluded.title,
            bid_close_date = excluded.bid_close_date,
            open_date      = excluded.open_date,
            corrigendum    = excluded.corrigendum,
            detail_url     = excluded.detail_url,
            scraped_at     = excluded.scraped_at,
            content_hash   = excluded.content_hash,
            -- reset flags so detail is re-fetched on update
            detail_fetched = 0,
            token_expired  = 0,
            fetch_error    = 0
        """,
        {**tender, "scraped_at": now, "content_hash": content_hash},
    )
    await conn.commit()


async def save_tender_detail(
    conn: aiosqlite.Connection, tender_id: str, detail: Dict[str, Any]
) -> None:
    """Upsert detail-page fields and mark detail_fetched=1."""
    raw_json = json.dumps(detail, ensure_ascii=False)
    await conn.execute(
        """
        INSERT INTO tender_details
            (tender_id, organisation_name, tender_value, emd_amount,
             tender_type, product_category, pincode, location, raw_json)
        VALUES
            (:tender_id, :organisation_name, :tender_value, :emd_amount,
             :tender_type, :product_category, :pincode, :location, :raw_json)
        ON CONFLICT(tender_id) DO UPDATE SET
            organisation_name  = excluded.organisation_name,
            tender_value       = excluded.tender_value,
            emd_amount         = excluded.emd_amount,
            tender_type        = excluded.tender_type,
            product_category   = excluded.product_category,
            pincode            = excluded.pincode,
            location           = excluded.location,
            raw_json           = excluded.raw_json,
            -- re-queue for Supabase sync (data changed)
            synced_to_supabase = 0
        """,
        {
            "tender_id": tender_id,
            "organisation_name": detail.get("Organisation Name"),
            "tender_value": detail.get("Tender Value")
                            or detail.get("Tender Value in INR (In Words)"),
            "emd_amount": detail.get("EMD Amount") or detail.get("EMD"),
            "tender_type": detail.get("Tender Type"),
            "product_category": detail.get("Product Category")
                                 or detail.get("Tender Category"),
            "pincode": detail.get("Pincode"),
            "location": detail.get("Location"),
            "raw_json": raw_json,
        },
    )
    await conn.execute(
        "UPDATE tenders SET detail_fetched=1 WHERE tender_id=?", (tender_id,)
    )
    await conn.commit()


async def mark_token_expired(conn: aiosqlite.Connection, tender_id: str) -> None:
    await conn.execute(
        "UPDATE tenders SET token_expired=1, detail_fetched=1 WHERE tender_id=?",
        (tender_id,),
    )
    await conn.commit()


async def mark_fetch_error(conn: aiosqlite.Connection, tender_id: str) -> None:
    await conn.execute(
        "UPDATE tenders SET fetch_error=1 WHERE tender_id=?", (tender_id,)
    )
    await conn.commit()


async def mark_synced(conn: aiosqlite.Connection, tender_ids: List[str]) -> None:
    await conn.executemany(
        "UPDATE tender_details SET synced_to_supabase=1 WHERE tender_id=?",
        [(tid,) for tid in tender_ids],
    )
    await conn.commit()


async def get_unsynced(
    conn: aiosqlite.Connection, limit: int = 1000
) -> List[Dict[str, Any]]:
    """Rows with detail data not yet pushed to Supabase."""
    async with conn.execute(
        """
        SELECT
            t.tender_id, t.title, t.ref_no, t.state,
            t.published_date, t.bid_close_date, t.open_date,
            t.corrigendum, t.detail_url, t.scraped_at,
            td.organisation_name, td.tender_value, td.emd_amount,
            td.tender_type, td.product_category, td.pincode,
            td.location, td.raw_json
        FROM tenders t
        JOIN tender_details td ON t.tender_id = td.tender_id
        WHERE td.synced_to_supabase = 0
        LIMIT ?
        """,
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_stats(conn: aiosqlite.Connection) -> Dict[str, int]:
    async with conn.execute("SELECT COUNT(*) FROM tenders") as cur:
        total = (await cur.fetchone())[0]
    async with conn.execute(
        "SELECT COUNT(*) FROM tenders WHERE detail_fetched=1"
    ) as cur:
        detail_fetched = (await cur.fetchone())[0]
    async with conn.execute(
        "SELECT COUNT(*) FROM tenders WHERE token_expired=1"
    ) as cur:
        token_expired = (await cur.fetchone())[0]
    async with conn.execute(
        "SELECT COUNT(*) FROM tenders WHERE fetch_error=1"
    ) as cur:
        fetch_error = (await cur.fetchone())[0]
    async with conn.execute(
        "SELECT COUNT(*) FROM tender_details WHERE synced_to_supabase=1"
    ) as cur:
        synced = (await cur.fetchone())[0]
    async with conn.execute(
        "SELECT COUNT(*) FROM tender_details WHERE synced_to_supabase=0"
    ) as cur:
        pending_sync = (await cur.fetchone())[0]
    return {
        "total": total,
        "detail_fetched": detail_fetched,
        "token_expired": token_expired,
        "fetch_error": fetch_error,
        "synced": synced,
        "pending_sync": pending_sync,
    }
