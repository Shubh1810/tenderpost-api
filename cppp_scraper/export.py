"""
export.py — Dump SQLite tenders + details to a CSV via pandas.
"""
import asyncio

import aiosqlite
import pandas as pd
from loguru import logger

from .config import CSV_EXPORT_PATH, DB_PATH


async def _load_rows() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT
                t.tender_id, t.title, t.ref_no, t.state,
                t.published_date, t.bid_close_date, t.open_date,
                t.corrigendum, t.detail_url, t.scraped_at,
                t.detail_fetched, t.token_expired, t.fetch_error,
                td.organisation_name, td.tender_value, td.emd_amount,
                td.tender_type, td.product_category, td.pincode,
                td.location, td.raw_json, td.synced_to_supabase
            FROM tenders t
            LEFT JOIN tender_details td ON t.tender_id = td.tender_id
            ORDER BY t.rowid
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def export_to_csv(output_path=None) -> None:
    """Export all tenders to CSV. Defaults to CSV_EXPORT_PATH from config."""
    path = output_path or CSV_EXPORT_PATH
    logger.info("[export] loading rows from SQLite …")
    rows = await _load_rows()
    if not rows:
        print("[export] no rows to export.")
        return

    df = pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[export] {len(df):,} rows written to {path}")
    logger.info(f"[export] done — {len(df):,} rows → {path}")
