# AI 독립 검색 작업량 기록 규격 (2026-10-23)

목적: 미국 Colorado 알림의 **검색 작업량**과 **새로 발견한 유효 행사**를 분리 계측하여, ChatGPT와 Claude가 기존 GitHub 수집기를 얼마나 보완하는지 확인한다. 각 AI의 사용 한도(구독 잔량)와 Claude API 결제·크레딧은 **서로 다른 지표**이다.

## 저장 위치
- ChatGPT 독립 검색: `research/gpt-workload/runs/YYYYMMDDTHHMMSSZ.json`
- Claude 독립 검색: `research/claude-workload/runs/YYYYMMDDTHHMMSSZ.json` (Claude 쪽이 별도로 실제 기록하도록 연동 필요)
- 기존 GitHub 자동 수집: 각 `collect.yml` 실행의 `reports/run.json` 아티팩트를 독립 출처로 유지
- 기록은 **실제 검색 1회 실행마다** 만들고, 신규 발송 0건이어도 저장. 같은 실행의 로그는 중복 생성하지 않는다.

## 필수 필드 (각 실행)
```json
{
  "schema": 1,
  "actor": "chatgpt",
  "started_at_utc": "ISO 8601",
  "finished_at_utc": "ISO 8601",
  "search_requests": 0,
  "queries": [],
  "url_read_attempts": 0,
  "official_detail_verified": 0,
  "candidate_events": 0,
  "duplicate_candidates": 0,
  "rejected_events": 0,
  "held_events": 0,
  "qualified_events": 0,
  "known_by_collector": 0,
  "verified_collector_misses": 0,
  "submitted_events": 0,
  "delivered_events": 0,
  "failed_searches": 0,
  "failed_page_reads": 0,
  "account_remaining_usage": "unknown",
  "notes": "",
  "qualified_urls": []
}
```

- 숫자는 **실제 수행·확인된 횟수**만 기입한다. 한 번의 검색에 결과 20개가 나와도 `search_requests=1`이다. 결과를 열지 않았으면 `official_detail_verified`에 넣지 않는다.
- `candidate_events`는 실제로 선별 검토한 모든 후보 수이고 `qualified_events`는 그중 공식 근거가 검증된 가치 있는 후보 수이다. **최종 제출한 10건만 기록하고 실제 검토한 100건을 숨기지 않는다.**
- `verified_collector_misses`는 해당 시간대의 기존 자동 수집기 결과/DB와 실제 대조한 건만 센다. GitHub 정보원 목록에 없는 사이트에서 나왔다는 이유만으로 누락으로 단정하지 않는다.
- `delivered_events`는 실제 발송 성공 근거가 있을 때만 채운다. 파일 제출만으로 발송 완료라고 적지 않는다.
- 현재 도구가 계정 구독 잔여량을 제공하지 않으면 `account_remaining_usage: "unknown"`으로 기록한다. 사용량 %를 추측하지 않는다.
- Claude API는 GitHub 수집기 내부에서 발생하는 비용이며 **Claude Max 앱 사용 한도나 ChatGPT 앱 사용 한도와 합산하지 않는다**.
- 공개 저장소이므로 행사 공식 URL/집계만 기록하고 토큰·비밀값·사적인 사용자 정보는 저장하지 않는다.
- 이 문서는 계측 기준이다. **ChatGPT 예약 재활성화, Claude 예약 변경, 코드 배포, Telegram 발송을 자동 수행하지 않는다.**
