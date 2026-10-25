# 경기 달력 자료 계약 (수집기 → Codex 달력 페이지) — 이슈 #27·#43

수집기(Claude 담당)가 `events.data` 에 넣는 경기 필드다. 화면(`worker/src/sports.js`)은 Codex 담당이며 이 문서는 그쪽이 읽을 값의 약속이다.
모든 필드는 **경기·대회가 아니면 null(목록은 [])** 이다. 값을 모르면 지어내지 않고 null 로 둔다.

| 필드 | 값 | 설명 |
|---|---|---|
| `sport` | 소문자 영어(`gymnastics`, `basketball`, `ice hockey` …) | 종목. 강습·등록 공지도 종목은 채워질 수 있으므로 **경기 여부는 `event_type` 으로 판단** |
| `event_type` | `match` · `meet` · `tournament` · null | `match` = 두 팀(A vs B, A at B). `meet` = 한 대회에 여러 팀(invitational, tri/quad meet, dual meet, league·regional·state meet). `tournament` = 며칠에 걸친 대진. null = 경기가 아님(연습·선발·강습·캠프·시즌 등록) |
| `school_level` | `elementary` · `middle` · `high` · `university` · null | 정보원 설정(`config/sources.json` 의 `school_level`)이 모델 추출보다 우선. 지금 `sports.js schoolGrade()` 가 그대로 읽는 값만 쓴다 |
| `level_detail` | `school_team` · `club` · `youth_league` · null | 대학 동아리(CU Sport Clubs)는 `university` + `club`. K–8·시 리그는 학년에 따라 `elementary`/`middle` + `youth_league`(학교 대표팀인 것처럼 표시하지 말 것) |
| `home_team`, `away_team` | 원문 표기 | `match` 일 때만. `meet` 은 항상 null(다자 대회를 1:1 로 꾸미지 않음) |
| `participants` | 문자열 목록(최대 24, 중복 제거) | `meet` 참가 학교·팀 전체. 대회 1개 = 행사 1건(팀별로 나누지 않음) |
| `start_time` | `"HH:MM"`(24시간, America/Denver) · null | iCal 은 DTSTART 를 덴버 현지 시각으로 바꾼 값. 자정(종일 일정)은 null |
| `venue` | 문자열 · null | 실제 경기 장소(= `location`). 이동시간 계산은 이 값과 `city` 로 |
| `city` | 문자열 · null | 개최 도시(기존 필드) |
| `cost_text` | 한국어 · null | **관중 입장료만**. 팀·리그 참가비는 `eligibility` 로 간다 |
| `cost_class` | `free` · `paid` · `unknown` | 원문에 무료가 적힌 경우만 `free`. 비어 있거나 애매하면 `unknown` |

## 함께 바뀐 점(화면 변경 없이 지금 Worker 판별이 통과하도록)
- 체육 정보원 아이디에는 `athletics` 가 들어간다(`chsaa-gymnastics-athletics`, `chsaa-state-championships-athletics`. 대학은 기존 `du-athletics-ics` 등) → `ATHLETIC_SOURCE` 통과.
- `event_type` 이 `match`·`meet`·`tournament` 인 행사는 `title_ko` 에 '경기'를 붙인다(예: `프론트레인지 리그 체조 대회 경기`). 지금 `isCalendarMatch` 의 `VS` 조건은 `vs/@` 만 보므로 `Women's Gymnastics at Denver Quad`, `Soccer at San Diego` 같은 원정·다자 대회가 빠졌다. 이 처리로 `(match|game|경기) + 체육 정보원` 조건을 통과한다.
- **Codex 에 요청(선택)**: `isCalendarMatch` 가 `e.event_type in (match, meet, tournament)` 를 직접 인정하면 위 제목 처리 없이도 된다. 그때도 위 필드는 그대로 유지한다.

## 진단(2026-10-25 운영 DB 읽기 전용, `collector/sports_diag.py`)
- 전체 행사 889 · 경기류 233 · 앞으로 열릴 경기 213 · 달력 판별 통과 202 — **202건 모두 대학**(DU 48, CU 47, Air Force 47, CSU 35, Mines 21 등 대학 iCal 5곳)
- 초·중·고 0건의 원인: 고교·중학교·유소년 리그 **정보원 자체가 없었다**(분류 오류가 아님). 체조 0건: 대학 체조는 1~3월 시즌이고 2027 일정이 아직 발표 전, 고교 체조는 CHSAA 정보원이 없었다.
- 판별 미통과 11건은 모두 '대 경기' 표시가 없는 리그 모집·지역 행사(피클볼 대회, 유소년 리그 등록 등)로 경기 달력 대상이 아니다.
