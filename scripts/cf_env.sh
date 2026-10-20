#!/usr/bin/env bash
# CLOUDFLARE_ACCOUNT_ID, Worker 주소(US_WORKER_URL), 관리 키(US_ADMIN_KEY)를 GITHUB_ENV 에 넣는다.
# 관리 키는 Cloudflare 토큰에서 파생한다(따로 입력할 비밀값 없음). 토큰을 바꾸면 다음 배포 때 Worker 쪽도 함께 바뀐다.
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
echo "::add-mask::$SUB"   # workers.dev 서브도메인은 계정 이메일에서 만들어질 수 있어 로그에서 가린다
US_ADMIN_KEY=$(printf 'us-admin:%s' "$CLOUDFLARE_API_TOKEN" | sha256sum | cut -d' ' -f1)
echo "::add-mask::$US_ADMIN_KEY"
{
  echo "US_ADMIN_KEY=$US_ADMIN_KEY"
  echo "CLOUDFLARE_ACCOUNT_ID=$CLOUDFLARE_ACCOUNT_ID"
  echo "US_WORKER_URL=https://us-alert-worker.$SUB.workers.dev"
} >> "${GITHUB_ENV:-/dev/stdout}"
