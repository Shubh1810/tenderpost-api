# 🐛 Docker Debug Mode - Quick Start Guide

This guide will help you run the TenderPost backend in Docker with full debugging enabled.

---

## 📋 Prerequisites

1. **Docker & Docker Compose installed**
   ```bash
   docker --version
   docker-compose --version
   ```

2. **2Captcha API Key**
   - Sign up at: https://2captcha.com/
   - Get your API key from dashboard

---

## 🚀 Quick Start (3 Steps)

### Step 1: Configure Environment

```bash
# Navigate to project directory
cd /Users/shubh/Desktop/1-Projects/tender-backend

# Copy environment template
cp .env.template .env

# Edit .env and add your 2Captcha API key
nano .env
```

**Important:** Update this line in `.env`:
```env
TWOCAPTCHA_API_KEY=your_actual_2captcha_api_key_here
```

### Step 2: Run Docker in Debug Mode

**Option A: Automated Script (Recommended)**
```bash
./run-debug.sh
```

**Option B: Manual Docker Compose**
```bash
# Build and run
docker-compose -f docker-compose.dev.yml up --build

# Or run in background
docker-compose -f docker-compose.dev.yml up --build -d

# View logs
docker logs -f tenderpost-scraper-dev
```

### Step 3: Test the API

**In a new terminal:**
```bash
# Automated testing
./test-api.sh

# Or manual tests
curl http://localhost:8000/health
curl http://localhost:8000/api/test-2captcha
```

---

## 🔍 Debug Configuration

The debug mode (`docker-compose.dev.yml`) includes:

| Feature | Setting | Purpose |
|---------|---------|---------|
| **Debug Mode** | `DEBUG=true` | Verbose logging |
| **Log Level** | `LOG_LEVEL=DEBUG` | Maximum detail |
| **Max Pages** | `MAX_PAGES=5` | Limit scraping for testing |
| **Auto-reload** | `--reload` | Hot reload on code changes |
| **Timeout** | `PAGE_TIMEOUT=30000` | 30 seconds per page |
| **Colored Output** | `tty: true` | Pretty console logs |

---

## 📡 API Endpoints for Testing

### 1. Health Check (Instant)
```bash
curl http://localhost:8000/health
```

**Expected Response:**
```json
{
  "status": "healthy",
  "service": "TenderPost Scraper",
  "version": "1.0.0",
  "captcha_api": "configured"
}
```

### 2. Test 2Captcha Connectivity (5 seconds)
```bash
curl http://localhost:8000/api/test-2captcha
```

**Expected Response:**
```json
{
  "success": true,
  "message": "2Captcha service is reachable and API key is configured.",
  "api_key_present": true
}
```

### 3. Get Latest Tenders - Fast (15-30 seconds, NO CAPTCHA)
```bash
curl "http://localhost:8000/api/tenders/latest?debug=true"
```

**What to expect:**
- ⏱️ Takes 15-30 seconds (scrapes 5 pages in debug mode)
- ✅ No CAPTCHA solving required
- 📊 Returns 100-200 tenders

### 4. Get Tenders - Full Pipeline (30-60 seconds, WITH CAPTCHA)
```bash
curl "http://localhost:8000/api/tenders?debug=true"
```

**What to expect:**
- ⏱️ Takes 30-60 seconds
- 🔐 Solves CAPTCHA automatically
- 📊 Returns validated tender data
- 🔄 Scrapes 5 pages (in debug mode)

---

## 📊 Monitor Real-Time Logs

### View Live Logs
```bash
# Follow logs in real-time
docker logs -f tenderpost-scraper-dev

# Filter for specific steps
docker logs -f tenderpost-scraper-dev | grep "✅"  # Success steps
docker logs -f tenderpost-scraper-dev | grep "❌"  # Errors
docker logs -f tenderpost-scraper-dev | grep "🔵"  # Initiated steps
```

### Example Debug Output
```
🔵 [CAPTCHA Handler] INITIATED
   • action: Checking for CAPTCHA

✅ [CAPTCHA Handler] SUCCESS
   • status: CAPTCHA detected, capturing...

🔵 [2Captcha Solver] INITIATED
   • action: Creating task

⏳ [2Captcha Solver] PROCESSING
   • task_id: 12345678

✅ [2Captcha Solver] SUCCESS
   • solution: ABC123
   • length: 6
   • elapsed: 8.3s

✅ [CAPTCHA Handler] SUCCESS
   • action: CAPTCHA solution filled

✅ [Page Extraction] SUCCESS
   • page: 1
   • tenders: 20
   • total: 20
```

---

## 🛠️ Docker Management Commands

### Start/Stop
```bash
# Start (if already built)
docker-compose -f docker-compose.dev.yml up

# Start in background
docker-compose -f docker-compose.dev.yml up -d

# Stop
docker-compose -f docker-compose.dev.yml down

# Stop and remove volumes
docker-compose -f docker-compose.dev.yml down -v
```

### Rebuild
```bash
# Rebuild after code changes
docker-compose -f docker-compose.dev.yml up --build

# Force rebuild (no cache)
docker-compose -f docker-compose.dev.yml build --no-cache
docker-compose -f docker-compose.dev.yml up
```

### Inspect
```bash
# View container status
docker ps

# View resource usage
docker stats tenderpost-scraper-dev

# Execute commands inside container
docker exec -it tenderpost-scraper-dev bash

# View container details
docker inspect tenderpost-scraper-dev
```

### Cleanup
```bash
# Remove stopped containers
docker-compose -f docker-compose.dev.yml down

# Remove images
docker rmi tenderpost-scraper-dev

# Clean everything (use with caution)
docker system prune -a
```

---

## 🐛 Debugging Tips

