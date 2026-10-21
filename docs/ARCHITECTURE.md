# 시스템 구조

설계안 「미국 체류 가족활동 알림 시스템 — Claude 교차검증 및 통합 설계안」의 처리 흐름을 그대로 코드로 옮겼다.
한국 시스템(`alert-collector`, `telegram-relay`, 한국 Worker)과는 코드 패턴만 참고하고 저장소·Worker·DB·봇·비밀값을 공유하지 않는다.

```
GitHub Actions (이 저장소, 공개)                         Cloudflare Worker us-alert-worker + D1 us-alert-db
┌──────────────────────────────────────────┐            ┌────────────────────────────────────────────┐
│ collect (매일 12:30 UTC)                  │            │ /admin/seen      이미 본 항목(주소+해시)        │
│  1 정보원 등록부 + 승격 후보               │─ seen ───▶ │ /admin/enqueue   행사 → 사람별 대기열          │
│  2 무토큰 수집 rss/ical/json/html/gnews   │            │   · 구독 봇 · 참가 가능 시작일 · 연례 예고      │
│  3 이미 본 것 제외, 실행당 상한            │            │   · 관심없음 · 차단 태그 · 소유자 전용 모드     │
│  4 html 새 링크는 상세 본문 붙이기(토큰 0) │            │   · 중요 필드 변경 시 새 판(version)           │
│  5 발굴(B): 요일 주제 웹 검색 → 후보       │─ cand ───▶ │ /admin/candidates 2회 이상 → 소유자에게 승격 질문│
│  6 추출: 작은 모델(구조화 출력)            │            │ /admin/dispatch  즉시·예고 발송(정지 시간 미룸) │
│  7 규칙: 링크 검증·지난 행사·유급·긴급도   │─ events ─▶ │ /admin/digest    주간 요약(현지 토 9시 이후)    │
│  8 애매한 건만 큰 모델 판단               │            │ /admin/run-log·usage·report 측정·주간 보고     │
│  9 연간 일정표(토큰 0)                    │            │ /hook/<bot>      버튼·명령(웹훅)               │
│ dispatch (매시) · key-check (매일) · test │            └───────────────┬────────────────────────────┘
└──────────────────────────────────────────┘                            │ 개인 DM
                                                     @ColoradoLife_bot · @ColoradoFamily_bot · @ColoradoKids_bot
```

## 파일
| 경로 | 역할 |
|---|---|
| `config/sources.json` | 정보원 등록부(A). 형식, 주기(every_days), 대상 봇, 공식 여부 |
| `config/queries.json` | 무토큰 검색어(매일 + 요일별 심층) |
| `config/models.json` | 모델·effort·묶음 크기·실행당 상한·월 예산·가격 |
| `config/annual.json` | 연간 고정 일정표 |
| `collector/fetch.py` | 무토큰 수집, 상세 페이지 본문 |
| `collector/ai.py` | **AI 호출이 있는 유일한 파일**: 키 점검, 추출, 판단, 발굴 |
| `collector/rules.py` | 규칙 처리(링크 환각 방지, 날짜, 유급 제외, 긴급도, 행사 키) |
| `collector/run.py` | 하루 실행 오케스트레이터 |
| `collector/probe.py` | 정보원 시험 조회 → `reports/probe.json` |
| `worker/src/index.js` | 라우팅, 웹훅(버튼·명령), 만료일, 키 점검, 상태 |
| `worker/src/pipeline.js` | seen / enqueue / dispatch / digest / 사용량 / 후보 / 보고 |
| `worker/src/render.js` | 7줄 알림 형식, 요약, 버튼 |
| `worker/migrations/*.sql` | D1 스키마 |

## 모델 사용 (설계안 '모델을 일의 난이도로 나눈다')
| 단계 | 모델 | 이유 |
|---|---|---|
| 추출 | claude-haiku-5-5, effort low, 구조화 출력 | 제목·날짜·가격 같은 정형 작업 |
| 판단 | claude-opus-5-5, effort medium, 서버측 대체 모델(fallbacks) | 자격·신분·연령이 애매한 건만 |
| 발굴 | claude-opus-5-5 + web_search(최대 5회) | 새 정보원 찾기. 검색 결과에 실제로 나온 URL 만 채택 |
| 키 점검 | 모델 목록 조회 | 토큰 0 |

## 안전장치
- **소유자 전용 모드**: `DELIVERY_MODE=owner-only`(기본). 지인이 승인돼 있어도 대기열·발송 단계에서 소유자 외에는 모두 건너뛴다.
- **링크 환각 방지**: AI 가 낸 URL 이 수집 주소와 다르면 수집 주소로 바꾼다. 발굴은 검색 결과에 나온 URL 만 쓴다.
- **비용 상한**: 실행당 AI 처리 건수 상한, 월 예산 초과 시 AI 단계 건너뜀(80% 경고).
- **비밀값**: GitHub 비밀값에만. 배포 때 Worker 비밀값으로 복사. 관리 키는 Cloudflare 토큰에서 파생. 로그에서 Worker 주소 가림.
- **개인정보**: 주소·아이 정보·텔레그램 ID 는 저장소에 없음. `/city` 는 숫자가 들어간 입력(번지·우편번호)을 거부.

