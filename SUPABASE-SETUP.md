# Supabase Integration Setup Guide

This document explains how to configure Supabase for storing tender snapshots.

## Prerequisites

1. A Supabase account (sign up at https://supabase.com/)
2. A Supabase project created

## Step 1: Create Database Schema

Run this SQL in your Supabase SQL Editor:

```sql
-- Table to store all tender snapshots (historical record)
CREATE TABLE IF NOT EXISTS snapshots (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    live_tenders INTEGER,
    count INTEGER NOT NULL,
    payload JSONB NOT NULL,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for faster queries by timestamp
CREATE INDEX IF NOT EXISTS idx_snapshots_scraped_at ON snapshots(scraped_at DESC);

-- Table to store only the LATEST snapshot (for fast API reads)
CREATE TABLE IF NOT EXISTS latest_snapshot (
    id INTEGER PRIMARY KEY DEFAULT 1,
    scraped_at TIMESTAMPTZ NOT NULL,
    live_tenders INTEGER,
    count INTEGER NOT NULL,
    payload JSONB NOT NULL,
    source TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);

-- Ensure only one row exists in latest_snapshot
CREATE UNIQUE INDEX IF NOT EXISTS idx_latest_snapshot_single_row ON latest_snapshot(id);

-- Comments for documentation
COMMENT ON TABLE snapshots IS 'Historical record of all tender scraping runs';
COMMENT ON TABLE latest_snapshot IS 'Always contains the most recent tender snapshot for fast API reads';
COMMENT ON COLUMN snapshots.payload IS 'JSON array of tender items with title, ref_no, dates, etc.';
COMMENT ON COLUMN latest_snapshot.payload IS 'JSON array of tender items with title, ref_no, dates, etc.';
```

## Step 2: Get Your Supabase Credentials

1. Go to your Supabase project dashboard
2. Click on **Settings** (gear icon) in the left sidebar
3. Navigate to **API** section
4. Copy the following:
   - **Project URL** (e.g., `https://your-project-id.supabase.co`)
   - **service_role key** (under "Project API keys" - this is a secret key!)

## Step 3: Configure Environment Variables

Create a `.env` file in the project root (or add to your existing `.env`):

```bash
# Supabase Configuration
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key_here

# 2Captcha API Key (if not already configured)
TWOCAPTCHA_API_KEY=your_2captcha_api_key_here

# Optional Application Settings
ENVIRONMENT=development
PORT=8000
HOST=0.0.0.0
MAX_PAGES=200
PAGE_TIMEOUT=30000
```

⚠️ **Security Note**: Never commit your `.env` file to version control!

## Step 4: Install Dependencies

```bash
pip install -r requirements.txt
```

This will install the new `supabase` package along with existing dependencies.

## Step 5: Test the Integration

Start your server:

```bash
python main.py
```

You should see:

```
✅ Supabase: Configured
   URL: https://your-project-id.supabase.co
```

## How It Works

### Automatic Data Saving

When you call either of these endpoints:

- `GET /api/tenders` (with CAPTCHA)
- `GET /api/tenders/latest` (without CAPTCHA)

The scraper will automatically:

1. ✅ Scrape tender data
2. ✅ Save to `snapshots` table (creates new historical record)
3. ✅ Update `latest_snapshot` table (overwrites with latest data)
4. ✅ Return the data to your API caller

### Data Structure

Each snapshot contains:

```json
{
  "id": "uuid-here",
  "scraped_at": "2024-01-01T12:00:00Z",
  "live_tenders": 1234,
  "count": 500,
  "source": "eprocure.gov.in/AdvancedSearch",
  "payload": [
    {
      "title": "Supply of Medical Equipment",
      "ref_no": "TENDER-123",
      "closing_date": "31-Dec-2024",
      "opening_date": "01-Jan-2025",
      "published_date": "01-Dec-2024",
      "organisation": "Ministry of Health",
      "url": "https://eprocure.gov.in/..."
    }
    // ... more tenders
  ]
}
```

### Frontend Integration

Your frontend can now query Supabase directly:

**Get Latest Snapshot:**
```javascript
const { data, error } = await supabase
  .from('latest_snapshot')
  .select('*')
  .single();
```

**Get Historical Snapshots:**
```javascript
const { data, error } = await supabase
  .from('snapshots')
  .select('*')
  .order('scraped_at', { ascending: false })
  .limit(10);
```

## Verification

### Check if Data is Being Saved

1. Trigger a scraping run by calling your API:
   ```bash
   curl http://localhost:8000/api/tenders/latest
   ```

2. Check your server logs for:
   ```
   ✅ Saved snapshot to Supabase: 500 tenders
   ```

3. Verify in Supabase dashboard:
   - Go to **Table Editor**
   - Check `snapshots` table (should have new rows after each scrape)
   - Check `latest_snapshot` table (should always have 1 row with latest data)

## Troubleshooting

### "Supabase: NOT CONFIGURED" warning

- Ensure your `.env` file exists and contains valid credentials
- Make sure you're using `SUPABASE_SERVICE_ROLE_KEY` (not the anon key)

### "Failed to save to Supabase" error

- Check your Supabase project is active (not paused)
- Verify your service role key has write permissions
- Check network connectivity to Supabase

### Database connection errors

- Ensure you've run the SQL schema creation script
- Verify table names match exactly (`snapshots` and `latest_snapshot`)

## Optional: Row Level Security (RLS)

Currently, the integration uses the service role key which bypasses RLS. If you want to add RLS policies:

```sql
-- Enable RLS
ALTER TABLE snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE latest_snapshot ENABLE ROW LEVEL SECURITY;

-- Allow service role to do everything (default)
-- Add custom policies for your frontend users as needed
```

## Support

For issues or questions:
- Check the main README.md
- Review the Supabase documentation: https://supabase.com/docs
- Check application logs for detailed error messages