### 1. Check if Container is Running
```bash
docker ps | grep tenderpost

# Should show:
# CONTAINER ID   IMAGE    ...   STATUS         PORTS                    NAMES
# abc123         ...      ...   Up 2 minutes   0.0.0.0:8000->8000/tcp   tenderpost-scraper-dev
```

### 2. Test from Inside Container
```bash
# Enter container shell
docker exec -it tenderpost-scraper-dev bash

# Inside container, test Python
python -c "import crawl4ai; print('Crawl4AI:', crawl4ai.__version__)"
python -c "import playwright; print('Playwright OK')"

# Check environment variables
env | grep TWOCAPTCHA

# Exit container
exit
```

### 3. Check Network Connectivity
```bash
# Test if API is accessible
curl -v http://localhost:8000/health

# If fails, check port binding
docker port tenderpost-scraper-dev
```

### 4. View All Logs (from start)
```bash
docker logs tenderpost-scraper-dev --tail 200
```

### 5. Enable Even More Verbose Logging
Edit `docker-compose.dev.yml` and add:
```yaml
environment:
  - DEBUG=true
  - LOG_LEVEL=DEBUG
  - PYTHONVERBOSE=1  # Add this
```

---

## 🧪 Test Scenarios

### Scenario 1: Quick Health Check (10 seconds)
```bash
# 1. Start container
./run-debug.sh

# 2. In new terminal, test endpoints
curl http://localhost:8000/health
curl http://localhost:8000/api/test-2captcha

# Expected: Both return 200 OK
```

### Scenario 2: Test Latest Tenders (30 seconds)
```bash
# This tests extraction WITHOUT CAPTCHA
curl "http://localhost:8000/api/tenders/latest?debug=true"

# Watch logs in another terminal
docker logs -f tenderpost-scraper-dev
```

**What to look for in logs:**
```
🔵 [Latest Tenders Scraper] INITIATED
✅ [Navigation] SUCCESS
✅ [Page Extraction] SUCCESS
   • page: 1
   • tenders: 20
✅ [Latest Tenders Scraper] SUCCESS
   • total_tenders: 100
   • total_pages: 5
```

### Scenario 3: Full CAPTCHA Test (60 seconds)
```bash
# This tests FULL pipeline with CAPTCHA
curl "http://localhost:8000/api/tenders?debug=true"
```

**What to look for in logs:**
```
🔵 [CAPTCHA Handler] INITIATED
✅ [CAPTCHA Handler] SUCCESS (CAPTCHA detected)
🔵 [2Captcha Solver] INITIATED
⏳ [2Captcha Solver] PROCESSING
✅ [2Captcha Solver] SUCCESS
✅ [Form Filling] SUCCESS
✅ [Page Extraction] SUCCESS
```

---

## ❌ Troubleshooting

### Issue: "Cannot reach 2captcha.com"
**Solution:**
```bash
# Check if API key is set
docker exec tenderpost-scraper-dev env | grep TWOCAPTCHA

# Test connectivity from container
docker exec tenderpost-scraper-dev curl -I https://2captcha.com

# If fails, check your network/firewall
```

### Issue: "Port 8000 already in use"
**Solution:**
```bash
# Find process using port 8000
lsof -i :8000

# Kill process (replace PID)
kill -9 <PID>

# Or use different port
# Edit docker-compose.dev.yml: "8001:8000"
```

### Issue: "Playwright browser not found"
**Solution:**
```bash
# Rebuild container
docker-compose -f docker-compose.dev.yml down
docker-compose -f docker-compose.dev.yml build --no-cache
docker-compose -f docker-compose.dev.yml up
```

### Issue: "No tenders extracted"
**Solution:**
```bash
# Enable maximum debugging
# Edit docker-compose.dev.yml and set MAX_PAGES=1

# Check if website structure changed
# View the HTML in logs or screenshot
```

### Issue: "Container exits immediately"
**Solution:**
```bash
# View container logs
docker logs tenderpost-scraper-dev

# Check for Python errors in startup
docker-compose -f docker-compose.dev.yml up

# Common causes:
# - Missing .env file
# - Invalid 2Captcha API key
# - Port conflict
```

---

## 📚 Interactive API Documentation

Once the container is running, visit:

**Swagger UI:** http://localhost:8000/docs

Features:
- 🔍 Try each endpoint interactively
- 📖 See request/response schemas
- 🧪 Test with different parameters
- 📥 Download response as JSON

---

## 🎯 Success Checklist

✅ **Setup Complete:**
- [ ] Docker installed and running
- [ ] `.env` file created with valid 2Captcha API key
- [ ] Container builds successfully
- [ ] Container starts without errors

✅ **API Working:**
- [ ] Health check returns 200
- [ ] 2Captcha test returns success
- [ ] Latest tenders endpoint returns data
- [ ] Full tenders endpoint solves CAPTCHA

✅ **Debugging Ready:**
- [ ] Can view real-time logs
- [ ] Debug output is verbose and clear
- [ ] Can see each scraping step
- [ ] Errors are logged with context

---

## 🚀 Next Steps After Testing

1. **Increase page limit** for production scraping:
   ```yaml
   # In docker-compose.dev.yml
   - MAX_PAGES=200  # Change from 5 to 200
   ```

2. **Switch to production mode:**
   ```bash
   docker-compose up --build -d
   ```

3. **Set up monitoring** (optional):
   - Add logging to file
   - Set up alerts for failures
   - Monitor 2Captcha balance

4. **Optimize performance:**
   - Adjust `PAGE_TIMEOUT` based on network speed
   - Configure resource limits
   - Enable caching (future enhancement)

---

**Happy Debugging! 🐛✨**

For issues: Check logs first → Review this guide → Check status.md

