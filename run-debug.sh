#!/bin/bash
# TenderPost — Debug Runner
# Builds Docker image and starts two services:
#   • cppp-scraper          — scrapes all active tenders, pushes to Supabase, then exits
#   • tenderpost-scraper-dev — FastAPI server on localhost:8000 (with hot-reload)
#
# Usage:
#   ./run-debug.sh                    # run both services (scraper + API)
#   ./run-debug.sh scraper-only       # run only the CPPP scraper (run_cron.py)
#   ./run-debug.sh api-only           # run only the FastAPI server

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

echo ""
echo "=================================="
echo "  TenderPost — Debug Mode"
echo "=================================="
echo ""

# ── Load .env ─────────────────────────────────────────────────────────────────
if [ -f "$DIR/.env" ]; then
    set -a; source "$DIR/.env"; set +a
fi

# Normalise: SUPABASE_SERVICE_ROLE_KEY and SUPABASE_KEY are the same credential
if [ -n "$SUPABASE_KEY" ] && [ -z "$SUPABASE_SERVICE_ROLE_KEY" ]; then
    export SUPABASE_SERVICE_ROLE_KEY="$SUPABASE_KEY"
fi
if [ -n "$SUPABASE_SERVICE_ROLE_KEY" ] && [ -z "$SUPABASE_KEY" ]; then
    export SUPABASE_KEY="$SUPABASE_SERVICE_ROLE_KEY"
fi

# ── Check required credentials ────────────────────────────────────────────────
if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_SERVICE_ROLE_KEY" ]; then
    echo "ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set."
    echo "       Add them to .env and re-run."
    exit 1
fi
echo "  Supabase URL : $SUPABASE_URL"
echo "  Supabase Key : ${SUPABASE_SERVICE_ROLE_KEY:0:20}..."
echo ""

# ── Stop any existing containers ──────────────────────────────────────────────
echo "Stopping existing containers..."
docker-compose -f docker-compose.dev.yml down 2>/dev/null || true
echo ""

# ── Build ─────────────────────────────────────────────────────────────────────
echo "Building Docker image..."
docker-compose -f docker-compose.dev.yml build --progress=plain
echo ""

# ── Start ─────────────────────────────────────────────────────────────────────
MODE="${1:-both}"

case "$MODE" in
  scraper-only)
    echo "Starting CPPP scraper (run_cron.py)..."
    echo "  → Central: eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"
    echo "  → State:   eprocure.gov.in/cppp/latestactivetendersnew/mmpdata"
    echo "  → Pushing to Supabase tenders table"
    echo ""
    docker-compose -f docker-compose.dev.yml run --rm cppp-scraper
    ;;
  api-only)
    echo "Starting FastAPI server only..."
    echo "  → http://localhost:8000"
    echo "  → Docs: http://localhost:8000/docs"
    echo ""
    docker-compose -f docker-compose.dev.yml up tenderpost-scraper-dev
    ;;
  *)
    echo "Starting all services (watch mode):"
    echo "  cppp-scraper          — scrapes tenders → Supabase (run_cron.py)"
    echo "  tenderpost-scraper-dev — FastAPI on http://localhost:8000"
    echo ""
    echo "  Code changes sync instantly into containers (no rebuild)."
    echo "  pyproject.toml changes trigger an automatic rebuild."
    echo ""
    echo "Logs:"
    echo "  docker logs -f tenderpost-cppp-scraper"
    echo "  docker logs -f tenderpost-scraper-dev"
    echo ""
    docker compose -f docker-compose.dev.yml up --watch
    ;;
esac
