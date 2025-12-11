#!/bin/bash

# Docker Build Optimization Verification Script
# This script helps you verify that Docker build caching is working correctly

set -e

echo "🔍 Docker Build Optimization Verification"
echo "=========================================="
echo ""

# Check Docker version
echo "1️⃣  Checking Docker version..."
DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "not found")
if [ "$DOCKER_VERSION" = "not found" ]; then
    echo "❌ Docker not found. Please install Docker first."
    exit 1
fi
echo "✅ Docker version: $DOCKER_VERSION"

# Check if version is >= 19.03 (required for BuildKit)
REQUIRED_VERSION="19.03"
if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$DOCKER_VERSION" | sort -V | head -n1)" = "$REQUIRED_VERSION" ]; then
    echo "✅ Docker version supports BuildKit (>= 19.03)"
else
    echo "⚠️  Warning: Docker $DOCKER_VERSION may not fully support BuildKit"
    echo "   Recommended: Update to Docker 19.03+"
fi
echo ""

# Check BuildKit
echo "2️⃣  Checking BuildKit..."
if [ "$DOCKER_BUILDKIT" = "1" ]; then
    echo "✅ BuildKit enabled in environment"
else
    echo "⚠️  BuildKit not enabled in environment"
    echo "   Set: export DOCKER_BUILDKIT=1"
fi
echo ""

# Check Docker Compose version
echo "3️⃣  Checking Docker Compose..."
COMPOSE_VERSION=$(docker-compose version --short 2>/dev/null || echo "not found")
if [ "$COMPOSE_VERSION" = "not found" ]; then
    echo "⚠️  Docker Compose not found (optional)"
else
    echo "✅ Docker Compose version: $COMPOSE_VERSION"
fi
echo ""

# Check .dockerignore
echo "4️⃣  Checking .dockerignore..."
if [ -f .dockerignore ]; then
    echo "✅ .dockerignore exists"
    LINE_COUNT=$(wc -l < .dockerignore)
    echo "   Contains $LINE_COUNT ignore rules"
else
    echo "❌ .dockerignore not found"
    echo "   This will slow down builds!"
fi
echo ""

# Check Dockerfile
echo "5️⃣  Checking Dockerfile..."
if [ -f Dockerfile ]; then
    echo "✅ Dockerfile exists"
    
    # Check for browser cache stage
    if grep -q "browser-cache" Dockerfile; then
        echo "✅ Browser cache stage found"
    else
        echo "❌ Browser cache stage not found"
        echo "   Browser will be re-downloaded on every build!"
    fi
    
    # Check for PLAYWRIGHT_BROWSERS_PATH
    if grep -q "PLAYWRIGHT_BROWSERS_PATH" Dockerfile; then
        echo "✅ PLAYWRIGHT_BROWSERS_PATH configured"
    else
        echo "⚠️  PLAYWRIGHT_BROWSERS_PATH not set"
    fi
    
    # Count stages
    STAGE_COUNT=$(grep -c "^FROM .* as" Dockerfile || echo "0")
    echo "   Found $STAGE_COUNT build stages"
else
    echo "❌ Dockerfile not found"
    exit 1
fi
echo ""

# Test build cache
echo "6️⃣  Testing build cache (this will take ~30 seconds)..."
echo ""
echo "   Building image first time..."

# Enable BuildKit for this test
export DOCKER_BUILDKIT=1

# Time first build
START_TIME=$(date +%s)
docker build -t tenderpost-test:verify . > /dev/null 2>&1
END_TIME=$(date +%s)
FIRST_BUILD_TIME=$((END_TIME - START_TIME))

echo "   ✅ First build: ${FIRST_BUILD_TIME}s"
echo ""
echo "   Making a trivial code change..."

# Make a small change
echo "# Verification test $(date)" >> main.py

echo "   Rebuilding..."

# Time second build
START_TIME=$(date +%s)
docker build -t tenderpost-test:verify . > /dev/null 2>&1
END_TIME=$(date +%s)
SECOND_BUILD_TIME=$((END_TIME - START_TIME))

echo "   ✅ Second build: ${SECOND_BUILD_TIME}s"
echo ""

# Revert change
git checkout -- main.py 2>/dev/null || sed -i '' '$d' main.py

# Analyze results
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 Build Performance Analysis"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "First build:  ${FIRST_BUILD_TIME}s"
echo "Second build: ${SECOND_BUILD_TIME}s"
echo ""

if [ $SECOND_BUILD_TIME -lt 20 ]; then
    echo "✅ EXCELLENT: Browser cache is working perfectly!"
    echo "   Code changes don't trigger browser re-download."
elif [ $SECOND_BUILD_TIME -lt 60 ]; then
    echo "⚠️  GOOD: Cache is working, but could be better."
    echo "   Expected: < 20s for code changes"
elif [ $SECOND_BUILD_TIME -lt 120 ]; then
    echo "⚠️  SLOW: Cache may not be working optimally."
    echo "   Expected: < 20s for code changes"
    echo "   Actual: ${SECOND_BUILD_TIME}s"
else
    echo "❌ FAILED: Browser is being re-downloaded!"
    echo "   Expected: < 20s for code changes"
    echo "   Actual: ${SECOND_BUILD_TIME}s"
    echo ""
    echo "Troubleshooting:"
    echo "   1. Check Dockerfile has 'browser-cache' stage"
    echo "   2. Verify PLAYWRIGHT_BROWSERS_PATH is set"
    echo "   3. Enable BuildKit: export DOCKER_BUILDKIT=1"
    echo "   4. Clear cache: docker builder prune -a"
fi
echo ""

# Cleanup
echo "🧹 Cleaning up test image..."
docker rmi tenderpost-test:verify > /dev/null 2>&1 || true
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Verification complete!"
echo ""
echo "📚 For more details, see: DOCKER-OPTIMIZATION.md"
echo ""

