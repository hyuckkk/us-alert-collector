#!/usr/bin/env bash
# CLOUDFLARE_ACCOUNT_ID 와 Worker 주소(US_WORKER_URL)를 찾아 GITHUB_ENV 에 넣는다.
set -euo pipefail
: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN secret not set}"
api() { curl -sS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" "https://api.cloudflare.com/client/v4/$1"; }
if [ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  CLOUDFLARE_ACCOUNT_ID=$(api "accounts?per_page=5" | python3 -c "import json,sys;r=json.load(sys.stdin).get('result') or [];print(r[0]['id'] if r else '')")
fi
[ -n "$CLOUDFLARE_ACCOUNT_ID" ] || { echo "Cloudflare account lookup failed (token permissions?)"; exit 1; }
echo "::add-mask::$CLOUDFLARE_ACCOUNT_ID"
SUB=$(api "accounts/$CLOUDFLARE_ACCOUNT_ID/workers/subdomain" | python3 -c "import json,sys;print((json.load(sys.stdin).get('result') or {}).get('subdomain',''))")
[ -n "$SUB" ] || { echo "workers.dev subdomain not found. Open Cloudflare dashboard → Workers & Pages once to create it."; exit 1; }
{
  echo "CLOUDFLARE_ACCOUNT_ID=$CLOUDFLARE_ACCOUNT_ID"
  echo "US_WORKER_URL=https://us-alert-worker.$SUB.workers.dev"
} >> "${GITHUB_ENV:-/dev/stdout}"
