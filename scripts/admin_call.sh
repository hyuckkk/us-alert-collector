#!/usr/bin/env bash
# Worker 관리 경로 호출: admin_call.sh <setup|test-send|clicks|status|run-cron>
set -euo pipefail
: "${US_WORKER_URL:?}"; : "${US_ADMIN_KEY:?run scripts/cf_env.sh first}"
args=(-sS -o out.json -w "%{http_code}" -H "Authorization: Bearer $US_ADMIN_KEY")
case "$1" in
  clicks|status) ;;
  setup|test-send|run-cron) args+=(-X POST -H "content-type: application/json" --data "{}") ;;
  *) echo "unknown action $1"; exit 2 ;;
esac
code=$(curl "${args[@]}" "$US_WORKER_URL/admin/$1")
echo "HTTP $code"
{ echo "### $1 (HTTP $code)"; echo '```json'; python3 -m json.tool out.json | sed "s#${US_WORKER_URL}#<worker>#g"; echo '```'; } | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
case "$code" in 2*) ;; *) exit 1 ;; esac
python3 -c "import json,sys; sys.exit(0 if json.load(open('out.json')).get('ok') else 1)"
