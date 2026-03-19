# TenderPost Scraper — Architecture Reference

## Overview
FastAPI service that scrapes Indian government tender portals, stores structured data in Supabase, and generates OpenAI embeddings for semantic matching.

---

## Data Sources

### Source A: CPPP Portal (Primary — Cloudflare)
- **URL**: `https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata`
- **Volume**: ~30,954 tenders across ~3,096 listing pages (10 per page)
- **No CAPTCHA** — Cloudflare can crawl directly
- **Server-rendered HTML** — `render=false` works (free during beta)
- **Pagination**: GET `?page=N` (Cloudflare auto-discovers via link following)
- **Detail pages**: `/cppp/tendersfullview/[base64_encoded_id]`
- **Scraped by**: `crawl_cppp_listing()` in `cloudflare_crawl.py`
- **Controlled by**: `SCRAPE_CPPP=true` env var

### Source B: eprocure.gov.in Advanced Search (Supplemental — Playwright)
- **URL**: `https://eprocure.gov.in/eprocure/app?page=FrontEndAdvancedSearch&service=page`
- **Has CAPTCHA** — Playwright + 2Captcha required for listing pages
- **Detail pages**: stateless HTML, `sp=SESSION_TOKEN` in URL (tokens expire!)
- **Listing scraped by**: `scrape_listing_pages()` in `scraper.py` (Playwright)
- **Detail fields scraped by**: `crawl_detail_pages()` in `cloudflare_crawl.py`
- **Controlled by**: `USE_CLOUDFLARE=true` env var for detail phase

---

## Hybrid Pipeline (run_cron.py)

```
Source A: CPPP
  crawl_cppp_listing()
    └── 1 Cloudflare job, render=false, limit=35000
    └── AI extracts array of tenders per listing page
    └── Returns ~30K flat tender dicts (source="cppp")

Source B: eprocure Advanced Search
  scrape_listing_pages()  (Playwright + 2Captcha)
    └── CAPTCHA solve → form submit → paginate → collect URLs
    └── Returns List[TenderItem] with list-page fields only

  crawl_detail_pages(urls)  (Cloudflare, immediately after listing)
    └── One job per detail URL, render=false, limit=1
    └── AI extracts 15 detail fields per page
    └── Fallback: Cloudflare HTML + Selectolax → direct httpx + Selectolax
    └── MUST run immediately — sp= tokens expire after Playwright session ends

Merge + Upsert
  run_upsert() → Supabase tenders table (upsert on ref_no+source)
  run_embed_tenders() → OpenAI text-embedding-3-small
```

---

## File Map

| File | Purpose |
|---|---|
| `cloudflare_crawl.py` | Cloudflare /crawl client — CPPP listing + eprocure detail pages |
| `scraper.py` | Playwright listing scraper (CAPTCHA) + Selectolax detail parser (fallback) |
| `run_cron.py` | Pipeline orchestrator — both sources, upsert, embed |
| `main.py` | FastAPI endpoints |
| `supabase_client.py` | Legacy snapshot blob storage |
| `embeddings.py` | OpenAI embedding wrapper |
| `captcha/solver.py` | 2Captcha API v2 |
| `captcha/screenshot.py` | CAPTCHA image capture |

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | ✅ | CF API token with "Browser Rendering - Edit" permission |
| `CLOUDFLARE_ACCOUNT_ID` | ✅ | CF account ID |
| `TWOCAPTCHA_API_KEY` | ✅ | 2Captcha for eprocure CAPTCHA solving |
| `SUPABASE_URL` | ✅ | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | ✅ | Supabase service role key |
| `OPENAI_API_KEY` | ✅ | For text-embedding-3-small |
| `SCRAPE_CPPP` | ❌ | Enable CPPP source (default: true) |
| `USE_CLOUDFLARE` | ❌ | Enable Cloudflare detail phase for eprocure (default: true) |
| `MAX_PAGES` | ❌ | Max listing pages for Playwright scraper (default: 200) |
| `PAGE_TIMEOUT` | ❌ | Playwright page timeout ms (default: 30000) |

---

## Cloudflare /crawl API Notes

- **Endpoint**: `POST /accounts/{id}/browser-rendering/crawl`
- **Async**: POST returns `job_id`; poll with GET until `status=complete`
- **`render: false`**: Free during beta — raw HTML fetch, no browser time billed
- **`render: true`**: Billed at $0.09/browser hour — avoid unless JS rendering needed
- **Rate limit**: 6 POST requests/min on free plan; `RateLimiter` class enforces this
- **Free plan**: 5 crawl jobs/day, max 100 pages/job — production needs paid plan

### CPPP crawl specifics
- One job, `limit=35000`, `render=false`
- Cloudflare discovers `?page=N` links automatically
- Poll timeout: 2 hours (big job — ~3096 pages)
- First poll after 15s; adaptive backoff up to 120s

### eprocure detail page specifics
- One job per URL, `limit=1`, `render=false`
- Rate-limited: 6 submissions/min via `RateLimiter`
- Poll timeout: 5 min per job (small pages)
- **Session token risk**: `sp=TOKEN` in URLs expires — submit to Cloudflare immediately after Playwright finishes

---

## HTML Structure — CPPP Listing Pages
```
Table columns: Sl.No | e-Published Date | Bid Submission Closing Date |
               Tender Opening Date | Title/Ref.No./Tender Id |
               Organisation Name | Corrigendum
Detail links:  a[href="/cppp/tendersfullview/BASE64_STRING"]
Pagination:    GET ?page=N (Drupal standard)
JS required:   No — server-rendered HTML
```

## HTML Structure — eprocure Detail Pages
```
Data scope:    table.tablebg (NOT the left nav sidebar)
Label cells:   td.td_caption
Value cells:   td.td_field (immediate sibling within same tr)
Multiple pairs per row: [caption][field][caption][field][caption][field]
```

---

## Supabase Schema Notes
- `tenders` table: upserted on `(ref_no, source)` conflict
- `source`: `"eprocure"` or `"cppp"`
- `detail_scraped`: `true` when ≥1 detail field is non-null
- `embedding`: pgvector(1536) column for semantic search
- `raw_data`: JSONB for overflow fields (pre_bid_meeting_date, inviting_authority_*)

---

## Running Locally
```bash
uvicorn main:app --reload --port 8000
python run_cron.py
```

## API Endpoints
- `GET /api/tenders` — trigger eprocure listing scrape
- `GET /api/tenders/latest` — trigger eprocure latest active tenders
- `GET /api/tenders/status` — read-only DB health check (counts, last updated)
- `GET /health` — service health
- `GET /api/test-2captcha` — verify 2Captcha connectivity
