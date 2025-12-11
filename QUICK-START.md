# ⚡ TenderPost Backend - 3-Minute Quick Start

## 🎯 Goal
Get the scraper running in Docker with debugging in **3 minutes**.

---

## 📍 Step-by-Step (Copy & Paste)

### 1️⃣ Setup Environment (30 seconds)

```bash
# Navigate to project
cd /Users/shubh/Desktop/1-Projects/tender-backend

# Create .env file
cp .env.template .env

# Edit .env and add your 2Captcha API key
nano .env
# Change: TWOCAPTCHA_API_KEY=your_2captcha_api_key_here
# Save: Ctrl+O, Enter, Ctrl+X
```

**Get 2Captcha API Key:** https://2captcha.com/enterpage

---

### 2️⃣ Run Docker in Debug Mode (1 minute)

```bash
# One-command startup
./run-debug.sh
```

**Wait for this message:**
```
🚀 TenderPost Scraper API Starting...
✅ 2Captcha API Key: Configured (abc12345...)
📡 API Documentation: http://localhost:8000/docs
```

---

### 3️⃣ Test the API (1 minute)

**Open new terminal and run:**

```bash
# Quick automated test
./test-api.sh

# OR manual tests:

# Test 1: Health check (instant)
curl http://localhost:8000/health

# Test 2: 2Captcha connectivity (5 sec)
curl http://localhost:8000/api/test-2captcha

# Test 3: Get tenders without CAPTCHA (15-30 sec)
curl "http://localhost:8000/api/tenders/latest?debug=true" | python3 -m json.tool

# Test 4: Full scrape with CAPTCHA (30-60 sec)
curl "http://localhost:8000/api/tenders?debug=true" | python3 -m json.tool
```

---

## 📊 Watch Live Progress

```bash
# In another terminal, watch logs
docker logs -f tenderpost-scraper-dev
```

You'll see:
```
🔵 [CAPTCHA Handler] INITIATED
✅ [CAPTCHA Handler] SUCCESS
🔵 [2Captcha Solver] INITIATED
⏳ [2Captcha Solver] PROCESSING
✅ [2Captcha Solver] SUCCESS - solution: ABC123 (8.3s)
✅ [Page Extraction] SUCCESS - page: 1, tenders: 20
✅ [Scraper Pipeline] SUCCESS - total_tenders: 100, pages: 5
```

---

## 🌐 Interactive API Docs

**Open in browser:** http://localhost:8000/docs

- Click "Try it out" on any endpoint
- See live responses
- No command line needed!

---

## 🛑 Stop the Server

```bash
# Press Ctrl+C in the terminal running docker-compose

# OR run this command:
docker-compose -f docker-compose.dev.yml down
```

---

## 🔥 Common Commands

```bash
# Start (already built)
docker-compose -f docker-compose.dev.yml up

# Start in background
docker-compose -f docker-compose.dev.yml up -d

# View logs
docker logs -f tenderpost-scraper-dev

# Stop
docker-compose -f docker-compose.dev.yml down

# Rebuild
docker-compose -f docker-compose.dev.yml up --build
```

---

## ✅ Success Checklist

After running `./test-api.sh`, you should see:

- ✅ Health check: `"status": "healthy"`
- ✅ 2Captcha test: `"success": true`
- ✅ Latest tenders: `"count": 100+` (5 pages × 20 tenders)
- ✅ Tenders extracted with all fields filled

---

## ❌ Quick Troubleshooting

| Problem | Solution |
|---------|----------|
| **"2Captcha API key not configured"** | Edit `.env` and add your API key |
| **"Port 8000 already in use"** | `lsof -i :8000` then `kill -9 <PID>` |
| **"Container exits immediately"** | Check logs: `docker logs tenderpost-scraper-dev` |
| **"No tenders extracted"** | Enable debug: `?debug=true` in URL |
| **"CAPTCHA solving failed"** | Check 2Captcha balance & API key |

---

## 📚 Full Documentation

- **Docker Debug Guide:** [DOCKER-DEBUG-GUIDE.md](./DOCKER-DEBUG-GUIDE.md)
- **Complete README:** [README.md](./README.md)
- **Project Status:** [status.md](./status.md)

---

## 🎉 You're Ready!

The scraper is now running in debug mode. Try the endpoints above and watch the logs to see the magic happen! 🚀

**Key Endpoints:**
- Health: http://localhost:8000/health
- Docs: http://localhost:8000/docs
- Tenders: http://localhost:8000/api/tenders/latest

---

**Next:** For production deployment, see [README.md](./README.md#production-deployment)

