"""
logger.py — Centralized structured logging for TenderPost scraper.

Usage:
    from logger import log, timer, run_summary

    log.info("Starting scrape")
    log.success("Upserted 42 tenders")
    log.warning("CAPTCHA solve took too long")
    log.error("Cloudflare job failed", job_id=job_id, url=url)

    with timer("cloudflare_detail_fetch"):
        ...  # automatically logs start + end with elapsed time

    run_summary.record("tenders_scraped", 150)
    run_summary.record("cf_jobs_submitted", 30)
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
log_cf       = get_logger("CLOUDFLARE")
log_embed    = get_logger("EMBEDDINGS")
log_supabase = get_logger("SUPABASE")
log_cppp     = get_logger("CPPP")
log_captcha  = get_logger("CAPTCHA")


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
        log.info("  TENDERPOST — RUN COMPLETE")
        log.info(sep)
        log.info(f"  Total run time       : {elapsed:.1f}s  ({elapsed/60:.1f} min)")

        sections: Dict[str, list] = {
            "DB State (pre-run)": [],
            "Scrape": [],
            "Cloudflare": [],
            "Upsert": [],
            "Embeddings": [],
            "Other": [],
        }

        key_map = {
            "known_already_detailed":   ("DB State (pre-run)", "Already detailed in DB"),
            "known_exists_no_detail":   ("DB State (pre-run)", "Exists, no detail in DB"),
            "listing_pages_scraped":    ("Scrape",             "Listing pages scraped"),
            "listing_tenders_found":    ("Scrape",             "Tenders found on listing"),
            "early_stop_triggered":     ("Scrape",             "Early stop triggered"),
            "captcha_solved":           ("Scrape",             "CAPTCHA solved"),
            "captcha_time_s":           ("Scrape",             "CAPTCHA solve time (s)"),
            "cf_jobs_submitted":        ("Cloudflare",         "Jobs submitted"),
            "cf_jobs_skipped":          ("Cloudflare",         "Jobs skipped (already complete)"),
            "cf_jobs_succeeded":        ("Cloudflare",         "Jobs succeeded"),
            "cf_jobs_failed":           ("Cloudflare",         "Jobs failed"),
            "cf_fallback_used":         ("Cloudflare",         "Fallback (direct httpx) used"),
            "cf_fields_extracted":      ("Cloudflare",         "Tenders with detail fields"),
            "upsert_full":              ("Upsert",             "Full upsert (new/undetailed)"),
            "upsert_listing_only":      ("Upsert",             "Listing-only upsert (complete)"),
            "upsert_total":             ("Upsert",             "Total rows upserted"),
            "marked_inactive":          ("Upsert",             "Marked inactive"),
            "embed_attempted":          ("Embeddings",         "Attempted"),
            "embed_succeeded":          ("Embeddings",         "Succeeded"),
            "embed_failed":             ("Embeddings",         "Failed"),
            "embed_skipped_no_fields":  ("Embeddings",         "Skipped (no embeddable fields)"),
            "embed_total_time_s":       ("Embeddings",         "Total time (s)"),
            "embed_avg_time_s":         ("Embeddings",         "Avg per tender (s)"),
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
