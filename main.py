"""
FastAPI application for TenderPost scraper.

Endpoints:
- GET /health                  - Health check
- GET /api/tenders/central     - Trigger central CPPP scrape (cpppdata)
- GET /api/tenders/state       - Trigger state CPPP scrape (mmpdata)
- GET /api/tenders/status      - DB pipeline health metrics
"""

import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional

from scraper import HEADERS, scrape_listing

load_dotenv()

app = FastAPI(
    title="TenderPost Scraper API",
    description="Tender scraper — httpx + selectolax, no browser required",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Response models ───────────────────────────────────────────────────────────

class TenderItem(BaseModel):
    title:          str
    ref_no:         Optional[str] = None
    closing_date:   Optional[str] = None
    opening_date:   Optional[str] = None
    published_date: Optional[str] = None
    organisation:   Optional[str] = None
    url:            Optional[str] = None
    source:         Optional[str] = None


class TendersResponse(BaseModel):
    success:     bool
    source:      str
    count:       int
    total_pages: int
    total_count: Optional[int] = None
    items:       List[TenderItem] = Field(default_factory=list)
    error:       Optional[str] = None


class HealthResponse(BaseModel):
    status:  str
    service: str
    version: str


class ScrapeStatusResponse(BaseModel):
    tender_count:          int
    detail_scraped_count:  int
    pending_detail_count:  int
    last_updated_at:       Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", tags=["Root"])
async def root() -> dict:
    return {
        "service": "TenderPost Scraper API",
        "version": "2.0.0",
        "endpoints": {
            "health":          "/health",
            "central_tenders": "/api/tenders/central",
            "state_tenders":   "/api/tenders/state",
            "status":          "/api/tenders/status",
            "docs":            "/docs",
        },
    }


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        service="TenderPost Scraper",
        version="2.0.0",
    )


@app.get("/api/tenders/central", response_model=TendersResponse, tags=["Tenders"])
async def get_central_tenders() -> TendersResponse:
    """
    Scrape central government tenders from CPPP (cpppdata).
    No CAPTCHA. Returns listing fields only (no detail pages).
    """
    async with httpx.AsyncClient(headers=HEADERS) as client:
        result = await scrape_listing(source="central", client=client)

    if result.get("success"):
        return TendersResponse(
            success=True,
            source="cppp/central",
            count=len(result["tenders"]),
            total_pages=result["total_pages"],
            total_count=result.get("total_count"),
            items=result["tenders"],
        )
    return TendersResponse(
        success=False,
        source="cppp/central",
        count=0,
        total_pages=0,
        error=result.get("error", "Unknown error"),
    )


@app.get("/api/tenders/state", response_model=TendersResponse, tags=["Tenders"])
async def get_state_tenders() -> TendersResponse:
    """
    Scrape state government tenders from CPPP (mmpdata).
    No CAPTCHA. Returns listing fields only (no detail pages).
    """
    async with httpx.AsyncClient(headers=HEADERS) as client:
        result = await scrape_listing(source="state", client=client)

    if result.get("success"):
        return TendersResponse(
            success=True,
            source="cppp/state",
            count=len(result["tenders"]),
            total_pages=result["total_pages"],
            total_count=result.get("total_count"),
            items=result["tenders"],
        )
    return TendersResponse(
        success=False,
        source="cppp/state",
        count=0,
        total_pages=0,
        error=result.get("error", "Unknown error"),
    )


@app.get("/api/tenders/status", response_model=ScrapeStatusResponse, tags=["Tenders"])
async def get_scrape_status() -> ScrapeStatusResponse:
    """Read-only DB health — counts and most-recently updated tender."""
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url or not supabase_key:
        return ScrapeStatusResponse(
            tender_count=0, detail_scraped_count=0, pending_detail_count=0
        )

    from supabase import create_client
    client = create_client(supabase_url, supabase_key)

    try:
        total_res   = client.table("tenders").select("id", count="exact").execute()
        detail_res  = client.table("tenders").select("id", count="exact").eq("detail_scraped", True).execute()
        pending_res = client.table("tenders").select("id", count="exact").eq("detail_scraped", False).execute()
        recent_res  = (
            client.table("tenders")
            .select("updated_at")
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        last_updated = (recent_res.data[0].get("updated_at") if recent_res.data else None)

        return ScrapeStatusResponse(
            tender_count=total_res.count or 0,
            detail_scraped_count=detail_res.count or 0,
            pending_detail_count=pending_res.count or 0,
            last_updated_at=last_updated,
        )
    except Exception:
        return ScrapeStatusResponse(
            tender_count=0, detail_scraped_count=0, pending_detail_count=0
        )


# ── Startup / shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    print("TenderPost Scraper API v2.0 starting...")
    print(f"Supabase: {'configured' if os.getenv('SUPABASE_URL') else 'NOT configured'}")


@app.on_event("shutdown")
async def shutdown_event():
    print("TenderPost Scraper API shutting down.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        log_level="info",
    )
