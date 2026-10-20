# 1차 독립 검증 자료 — 기본 코드와 전체 구조 (운영 시작 전)

- 저장소: https://github.com/hyuckkk/us-alert-collector (공개)
- 검증 대상 브랜치: **`stage1-review`** — main 과의 차이: https://github.com/hyuckkk/us-alert-collector/compare/main...stage1-review
- 기준: main = 0단계(봇 3개·D1·버튼 기록·만료일 알림), 운영 중. `stage1-review` 는 **운영에 반영되지 않았다**(Worker 배포·예약 실행은 main 에서만 동작).
- 구조: [docs/ARCHITECTURE.md](ARCHITECTURE.md) · 원 설계안: 「미국 체류 가족활동 알림 시스템 — Claude 교차검증 및 통합 설계안」

## 1. 구현한 기능
| 설계안 항목 | 구현 | 위치 |
|---|---|---|
| 정보원 등록부(A), 변경 빈도 | 21곳, every_days, 공식 여부, 대상 봇 | `config/sources.json` |
| 무토큰 수집 | rss/atom, ical, json 경로 매핑, html(새 링크/페이지 변경), Google News RSS | `collector/fetch.py` |
| AI 는 새것만 읽는다 | 주소+내용 해시를 D1 `seen` 으로 확인, 처리 성공 후에만 표시 | `run.py`, `pipeline.seen` |
| html 원문 확인 | 새 링크의 상세 페이지 본문을 붙여 추출(토큰 0) | `fetch.fetch_detail` |
| 세션 묶기 | 모든 주제 신규분을 한 실행에서 묶어 추출(12건/호출) | `ai.extract` |
| 모델 분할 | 추출 haiku-5-5 / 애매한 건 판단 opus-5-5 / 발굴 opus-5-5 + web_search | `ai.py`, `config/models.json` |
| 신규 정보원 발굴(B), 요일별 심층 | 요일 주제 웹 검색 → 후보 D1 → 2회 이상이면 소유자 DM [추가][무시] → 승격 정보원 자동 수집 | `ai.discover`, `pipeline.addCandidates` |
| 링크 환각 방지 | 추출 URL ≠ 수집 URL 이면 교체, 발굴은 검색 결과 URL 만 | `rules.finalize`, `ai.discover` |
| 신분 필터 | 유급 활동(F-2 취업 불가) 제외, 신분·주민 메모 줄 | `rules.py`, 프롬프트 |
| 긴급도 = 마감·선착순 | 마감 7일 이내, 선착순·추첨 접수가 7일 안에 열림 → 즉시, 그 외 주간 요약 | `rules.urgency` |
| 연도 구분, 실질 변경 재알림 | year_label, 중요 필드 해시 → 새 판 + "바뀐 점" 줄, 확인한 사람에게도 재알림 | `rules.py`, `pipeline.enqueue` |
| 7줄 알림 | 제목·주최 / 날짜·장소 / 연령·영어 / 비용 / 신청·마감 / 신분 메모 / 링크·확인 시각 | `render.renderEvent` |
| 개인 DM·버튼 | [확인][관심없음][이 태그 그만], 요약은 줄마다 [n ✅][n 🚫] | `render.js`, `index.handleCallback` |
| 수신자별 참가 가능 시기 | 소유자 기본 2027-01-01, 그 전 행사는 연례면 예고, 아니면 제외. 지인은 지금부터 | `pipeline.enqueue`, `/from` |
| 사람별 설정 | `/settings /quiet /tz /city /from`, 정지 시간대는 각자 현지 기준으로 미룸 | `index.personCommand`, `pipeline.dispatch` |
| 주간 요약·계절 예고 | 현지 토요일 9시 이후 1통, 연간 일정표 | `pipeline.digest`, `annual.py` |
| 측정·주간 보고 | 실행별 지표(D1 runs), 월 사용량(usage), 매주 월요일 보고 DM | `pipeline.runLog/usage/weeklyReport` |
| 토큰 폭주 방지 | 실행당 120건 상한, 월 $20 예산(80% 경고, 초과 시 AI 건너뜀) | `config/models.json`, `run.py` |
| 1차 검증 전 지인 발송 금지 | `DELIVERY_MODE=owner-only` 기본, 대기열·발송·요약 모두에서 소유자 외 건너뜀 | `common.ownerOnly` |

## 2. 테스트 결과
| 종류 | 결과 | 실행 |
|---|---|---|
| Worker 0단계 종단(가짜 텔레그램 + 로컬 D1) | 17/17 통과 | `cd worker && npm test` |
| Worker 파이프라인 종단 | 15/15 통과 (소유자 전용 모드 별도 인스턴스 검사 포함) | 〃 |
| 수집기 단위·통합(pytest, 가짜 AI·가짜 Worker) | 29/29 통과 | `python -m pytest collector/tests` |
| CI | push 마다 위 세 가지 실행(`test` 워크플로) | GitHub Actions |
| 정보원 시험 조회(실제 사이트, GitHub Actions) | `reports/probe.json` | `probe-sources` |
| 시험 실행(실제 사이트 + 실제 AI, Worker 쓰기 없음) | `reports/dry-run.json` | `collect` (브랜치에서 dry-run) |

