#!/bin/bash

# TenderPost Scraper - Quick Start Script
# This script helps you get started with the TenderPost backend

set -e

echo "🚀 TenderPost Backend - Quick Start"
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
        echo "⚠️  IMPORTANT: Edit .env and add your 2Captcha API key!"
        echo "   Run: nano .env"
        echo ""
        read -p "Press Enter to continue after updating .env..."
    else
        echo "❌ .env.template not found!"
        exit 1
    fi
fi

# Check Python version
echo "🐍 Checking Python version..."
python_version=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
required_version="3.9"

if [ "$(printf '%s\n' "$required_version" "$python_version" | sort -V | head -n1)" = "$required_version" ]; then
    echo "✅ Python $python_version detected (>= 3.9)"
else
    echo "❌ Python 3.9+ is required (found: $python_version)"
    exit 1
fi

# Check if Poetry is installed
echo ""
echo "📦 Checking Poetry installation..."
if command -v poetry &> /dev/null; then
    echo "✅ Poetry is installed"
    
    echo ""
    echo "📦 Installing dependencies with Poetry..."
    poetry install
    
    echo ""
    echo "🎭 Installing Playwright browsers..."
    poetry run playwright install chromium
    poetry run playwright install-deps chromium
    
    echo ""
    echo "✅ Installation complete!"
    echo ""
    echo "🚀 Starting server with Poetry..."
    poetry run python main.py
    
else
    echo "⚠️  Poetry not found. Using pip..."
    
    # Check if venv exists
    if [ ! -d "venv" ]; then
        echo "📦 Creating virtual environment..."
        python3 -m venv venv
    fi
    
    echo "📦 Activating virtual environment..."
    source venv/bin/activate
    
    echo "📦 Installing dependencies with pip..."
    pip install -r requirements.txt
    
    echo ""
    echo "🎭 Installing Playwright browsers..."
    playwright install chromium
    playwright install-deps chromium
    
    echo ""
    echo "✅ Installation complete!"
    echo ""
    echo "🚀 Starting server..."
    python main.py
fi

