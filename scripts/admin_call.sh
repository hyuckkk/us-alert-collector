#!/usr/bin/env bash
# Worker 관리 경로 호출: admin_call.sh <preview-life|preview-family|preview-kids|setup|test-send|clicks|status|usage|run-cron|dispatch|digest|report|watchdog|source-health|baseline|baseline-start|baseline-stop>
set -euo pipefail
: "${US_WORKER_URL:?}"; : "${US_ADMIN_KEY:?run scripts/cf_env.sh first}"
args=(-sS -o out.json -w "%{http_code}" -H "Authorization: Bearer $US_ADMIN_KEY")
case "$1" in
  clicks|status|watchdog|source-health|baseline|usage) ;;
  compare) set -- "compare?since=${COMPARE_SINCE:-2026-10-01}" ;;
  workload) set -- "workload?since=${COMPARE_SINCE:-2026-10-01}" ;;
  event) set -- "event?q=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "${SKIP_KEY:?key 입력에 검색어}")" ;;
  catchup) args+=(-X POST -H "content-type: application/json" --data "{}") ;;
  catchup-dry) args+=(-X POST -H "content-type: application/json" --data '{"dry": true}'); set -- catchup ;;
  dump-life|dump-family|dump-kids) set -- "events-dump?bot=${1#dump-}" ;;
  baseline-start) args+=(-X POST -H "content-type: application/json" --data "{\"hours\": ${BASELINE_HOURS:-36}}"); set -- baseline ;;
  baseline-stop) args+=(-X POST -H "content-type: application/json" --data '{"hours": 0}'); set -- baseline ;;
  setup|test-send|run-cron|dispatch|digest|report|heartbeat) args+=(-X POST -H "content-type: application/json" --data "{}") ;;
  outbox-skip) args+=(-X POST -H "content-type: application/json" --data "{\"event_key\": \"${SKIP_KEY:?}\", \"version\": ${SKIP_VERSION:-null}, \"reason\": \"외부 재입력 시험의 표현 차이(변경 아님)\"}") ;;
  preview-life|preview-family|preview-kids) args+=(-X POST -H "content-type: application/json" --data "{\"bot\": \"${1#preview-}\", \"n\": ${PREVIEW_N:-4}}"); set -- preview ;;
  *) echo "unknown action $1"; exit 2 ;;
esac
code=$(curl "${args[@]}" "$US_WORKER_URL/admin/$1")
echo "HTTP $code"
# 공개 저장소의 실행 요약·주석에 텔레그램 ID 가 남지 않게 가린다(chat_id·from_id 값, 7자리 이상 숫자)
python3 - "$US_WORKER_URL" > shown.json <<'PY'
import json, re, sys
try:
    d = json.load(open("out.json"))
except ValueError:  # Worker 가 JSON 이 아닌 오류(예외 페이지)를 돌려준 경우: 원문 앞부분을 남겨 원인을 볼 수 있게
    d = {"ok": False, "non_json_body": open("out.json", errors="replace").read()[:1500]}
def red(x, k=None):
    if isinstance(x, dict):
        return {kk: red(v, kk) for kk, v in x.items()}
    if isinstance(x, list):
        return [red(v) for v in x]
    if k in ("chat_id", "from_id", "user_id", "OWNER_CHAT_ID") and x is not None:
        return "<id>"
    if isinstance(x, (int, str)) and re.fullmatch(r"-?\d{7,}", str(x)):
        return "<id>"
    if isinstance(x, str):
        return re.sub(r"(?<!\d)-?\d{9,}(?!\d)", "<id>", x.replace(sys.argv[1], "<worker>"))
    return x
d = red(d)
if isinstance(d.get("events"), list) and "q" not in d:  # 행사 목록은 확인에 필요한 필드만(주석 길이 제한). 한 건 추적(event)은 전체
    d["events"] = [{k: e.get(k) for k in ("event_key", "bot", "title_ko", "start_date", "rec_level", "rec_score", "category", "source_id", "url")} if isinstance(e, dict) else e
                   for e in d["events"]]
print(json.dumps(d, ensure_ascii=False, indent=1))
PY
{ echo "### $1 (HTTP $code)"; echo '```json'; cat shown.json; echo '```'; } | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
# 결과를 주석으로도 남긴다(로그를 받을 수 없는 환경에서 API 로 확인). 주석 한 개 최대 약 4KB
python3 -c "import json;s=json.dumps(json.load(open('shown.json')),ensure_ascii=False,separators=(',',':'));[print('::notice title=admin $1 (%02d)::%s' % (i+1, s[j:j+3500])) for i,j in enumerate(range(0,min(len(s),140000),3500))]"
case "$code" in 2*) ;; *) exit 1 ;; esac
python3 -c "import json,sys; sys.exit(0 if json.load(open('out.json')).get('ok') else 1)"
