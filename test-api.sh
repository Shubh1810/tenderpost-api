#!/bin/bash

# TenderPost Scraper - API Testing Script
# Quick commands to test the API endpoints

API_URL="${API_URL:-http://localhost:8000}"

echo "🧪 TenderPost API Testing Suite"
echo "================================"
echo "API URL: $API_URL"
echo ""

# Function to make requests with formatted output
test_endpoint() {
    local name=$1
    local endpoint=$2
    local method=${3:-GET}
    
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "📍 Testing: $name"
    echo "   Endpoint: $method $endpoint"
    echo ""
    
    if [ "$method" = "GET" ]; then
        response=$(curl -s -w "\n%{http_code}" "$API_URL$endpoint")
        http_code=$(echo "$response" | tail -n 1)
        body=$(echo "$response" | sed '$d')
        
        if [ "$http_code" -eq 200 ]; then
            echo "✅ Status: $http_code OK"
            echo ""
            echo "Response:"
            echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"
        else
            echo "❌ Status: $http_code"
            echo ""
            echo "Response:"
            echo "$body"
        fi
    fi
    
    echo ""
}

# Test 1: Root endpoint
test_endpoint "Root Endpoint" "/"

# Test 2: Health check
test_endpoint "Health Check" "/health"

# Test 3: 2Captcha connectivity
test_endpoint "2Captcha Connectivity Test" "/api/test-2captcha"

# Test 4: Latest tenders (fast, no CAPTCHA)
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📍 Testing: Latest Tenders (No CAPTCHA)"
echo "   Endpoint: GET /api/tenders/latest?debug=true"
echo ""
echo "⚠️  This may take 15-30 seconds..."
echo ""

response=$(curl -s -w "\n%{http_code}" "$API_URL/api/tenders/latest?debug=true")
http_code=$(echo "$response" | tail -n 1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" -eq 200 ]; then
    echo "✅ Status: $http_code OK"
    echo ""
    
    # Parse JSON and show summary
    count=$(echo "$body" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('count', 0))" 2>/dev/null)
    pages=$(echo "$body" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('total_pages', 0))" 2>/dev/null)
    success=$(echo "$body" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('success', False))" 2>/dev/null)
    
    echo "📊 Summary:"
    echo "   • Success: $success"
    echo "   • Tenders Found: $count"
    echo "   • Pages Scraped: $pages"
    echo ""
    
    # Show first 3 tenders
    echo "📄 Sample Tenders (first 3):"
    echo "$body" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for i, tender in enumerate(data.get('items', [])[:3], 1):
    print(f\"\\n{i}. {tender.get('title', 'N/A')[:80]}\")
    print(f\"   Ref: {tender.get('ref_no', 'N/A')}\")
    print(f\"   Closing: {tender.get('closing_date', 'N/A')}\")
    print(f\"   Org: {tender.get('organisation', 'N/A')[:50]}\")
" 2>/dev/null || echo "$body" | python3 -m json.tool
else
    echo "❌ Status: $http_code"
    echo ""
    echo "Response:"
    echo "$body"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "✅ Testing complete!"
echo ""
echo "🚀 Next steps:"
echo "   1. Test with CAPTCHA: curl \"$API_URL/api/tenders?debug=true\""
echo "   2. View API docs: open $API_URL/docs"
echo "   3. View logs: docker logs -f tenderpost-scraper-dev"
echo ""

