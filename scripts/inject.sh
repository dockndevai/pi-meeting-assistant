#!/usr/bin/env bash
set -euo pipefail
SERVER_URL="${SERVER_URL:-http://localhost:3000}"
TRANSCRIPT="${1:?Usage: inject.sh '<transcript text>'}"

HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$SERVER_URL/api/transcript" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg t "$TRANSCRIPT" '{transcript: $t, source_file: "cli-inject", duration_seconds: 0}')")

# Note: FastAPI now returns 202 (Accepted) not 200
[ "$HTTP" = "202" ] && echo "✓ Injected. Open http://localhost:3000" || echo "✗ HTTP $HTTP"
