#!/usr/bin/env bash
# 운영 시작 순서 보장(4차 검증 2): 배포된 Worker 가 새 코드 + D1 0003(기준선 상태)까지 적용된 경우에만 READY=1.
# main 병합 직후 deploy-worker 가 끝나기 전에 예약된 collect·dispatch 가 돌면 READY=0 으로 건너뛴다(실패로 치지 않음).
# 배포 직후 Worker 는 기준선 '시작 대기' 상태라, 운영자가 baseline-start 를 하기 전 첫 수집이 돌아도 즉시·대량 발송은 없다.
set -uo pipefail
: "${US_WORKER_URL:?}"; : "${US_ADMIN_KEY:?run scripts/cf_env.sh first}"
body=$(curl -sS -m 20 -H "Authorization: Bearer $US_ADMIN_KEY" "$US_WORKER_URL/admin/baseline" || true)
state=$(printf '%s' "$body" | python3 -c "import json,sys
try:
    j=json.load(sys.stdin); print('ok' if j.get('ok') and 'active' in j and j.get('baseline_until') is not None else 'no')
except Exception: print('no')")
if [ "$state" = ok ]; then
  echo "READY=1" >> "${GITHUB_ENV:-/dev/stdout}"
  printf '%s' "$body" | python3 -c "import json,sys;j=json.load(sys.stdin);print('worker ready; baseline active=%s awaiting_start=%s' % (j.get('active'), j.get('awaiting_start')))"
else
  echo "READY=0" >> "${GITHUB_ENV:-/dev/stdout}"
  echo "::notice title=worker-not-ready::배포된 Worker 가 아직 기준선 상태를 갖추지 않았습니다(배포 진행 중이거나 미배포). 이번 실행은 건너뜁니다."
fi
