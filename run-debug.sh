#!/bin/bash

# TenderPost Scraper - Debug Mode Runner
# This script builds and runs the Docker container with debugging enabled

set -e

# Enable Docker BuildKit for better caching
export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

echo "🐛 TenderPost Backend - Debug Mode"
echo "===================================="
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found!"
    echo "📝 Creating .env from template..."
    
    if [ -f .env.template ]; then
        cp .env.template .env
        echo "✅ .env file created"
        echo ""
        echo "⚠️  IMPORTANT: You must add your 2Captcha API key to .env"
        echo "   Edit the file: nano .env"
        echo "   Add: TWOCAPTCHA_API_KEY=your_api_key_here"
        echo ""
        read -p "Press Enter after updating .env, or Ctrl+C to exit..."
    else
        echo "❌ .env.template not found!"
        exit 1
    fi
fi

# Check if 2Captcha API key is set
source .env
if [ -z "$TWOCAPTCHA_API_KEY" ] || [ "$TWOCAPTCHA_API_KEY" = "your_2captcha_api_key_here" ]; then
    echo "❌ ERROR: TWOCAPTCHA_API_KEY not set in .env file!"
    echo ""
    echo "Please edit .env and add your 2Captcha API key:"
    echo "   TWOCAPTCHA_API_KEY=your_actual_api_key"
    echo ""
    echo "Get your API key from: https://2captcha.com/"
    exit 1
fi

echo "✅ 2Captcha API key detected: ${TWOCAPTCHA_API_KEY:0:8}..."
echo ""

# Stop any existing containers
echo "🛑 Stopping existing containers..."
docker-compose -f docker-compose.dev.yml down 2>/dev/null || true
echo ""

# Build the image
echo "🔨 Building Docker image..."
echo "   ℹ️  BuildKit enabled for faster builds with better caching"
echo "   ℹ️  Chromium browser will be cached (no re-download on code changes)"
echo ""
docker-compose -f docker-compose.dev.yml build --progress=plain
echo ""

# Run the container
echo "🚀 Starting container in DEBUG mode..."
echo ""
echo "📊 Debug Configuration:"
echo "   • Debug Mode: ENABLED"
echo "   • Max Pages: 5 (for testing)"
echo "   • Log Level: DEBUG"
echo "   • Auto-reload: ENABLED"
echo ""
echo "🌐 API will be available at: http://localhost:8000"
echo "📚 API Documentation: http://localhost:8000/docs"
echo ""
echo "📝 Useful commands while running:"
echo "   • View logs: docker logs -f tenderpost-scraper-dev"
echo "   • Stop: docker-compose -f docker-compose.dev.yml down"
echo "   • Restart: docker-compose -f docker-compose.dev.yml restart"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Run with logs attached
docker-compose -f docker-compose.dev.yml up

