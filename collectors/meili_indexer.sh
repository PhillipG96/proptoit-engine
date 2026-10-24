#!/usr/bin/env bash
set -euo pipefail
# meili_indexer.sh — Reindex PropToIt properties into Meilisearch
# Usage: bash meili_indexer.sh [--notify]

MEILI_URL="http://localhost:7700"
INDEX_NAME="properties"
MEILI_KEY="${MEILI_MASTER_KEY:?MEILI_MASTER_KEY environment variable is required}"
NOTIFY=false

if [[ "${1:-}" == "--notify" ]]; then NOTIFY=true; fi

echo "=== Meili Indexer: $(date) ==="

export PGPASSWORD="${PGPASSWORD:?PGPASSWORD environment variable is required}"

# Export properties as JSONL (one JSON object per line)
TMPFILE=$(mktemp)
psql -h localhost -U proptoit -d proptoit -t -A -c "
SELECT row_to_json(t) FROM properties t;
" > "$TMPFILE"

COUNT=$(wc -l < "$TMPFILE")
echo "Exported $COUNT rows from database"

# Wrap into JSON array
WRAPPER=$(mktemp)
echo "[" > "$WRAPPER"
awk '{printf "%s%s", (NR>1?",":""), $0}' "$TMPFILE" >> "$WRAPPER"
echo "]" >> "$WRAPPER"

# Ensure index exists with correct primaryKey
curl -s -X PUT "$MEILI_URL/indexes/$INDEX_NAME" \
  -H "Authorization: Bearer $MEILI_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"primaryKey":"id"}' > /dev/null 2>&1

# Delete existing documents
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
  -X DELETE "$MEILI_URL/indexes/$INDEX_NAME/documents" \
  -H "Authorization: Bearer $MEILI_KEY")
echo "DELETE returned: $HTTP_CODE"

# Import all documents
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "$MEILI_URL/indexes/$INDEX_NAME/documents?primaryKey=id" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $MEILI_KEY" \
  -d "@$WRAPPER")
echo "Import returned: $HTTP_CODE"

# Wait for indexing to finish
sleep 2
STATS=$(curl -s "$MEILI_URL/indexes/$INDEX_NAME/stats" \
  -H "Authorization: Bearer $MEILI_KEY")
DOCS=$(echo "$STATS" | python3 -c "import sys,json; print(json.load(sys.stdin)['numberOfDocuments'])" 2>/dev/null || echo "?")
echo "Index now contains: $DOCS documents"

rm -f "$TMPFILE" "$WRAPPER"

# Telegram notification
if [[ "$NOTIFY" == "true" ]]; then
  export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN environment variable is required}"
  export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-8606012827}"

  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c "
import json, sys
print(json.dumps({
  'chat_id': '$TELEGRAM_CHAT_ID',
  'text': '''🔍 *Meilisearch Index Complete*

Properties indexed: *${DOCS}*
Time: *$(date '+%H:%M %Z')*

Ready for search 🚀''',
  'parse_mode': 'Markdown'
}))")" > /dev/null

  echo "Telegram notification sent"
fi

echo "Done."