시험 실행 추이 (결과가 기록된 3회, 상한 25건):
| 회 | 정보원 성공 | 수집 | 행사 | 비용 | 바뀐 점 |
|---|---|---|---|---|---|
| 1 | 6/14 | 493 | 10 | $0.28 | 날짜 전부 미확인(링크 글자만 AI 에 감) |
| 2 | 5/14 | 493 | 14 | $0.35 | 상세 본문 추가 → 시간·가격 추출, 날짜는 여전히 없음 |
| 3 | 10/15 | 472 | 4 | $0.30 | 연도 추론 규칙 + Westminster·Hyland Hills 실제 도메인(.gov) 반영 → 날짜 추출 |
비용의 약 90% 는 발굴 단계(opus + 웹 검색 5회)이고 추출(haiku)은 회당 $0.004 수준이다.

## 3. 발견된 문제 (검증 의견을 받고 싶은 순서)
1. **미국 사이트의 자동 조회 차단(설계안 1순위 위험) 실제 발생**: 21곳 중 약 10곳이 GitHub Actions 에서 403. 브라우저 위장(User-Agent 변조)으로 우회하지 않았다. 대안 후보: (a) 같은 기관의 공식 RSS·API 찾기 (b) Anthropic web_fetch 로 원문 조회(토큰 비용) (c) Cloudflare Worker 에서 조회(다른 IP 대역) (d) 제외하고 발굴(B)로 보완.
2. **발굴 단계 비용 비중**: 회당 약 $0.3, 매일이면 월 약 $9 로 전체의 대부분. 주기·검색 횟수·모델 선택 의견 필요.
3. **연도 없는 날짜 가정**: "월·일만 있으면 오늘 이후 가장 가까운 날짜"로 정함. 다음 해 행사를 올해로 잘못 넣을 가능성.
4. **첫 실행 밀린 항목**: 약 470건. 실행당 120건 상한으로 약 4일에 걸쳐 처리(haiku 기준 비용 미미). 그동안 오래된 항목 비율이 높다.
5. **D1 무료 요금제 호출당 쿼리 한도**: 묶음 처리(json_each)로 호출당 쿼리 수를 일정하게 했지만 운영 환경에서는 아직 검증 전.
6. **Cloudflare 무료 cron 5개를 한국 Worker 가 모두 사용** → 미국 쪽 매일·매시 작업을 GitHub Actions 예약으로 대신. 예약 지연(수 분~수십 분) 가능.
7. **2건 주소 오류(404)**: 덴버 공공도서관 API, 자연과학박물관 → 비활성.
8. **workers.dev 주소에 계정 이메일 앞부분 포함**: 로그·요약에서는 가렸다. Cloudflare 에서 서브도메인 변경 권장.
9. **공개 저장소 노출 사고 1건(처리 중)**: 테스트 코드에 집 주소 일부를 예시로 넣었다가 발견. 기록에서 제거한 새 브랜치(`stage1-review`)로 옮겼고, 옛 브랜치 `stage1-structure` 삭제는 소유자 승인 대기.
10. **0단계 실기 확인 미완**: 소유자 텔레그램에서 /start → test-send → 버튼 기록 확인을 아직 하지 않음.

## 4. 아직 미구현
- 분야별 기준가 표(분기 1회)와 가격 수준(price_level) 산출, 도시별 이동시간 표(사람별 `/city` 는 저장만 함)
- 선착순 등록 페이지의 오픈 전후 시간 단위 감시
- 항공 특가 이메일 중계, 크루즈 소개 정보원
- 주민 자격(Westminster 시, Hyland Hills 구역) 원문 확인 데이터, 학교 배정 확인
- 묶음 판정 품질 2주 병행 비교, 작은 모델 추출 오류율 표본 측정(측정 항목은 있음)
- 버튼 없는 보조 채널
- 운영 반영: Worker 0.2.0 배포, D1 `0002` 마이그레이션, collect·dispatch 예약 실행 → **승인 후 main 병합 시**

## 5. 검증자에게 묻는 질문
1. 정보원 차단 대안 (a)~(d) 중 무엇을 기본으로 하고, 이용약관상 피해야 할 것은?
2. 소유자 전용 모드가 대기열·발송·요약 세 곳에서 모두 막히는지(`common.ownerOnly` 사용처) 빠진 경로가 있는가?
3. 링크 환각 방지 규칙(수집 URL 로 교체)이 한 페이지에 여러 행사가 있는 경우에 충분한가?
4. 긴급도 규칙(마감 7일, 접수 7일 내 오픈)과 변경 재알림 기준(중요 필드 8개)이 적절한가?
5. 월 예산 $20, 실행당 120건, 발굴 매일 — 6개월 운영 측정 목적에 맞는 값인가?
6. 공개 저장소에 남는 `reports/probe.json`, `reports/dry-run.json`(공개 웹 내용만)에 문제 될 정보가 있는가?
7. 한국 시스템과의 분리(봇·비밀값·DB·저장소·Worker)가 코드상 지켜졌는가?
