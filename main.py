"""
FastAPI application for TenderPost scraper.

Endpoints:
- GET /api/tenders        - Get tender data (listing only; detail via Cloudflare cron)
- GET /api/tenders/latest - Get latest active tenders (no CAPTCHA)
- GET /api/tenders/status - Scrape pipeline health and data completeness metrics
- GET /health             - Health check endpoint
- GET /api/test-2captcha  - Test 2Captcha connectivity
"""

import os
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from captcha.solver import test_2captcha_connectivity
from scraper import scrape_latest_active_tenders, scrape_listing_pages, scrape_tenders_crawl4ai_playwright
from supabase_client import save_to_supabase

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI(
    title="TenderPost Scraper API",
    description="Production-grade tender scraper using Crawl4AI + Playwright + 2Captcha",
    version="1.0.0",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure based on your needs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== Response Models ====================


class TenderItemResponse(BaseModel):
    """Single tender item response model."""

    title: str = Field(..., description="Tender title/description")
    ref_no: Optional[str] = Field(None, description="Reference number/Tender ID")
    closing_date: Optional[str] = Field(None, description="Bid closing date")
    opening_date: Optional[str] = Field(None, description="Bid opening date")
    published_date: Optional[str] = Field(None, description="Publication date")
    organisation: Optional[str] = Field(None, description="Organization/Department name")
    url: Optional[str] = Field(None, description="Tender detail page URL")


class TendersResponse(BaseModel):
    """Response model for tender list."""

    success: bool = Field(..., description="Whether the operation was successful")
    source: str = Field(..., description="Data source URL or identifier")
    count: int = Field(..., description="Number of tenders in this response")
    total_pages: int = Field(..., description="Total pages scraped")
    live_tenders: Optional[int] = Field(None, description="Total live tenders available")
    items: List[TenderItemResponse] = Field(..., description="List of tender items")
    error: Optional[str] = Field(None, description="Error message if success=false")


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    service: str
    version: str
    captcha_api: Optional[str] = None


class CaptchaTestResponse(BaseModel):
    """2Captcha connectivity test response."""

    success: bool
    message: str
    api_key_present: bool


class ScrapeStatusResponse(BaseModel):
    """Data completeness metrics for the scrape pipeline."""

    tender_count: int = Field(..., description="Total tenders in database")
    detail_scraped_count: int = Field(..., description="Tenders with detail fields populated")
    pending_detail_count: int = Field(..., description="Tenders awaiting detail extraction")
    embedding_pending_count: int = Field(..., description="Tenders without embeddings yet")
    cloudflare_enabled: bool = Field(..., description="Whether USE_CLOUDFLARE=true")
    last_updated_at: Optional[str] = Field(None, description="ISO timestamp of most-recently updated tender")


# ==================== Endpoints ====================


@app.get("/", tags=["Root"])
async def root() -> dict:
    """Root endpoint with API information."""
    return {
        "service": "TenderPost Scraper API",
        "version": "1.0.0",
        "status": "operational",
        "endpoints": {
            "health": "/health",
            "tenders": "/api/tenders",
            "tenders_free": "/api/tenders/free",
            "latest_tenders": "/api/tenders/latest",
            "scrape_status": "/api/tenders/status",
            "test_captcha": "/api/test-2captcha",
            "docs": "/docs",
        },
    }


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check() -> HealthResponse:
    """
    Health check endpoint.

    Returns service status and configuration.
    """
    api_key = os.getenv("TWOCAPTCHA_API_KEY")

    return HealthResponse(
        status="healthy",
        service="TenderPost Scraper",
        version="1.0.0",
        captcha_api="configured" if api_key else "not_configured",
    )


@app.get("/api/test-2captcha", response_model=CaptchaTestResponse, tags=["Testing"])
async def test_captcha_service() -> CaptchaTestResponse:
    """
    Test 2Captcha API connectivity.

    Checks if 2Captcha service is reachable and API key is configured.
    """
    api_key = os.getenv("TWOCAPTCHA_API_KEY")
    api_key_present = bool(api_key)

    if not api_key_present:
        return CaptchaTestResponse(
            success=False,
            message="2Captcha API key not configured. Set TWOCAPTCHA_API_KEY environment variable.",
            api_key_present=False,
        )

    # Test connectivity
    connectivity = await test_2captcha_connectivity()

    if connectivity:
        return CaptchaTestResponse(
            success=True,
            message="2Captcha service is reachable and API key is configured.",
            api_key_present=True,
        )
    else:
        return CaptchaTestResponse(
            success=False,
            message="Cannot reach 2Captcha service. Check network or service status.",
            api_key_present=True,
        )


@app.get("/api/tenders", response_model=TendersResponse, tags=["Tenders"])
async def get_tenders(
    force_refresh: bool = Query(
        False, description="Force fresh scraping instead of using cached data"
    ),
    debug: bool = Query(False, description="Enable debug logging"),
) -> TendersResponse:
    """
    Get tender data from eProcure Advanced Search.

    This endpoint:
    1. Navigates to eProcure Advanced Search page
    2. Solves CAPTCHA using 2Captcha API
    3. Submits search form for "Open Tender" type
    4. Extracts all tenders with pagination
    5. Returns structured JSON data

    **Note:** First request will be slow (30-60s) due to CAPTCHA solving and pagination.

    **Parameters:**
    - `force_refresh`: Set to `true` to force fresh scraping (default: false)
    - `debug`: Enable debug logging (default: false)

    **Response:**
    - Returns list of tenders with metadata (title, dates, organization, etc.)
    - Includes total pages scraped and live tenders count
    """
    if debug:
        os.environ["DEBUG"] = "true"

    # TODO: Implement caching logic here (Redis/Supabase)
    # For now, always scrape fresh data

    result = await scrape_tenders_crawl4ai_playwright()

    # Save to Supabase if scraping was successful
    if result.get("success") and len(result.get("tenders", [])) > 0:
        supabase_result = save_to_supabase(
            tenders=result.get("tenders", []),
            source="eprocure.gov.in/AdvancedSearch",
            live_tenders=result.get("live_tenders"),
        )
        
        if supabase_result.get("success"):
            print(f"✅ Saved snapshot to Supabase: {supabase_result.get('count')} tenders")
        else:
            print(f"⚠️  Failed to save to Supabase: {supabase_result.get('error')}")

    if result.get("success"):
        return TendersResponse(
            success=True,
            source="eprocure.gov.in/AdvancedSearch",
            count=len(result.get("tenders", [])),
            total_pages=result.get("total_pages", 0),
            live_tenders=result.get("live_tenders"),
            items=result.get("tenders", []),
        )
    else:
        return TendersResponse(
            success=False,
            source="eprocure.gov.in/AdvancedSearch",
            count=len(result.get("tenders", [])),
            total_pages=result.get("total_pages", 0),
            live_tenders=result.get("live_tenders"),
            items=result.get("tenders", []),
            error=result.get("error", "Unknown error occurred"),
        )


@app.get("/api/tenders/latest", response_model=TendersResponse, tags=["Tenders"])
async def get_latest_tenders(
    debug: bool = Query(False, description="Enable debug logging"),
) -> TendersResponse:
    """
    Get latest active tenders (no CAPTCHA required).

    This is a faster endpoint that scrapes the "Latest Active Tenders" page
    which doesn't require CAPTCHA solving.

    **Response:**
    - Returns list of latest active tenders
    - Faster than /api/tenders (no CAPTCHA delay)
    """
    if debug:
        os.environ["DEBUG"] = "true"

    result = await scrape_latest_active_tenders()

    # Save to Supabase if scraping was successful
    if result.get("success") and len(result.get("tenders", [])) > 0:
        supabase_result = save_to_supabase(
            tenders=result.get("tenders", []),
            source="eprocure.gov.in/LatestActiveTenders",
            live_tenders=None,
        )
        
        if supabase_result.get("success"):
            print(f"✅ Saved snapshot to Supabase: {supabase_result.get('count')} tenders")
        else:
            print(f"⚠️  Failed to save to Supabase: {supabase_result.get('error')}")

    if result.get("success"):
        return TendersResponse(
            success=True,
            source="eprocure.gov.in/LatestActiveTenders",
            count=len(result.get("tenders", [])),
            total_pages=result.get("total_pages", 0),
            live_tenders=None,
            items=result.get("tenders", []),
        )
    else:
        return TendersResponse(
            success=False,
            source="eprocure.gov.in/LatestActiveTenders",
            count=len(result.get("tenders", [])),
            total_pages=result.get("total_pages", 0),
            live_tenders=None,
            items=result.get("tenders", []),
            error=result.get("error", "Unknown error occurred"),
        )


@app.get("/api/tenders/free", response_model=TendersResponse, tags=["Tenders"])
async def get_tenders_free(
    debug: bool = Query(False, description="Enable debug logging"),
) -> TendersResponse:
    """
    Free-tier crawl: scrapes up to 100 listing pages from eProcure Advanced Search.

    Identical to /api/tenders but hard-capped at 100 pages regardless of the
    MAX_PAGES environment variable. No detail extraction or embeddings are run.
    """
    if debug:
        os.environ["DEBUG"] = "true"

    result = await scrape_listing_pages(max_pages=100)

    if result.get("success") and len(result.get("tenders", [])) > 0:
        supabase_result = save_to_supabase(
            tenders=result.get("tenders", []),
            source="eprocure.gov.in/AdvancedSearch",
            live_tenders=result.get("live_tenders"),
        )
        if supabase_result.get("success"):
            print(f"✅ Saved snapshot to Supabase: {supabase_result.get('count')} tenders")
        else:
            print(f"⚠️  Failed to save to Supabase: {supabase_result.get('error')}")

    if result.get("success"):
        return TendersResponse(
            success=True,
            source="eprocure.gov.in/AdvancedSearch",
            count=len(result.get("tenders", [])),
            total_pages=result.get("total_pages", 0),
            live_tenders=result.get("live_tenders"),
            items=result.get("tenders", []),
        )
    else:
        return TendersResponse(
            success=False,
            source="eprocure.gov.in/AdvancedSearch",
            count=len(result.get("tenders", [])),
            total_pages=result.get("total_pages", 0),
            live_tenders=result.get("live_tenders"),
            items=result.get("tenders", []),
            error=result.get("error", "Unknown error occurred"),
        )


@app.get("/api/tenders/status", response_model=ScrapeStatusResponse, tags=["Tenders"])
async def get_scrape_status() -> ScrapeStatusResponse:
    """
    Read-only: query Supabase for scrape pipeline health and data completeness.

    Returns counts of total, detail-scraped, and embedding-pending tenders,
    plus the timestamp of the most-recently updated tender.
    Does NOT trigger any scraping.
    """
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    cf_enabled   = os.getenv("USE_CLOUDFLARE", "true").lower() == "true"

    if not supabase_url or not supabase_key:
        return ScrapeStatusResponse(
            tender_count=0,
            detail_scraped_count=0,
            pending_detail_count=0,
            embedding_pending_count=0,
            cloudflare_enabled=cf_enabled,
            last_updated_at=None,
        )

    from supabase import create_client
    client = create_client(supabase_url, supabase_key)

    try:
        total_res   = client.table("tenders").select("id", count="exact").execute()
        detail_res  = client.table("tenders").select("id", count="exact").eq("detail_scraped", True).execute()
        pending_res = client.table("tenders").select("id", count="exact").eq("detail_scraped", False).execute()
        embed_res   = client.table("tenders").select("id", count="exact").is_("embedding", "null").execute()
        recent_res  = (
            client.table("tenders")
            .select("updated_at")
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )

        last_updated = None
        if recent_res.data:
            last_updated = recent_res.data[0].get("updated_at")

        return ScrapeStatusResponse(
            tender_count=total_res.count or 0,
            detail_scraped_count=detail_res.count or 0,
            pending_detail_count=pending_res.count or 0,
            embedding_pending_count=embed_res.count or 0,
            cloudflare_enabled=cf_enabled,
            last_updated_at=last_updated,
        )
    except Exception as e:
        return ScrapeStatusResponse(
            tender_count=0,
            detail_scraped_count=0,
            pending_detail_count=0,
            embedding_pending_count=0,
            cloudflare_enabled=cf_enabled,
            last_updated_at=None,
        )


# ==================== Error Handlers ====================


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler for unhandled errors."""
    return {
        "success": False,
        "error": str(exc),
        "message": "An unexpected error occurred. Please check logs for details.",
    }


# ==================== Startup Event ====================


@app.on_event("startup")
async def startup_event():
    """Startup event handler."""
    print("🚀 TenderPost Scraper API Starting...")
    print("=" * 60)
    print(f"📌 Service: TenderPost Scraper v1.0.0")
    print(f"📌 Framework: Crawl4AI + Playwright + 2Captcha")
    print(f"📌 Environment: {os.getenv('ENVIRONMENT', 'development')}")

    # Check 2Captcha API key
    api_key = os.getenv("TWOCAPTCHA_API_KEY")
    if api_key:
        print(f"✅ 2Captcha API Key: Configured ({api_key[:8]}...)")
    else:
        print("⚠️  2Captcha API Key: NOT CONFIGURED")
        print("   Set TWOCAPTCHA_API_KEY environment variable")

    # Check Supabase configuration
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if supabase_url and supabase_key:
        print(f"✅ Supabase: Configured")
        print(f"   URL: {supabase_url}")
    else:
        print("⚠️  Supabase: NOT CONFIGURED")
        print("   Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY environment variables")

    # Check Cloudflare Browser Rendering configuration
    cf_token   = os.getenv("CLOUDFLARE_API_TOKEN")
    cf_account = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    cf_enabled = os.getenv("USE_CLOUDFLARE", "true").lower() == "true"
    if cf_token and cf_account and cf_enabled:
        print(f"✅ Cloudflare Browser Rendering: Configured (USE_CLOUDFLARE=true)")
    elif cf_enabled:
        print("⚠️  Cloudflare Browser Rendering: NOT CONFIGURED")
        print("   Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID")
        print("   Detail scraping will fall back to direct httpx + Selectolax")
    else:
        print("📌 Cloudflare Browser Rendering: Disabled (USE_CLOUDFLARE=false)")

    print("=" * 60)
    print("📡 API Documentation: http://localhost:8000/docs")
    print("🔍 Health Check: http://localhost:8000/health")
    print("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown event handler."""
    print("\n🛑 TenderPost Scraper API Shutting Down...")


# ==================== Main ====================

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=True,  # Enable auto-reload for development
        log_level="info",
    )

