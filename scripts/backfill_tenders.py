"""
One-time backfill script: unpack latest_snapshot.payload into individual tenders rows.

Run this ONCE after the migration to seed your tenders table.

Usage:
    python scripts/backfill_tenders.py
"""

import os
import re
import sys
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


# ── helpers ──────────────────────────────────────────────────────────────────

def parse_date(date_str: Optional[str]) -> Optional[str]:
    """
    Convert eprocure date strings to ISO 8601 for Postgres.

    Handles:
        "26-Feb-2026 10:00 AM"  →  "2026-02-26T10:00:00"
        "26-Feb-2026"           →  "2026-02-26T00:00:00"
        None / ""               →  None
    """
    if not date_str or not date_str.strip():
        return None

    date_str = date_str.strip()
    formats = [
        "%d-%b-%Y %I:%M %p",   # "26-Feb-2026 10:00 AM"
        "%d-%b-%Y %H:%M",      # "26-Feb-2026 10:00"
        "%d-%b-%Y",            # "26-Feb-2026"
        "%d/%m/%Y",            # "26/02/2026"
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).isoformat()
        except ValueError:
            continue

    # Return None if nothing matched — don't crash on unknown formats
    print(f"  ⚠️  Could not parse date: '{date_str}' — storing as NULL")
    return None


def build_tender_row(item: dict) -> dict:
    """
    Map a raw payload item to a tenders table row.
    Unknown fields go into raw_data as a safety net.
    """
    known_fields = {"url", "title", "ref_no", "closing_date",
                    "opening_date", "organisation", "published_date"}

    raw_data = {k: v for k, v in item.items() if k not in known_fields}

    return {
        "ref_no":           item.get("ref_no", "").strip() or "UNKNOWN",
        "source":           "eprocure",
        "title":            (item.get("title") or "").strip(),
        "organisation":     (item.get("organisation") or "").strip() or None,
        "url":              (item.get("url") or "").strip(),
        "published_date":   parse_date(item.get("published_date")),
        "closing_date":     parse_date(item.get("closing_date")),
        "opening_date":     parse_date(item.get("opening_date")),
        "detail_scraped":   False,
        "is_active":        True,
        "raw_data":         raw_data if raw_data else {},
    }


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        print("❌  SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")
        sys.exit(1)

    client = create_client(url, key)

    # ── 1. Pull latest_snapshot payload ──────────────────────────────────────
    print("📦  Fetching latest_snapshot...")
    result = client.table("latest_snapshot").select("payload, count, scraped_at").eq("id", 1).execute()

    if not result.data:
        print("❌  latest_snapshot is empty — run a scrape first.")
        sys.exit(1)

    snapshot    = result.data[0]
    payload     = snapshot["payload"]        # list of raw tender dicts
    total_raw   = len(payload)
    scraped_at  = snapshot.get("scraped_at", "unknown")

    print(f"✅  Snapshot loaded: {total_raw} items (scraped_at: {scraped_at})")

    # ── 2. Check existing tenders to avoid duplicates ────────────────────────
    print("\n🔍  Checking existing tenders table...")
    existing = client.table("tenders").select("ref_no", count="exact").execute()
    existing_count = existing.count or 0
    print(f"   Found {existing_count} existing rows in tenders table")

    existing_refs = set()
    if existing_count > 0:
        # Fetch all existing ref_nos for dedup
        existing_rows = client.table("tenders").select("ref_no").execute()
        existing_refs = {row["ref_no"] for row in existing_rows.data}
        print(f"   Will skip {len(existing_refs)} already-existing ref_nos")

    # ── 3. Build rows ─────────────────────────────────────────────────────────
    print("\n🔨  Building tender rows...")
    rows         = []
    skipped_dup  = 0
    skipped_bad  = 0

    for item in payload:
        row = build_tender_row(item)

        # Skip if no title or url
        if not row["title"] or not row["url"]:
            skipped_bad += 1
            continue

        # Skip duplicates
        if row["ref_no"] in existing_refs:
            skipped_dup += 1
            continue

        rows.append(row)

    # Dedupe by (ref_no, source) so upsert batches never have duplicate conflict keys
    rows_before_dedup = len(rows)
    seen = {}
    for row in rows:
        seen[(row["ref_no"], row["source"])] = row
    rows = list(seen.values())
    skipped_dedup_snapshot = rows_before_dedup - len(rows)

    print(f"   ✅  {len(rows)} new rows to insert")
    print(f"   ⏭️   {skipped_dup} skipped (already in DB)")
    print(f"   🔄  {skipped_dedup_snapshot} skipped (duplicate ref_no within snapshot)")
    print(f"   🗑️   {skipped_bad} skipped (missing title or url)")

    if not rows:
        print("\n✅  Nothing to insert. Tenders table is already up to date.")
        return

    # ── 4. Batch insert (100 rows at a time — Supabase safe limit) ────────────
    print(f"\n💾  Inserting {len(rows)} rows in batches of 100...")
    BATCH = 100
    inserted = 0
    failed   = 0

    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        batch_num = (i // BATCH) + 1
        total_batches = (len(rows) + BATCH - 1) // BATCH

        try:
            # NEW - upsert safely skips existing rows
            client.table("tenders").upsert(
                batch,
                on_conflict="ref_no,source"
            ).execute()
            inserted += len(batch)
            print(f"   Batch {batch_num}/{total_batches} — inserted {len(batch)} rows "
                  f"(running total: {inserted})")
        except Exception as e:
            failed += len(batch)
            print(f"   ❌  Batch {batch_num} failed: {e}")

    # ── 5. Summary ────────────────────────────────────────────────────────────
    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅  Backfill complete
    Raw items in snapshot : {total_raw}
    Inserted              : {inserted}
    Skipped (already in DB): {skipped_dup}
    Skipped (dup in snapshot): {skipped_dedup_snapshot}
    Skipped (bad data)    : {skipped_bad}
    Failed batches        : {failed}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Next step: run the detail scraper to fill
work_description, tender_value, etc.
""")


if __name__ == "__main__":
    main()