## 2단계 변경 (1차 검증 반영, 브랜치 `stage2-fixes`)
```
collect full(매일)  : sources → search(34+) → discover(Kids·Family·Life + 요일 심층) → watch → queue → ai → enqueue → annual → candidates → fallback → dispatch → log
collect light(4시간, UTC 00:40·04:40·08:40·16:40·20:40 — full 12:30 과 합쳐 최대 간격 4시간 10분): watch → queue → ai → enqueue → fallback → dispatch → log
dispatch(매시)      : 발송·재시도 → 일간 요약 → (월) 주간 보고 → 수집 감시(26시간 넘으면 collect 재실행)
```
- **처리 대기열**(D1 `pending`): 새·바뀐 항목은 AI 전에 모두 보존. AI 결과는 `extracted` 로 저장 후 Worker 등록이 끝나야 `done`.
- **행사 식별**(`collector/identity.py`): 기준(iCal UID → 공식 상세 주소 → 기관+제목) + 연도 + 회차, 기존 행사와 유사도 대조.
- **상세 재검사**(D1 `watch`): 행사 관련 문장 지문이 바뀐 경우만 AI, 접수 상태별 주기.
- **발송**: 선점(`sending`) → 성공/재시도(`failed`)/불명(`uncertain`)/확실한 실패(`dead`), 요약은 10건씩 여러 통.
- **상태 기록**: 정보원별 상태(`source_health`), AI 중단(`kv.ai_health`), 마지막 수집 성공(`kv.last_collect_ok_at`), 사용량(캐시·검색·오류 포함).
- **개인정보**: 가족 정보는 `HOUSEHOLD_PROFILE`·`FAMILY_AVAILABLE_FROM` 비밀값, CI 에서 `PRIVACY_TERMS` 로 검사.

## 4단계 변경 (3차 검증 반영, 브랜치 `stage4-preop`)
- **행사 등록 = 알림 대기열 생성(한 트랜잭션)**: `/admin/enqueue` 는 받는 사람·확인 상태를 먼저 읽고 `events`·`items`·`watch`·`outbox` 를 한 `DB.batch` 로 쓴다. 하나라도 실패하면 모두 되돌아간다.
  `events.fanout_version` 은 알림 대기열을 만든 판. 이 값이 비었거나 현재 판보다 작으면(이전 코드의 실패 흔적) 내용이 같아도 재처리 때 빠진 사람 몫을 만든다. 이미 만든·보낸 사람 몫은 `UNIQUE(chat_id, bot, event_key, version)` 로 건너뛴다. 나중에 가입한 사람에게 지난 행사를 몰아 보내지 않는다.
- **대기열 임대 소유권**: `take` 가 항목마다 `lease_id`·`hash` 를 준다. `result` 는 같은 임대·같은 해시·임대 유효 시간 안일 때만 반영하고 나머지는 `rejected` 로 돌려준다. `extend` 로 자기 임대만 연장. `extracted` 보고 뒤에도 임대를 유지해 같은 실행이 등록 후 `done` 을 보고한다.
  수집기는 AI 호출·Worker 등록 직전에 임대를 연장하고, 잃은 항목은 처리·등록하지 않는다.
- **운영 첫날 기준선**: `/admin/baseline {hours}` 동안 새 행사는 조용히 등록하고, 3주 안에 열리거나 2주 안에 접수 시작·마감인 것만 일간 요약에 넣는다(즉시 알림 없음). 조용히 등록한 행사도 나중에 중요한 내용이 바뀌면 정상 알림.
- **light 실행**: UTC 00:40·04:40·08:40·16:40·20:40 + full 12:30 → 최대 간격 4시간 10분.

## 5단계 보완 (4차 검증 반영)
- **기준선 시작 대기**: 마이그레이션 0003 이 `kv.baseline_until = 'awaiting-start'` 를 심는다. 운영자가 `baseline-start`/`baseline-stop` 을 실행하기 전까지 기준선으로 동작해, main 병합 직후 첫 자동 수집이 먼저 돌아도 옛 정보 대량 발송·즉시 알림이 없다.
- **기준선 중 시간 알림**: 오늘 접수 시작·내일 마감 알림을 즉시(`now`) 대신 일간 요약(`digest`)으로 만든다. 처리 기록(`trigger_log`)은 남겨, 기준선이 끝난 같은 날 즉시 알림으로 중복되지 않는다.
- **운영 시작 순서 관문**: collect·dispatch 는 `scripts/worker_ready.sh` 로 배포된 Worker 가 기준선 상태를 갖췄는지 먼저 확인하고, 아니면(배포 전·배포 중) 그 실행을 건너뛴다.
- **staging-test**: 실행마다 `us-alert-worker-stg-<실행ID>-<시도>` / `us-alert-db-stg-<실행ID>-<시도>` 를 새로 만든다. 같은 이름이 있으면 시작하지 않고, 정리는 이 실행이 만들려고 한 이름만 한다. 시작 전 계정 공유 무료 한도를 재고, 잴 수 없으면 멈춘다(`scripts/cf_quota.py`).
