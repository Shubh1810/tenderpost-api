"""
logger.py — Centralized structured logging for TenderPost scraper.

Usage:
    from logger import log, log_scraper, log_supabase, timer, run_summary

    log.info("Starting scrape")
    log_scraper.success("Scraped 42 tenders")
    log_supabase.warning("Upsert batch failed")

    with timer("listing_central", stage="SCRAPER"):
        ...  # logs start + elapsed time automatically

    run_summary.record("listing_tenders_central", 150)
    run_summary.print()
"""

import os
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict

from loguru import logger

# ── Configuration ──────────────────────────────────────────────────────────────

LOG_LEVEL   = os.getenv("LOG_LEVEL", "DEBUG").upper()
LOG_FORMAT  = os.getenv("LOG_FORMAT", "pretty")   # "pretty" | "json"
LOG_FILE    = os.getenv("LOG_FILE", "")            # optional path e.g. "logs/scraper.log"

# ── Sink setup ─────────────────────────────────────────────────────────────────

logger.remove()  # Remove default handler

PRETTY_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
    "| <level>{level: <8}</level> "
    "| <cyan>{extra[stage]: <22}</cyan> "
    "| {message}"
)

JSON_FORMAT = (
    '{{"time":"{time:YYYY-MM-DD HH:mm:ss.SSS}",'
    '"level":"{level}",'
    '"stage":"{extra[stage]}",'
    '"message":"{message}"}}'
)

fmt = JSON_FORMAT if LOG_FORMAT == "json" else PRETTY_FORMAT

logger.configure(extra={"stage": "SYSTEM"})

logger.add(
    sys.stdout,
    format=fmt,
    level=LOG_LEVEL,
    colorize=(LOG_FORMAT != "json"),
    enqueue=False,
)

if LOG_FILE:
    logger.add(
        LOG_FILE,
        format=JSON_FORMAT,
        level=LOG_LEVEL,
        rotation="50 MB",
        retention="7 days",
        compression="zip",
        enqueue=True,
    )


# ── Stage-bound logger factory ─────────────────────────────────────────────────

def get_logger(stage: str):
    """Return a loguru logger bound to a named stage label."""
    return logger.bind(stage=stage)


# ── Pre-bound loggers per domain ───────────────────────────────────────────────

log          = get_logger("PIPELINE")
log_scraper  = get_logger("SCRAPER")
log_supabase = get_logger("SUPABASE")


# ── Timing context manager ─────────────────────────────────────────────────────

@contextmanager
def timer(label: str, stage: str = "PIPELINE"):
    """
    Context manager that logs start + elapsed time for any code block.

    Usage:
        with timer("cloudflare_submit", stage="CLOUDFLARE"):
            await submit_jobs(...)
    """
    bound = get_logger(stage)
    bound.info(f"⏱  START  {label}")
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        bound.info(f"⏱  END    {label} — {elapsed:.2f}s")


# ── Run-level metrics accumulator ──────────────────────────────────────────────

class RunSummary:
    """
    Accumulates metrics across the whole cron run and prints a
    formatted summary table at the end.

    Usage:
        run_summary.record("tenders_scraped", 200)
        run_summary.increment("cf_jobs_submitted")
        run_summary.print()
    """

    def __init__(self) -> None:
        self._metrics: Dict[str, Any] = {}
        self._t0 = time.perf_counter()

    def record(self, key: str, value: Any) -> None:
        self._metrics[key] = value

    def increment(self, key: str, by: int = 1) -> None:
        self._metrics[key] = self._metrics.get(key, 0) + by

    def elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def print(self) -> None:
        elapsed = self.elapsed()
        sep = "=" * 58
        log.info(sep)
        log.info("  TENDERPOST — CPPP PIPELINE COMPLETE")
        log.info(sep)
        log.info(f"  Total run time       : {elapsed:.1f}s  ({elapsed/60:.1f} min)")

        sections: Dict[str, list] = {
            "DB State (pre-run)": [],
            "Scrape": [],
            "Detail": [],
            "Upsert": [],
            "Other": [],
        }

        key_map = {
            "known_central_detailed":    ("DB State (pre-run)", "Central — already detailed"),
            "known_central_no_detail":   ("DB State (pre-run)", "Central — exists, no detail"),
            "known_state_detailed":      ("DB State (pre-run)", "State — already detailed"),
            "known_state_no_detail":     ("DB State (pre-run)", "State — exists, no detail"),
            "listing_tenders_central":   ("Scrape",  "Central tenders scraped"),
            "listing_pages_central":     ("Scrape",  "Central pages scraped"),
            "listing_tenders_state":     ("Scrape",  "State tenders scraped"),
            "listing_pages_state":       ("Scrape",  "State pages scraped"),
            "early_stop_central":        ("Scrape",  "Central early stop triggered"),
            "early_stop_state":          ("Scrape",  "State early stop triggered"),
            "detail_pages_fetched":      ("Detail",  "Detail pages fetched"),
            "detail_pages_with_data":    ("Detail",  "Detail pages with data"),
            "detail_written":            ("Detail",  "Written to Supabase"),
            "detail_no_fields":          ("Detail",  "No fields extracted"),
            "upsert_full":               ("Upsert",  "Full upsert (new/undetailed)"),
            "upsert_listing_only":       ("Upsert",  "Listing-only upsert (complete)"),
            "upsert_total":              ("Upsert",  "Total rows upserted"),
            "marked_inactive":           ("Upsert",  "Marked inactive"),
        }

        for key, value in self._metrics.items():
            section, label = key_map.get(key, ("Other", key))
            sections[section].append((label, value))

        for section, items in sections.items():
            if not items:
                continue
            log.info(f"\n  [{section}]")
            for label, value in items:
                log.info(f"    {label:<38}: {value}")

        log.info(sep)


run_summary = RunSummary()
