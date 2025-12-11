# Multi-stage Dockerfile for TenderPost Scraper
# Optimized for production with Crawl4AI + Playwright
# Browser caching optimized to avoid re-downloading Chromium on every build

# ==================== Stage 1: Base ====================
FROM python:3.11-slim as base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# ==================== Stage 2: Dependencies ====================
FROM base as dependencies

WORKDIR /app

# Install Poetry
RUN pip install poetry==1.7.1

# Copy dependency files ONLY (for better caching)
COPY pyproject.toml ./

# Install Python dependencies
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --no-root --only main

# Install Playwright system dependencies (as root)
RUN playwright install-deps chromium

# ==================== Stage 3: Browser Cache ====================
# This stage caches the browser installation separately from app code
FROM dependencies as browser-cache

# Create non-root user early
RUN useradd -m -u 1000 scraper

# Create browser directory with correct permissions
RUN mkdir -p /ms-playwright && \
    chown -R scraper:scraper /ms-playwright

# Switch to scraper user and install Playwright browsers
# This layer will be cached unless dependencies change
USER scraper
RUN playwright install chromium

# ==================== Stage 4: Production ====================
FROM browser-cache as production

WORKDIR /app

# Copy application code (this won't invalidate browser cache)
COPY --chown=scraper:scraper . .

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=5.0)" || exit 1

# Start application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

