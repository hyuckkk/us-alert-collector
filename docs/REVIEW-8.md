# 정식 운영 전 준비: 개인정보 이력 정리(A안) · PRIVACY_TERMS · 실제 행사 기준 목록 · 소유자 전용 실운영 절차

- 브랜치 `stage4-preop`. 이번에 실행한 것: 기준 목록 원문 수집(AI 없음, GitHub Actions 실행 [39542804436](https://github.com/hyuckkk/us-alert-collector/actions/runs/39542804436)·[39543068658](https://github.com/hyuckkk/us-alert-collector/actions/runs/39543068658)), 이력 재작성 **리허설**(이 세션의 임시 사본에서만, push 없음, 사본은 삭제).
- 하지 않은 것: 실제 이력 재작성·강제 push·브랜치 삭제·main 병합·운영 Worker 변경·지인 발송·예산 변경.

---

## 1. 개인정보 과거 이력 정리 — A안(기존 저장소 유지)

### 1-1. 정정 사항
4차 자료(REVIEW-4)에서 "각 브랜치의 현재 파일에는 이제 없다"고 적었으나 **`stage4-preop` 에만 해당**했다. 지금도 아래 브랜치의 최신 파일에 남아 있다.

| 브랜치 | 최신 파일에 남은 곳 | 이력에 남은 곳 |
|---|---|---|
| `main` | 없음 | 없음(정리 대상 아님) |
| `stage1-structure` | 4개 파일 | 있음(주소 일부 포함) |
| `stage1-review` | 5개 파일(AI 지시문의 가구 정보, 참가 가능일 예시 등) | 있음 |
| `stage2-fixes` | 1개(문서의 참가 가능일) | 있음 |
| `stage3-fixes` | 1개(문서의 참가 가능일) | 있음 |
| `stage4-preop` | 없음 | 있음 |

노출 항목 종류(값은 적지 않음): 집 주소 일부(번지·거리명), 우편번호가 들어간 가구 설명, 체류 기간, 체류 신분 문장, 자녀 학년 기준 문장, 가족 참가 가능일. 커밋 메시지·작성자 이메일에는 없음(모두 noreply). 포크 0, PR 0, 태그 0.

**주의:** 우편번호 숫자 자체는 공개 행사 장소 주소(`reports/dry-run.json`)에도 나온다. 숫자만 바꾸면 공개 데이터까지 망가지므로 **문장 단위로만** 바꾼다.

### 1-2. 리허설 결과(임시 사본, push 없음)
- 도구: `git-filter-repo` 2.47.0, `--replace-text`(치환 규칙 파일은 저장소 밖 비공개 파일), `--prune-empty never`, 범위는 `main` 에 없는 커밋만(`--refs stage1-review stage1-structure stage2-fixes stage3-fixes stage4-preop ^main`).
- 결과:

| 항목 | 결과 |
|---|---|
| 이력 개인정보 검사(같은 검사어 7개) | 재작성 전 **23건** → 재작성 후 **0건** |
| `main` | 커밋·내용 그대로(`c6d1760` 유지) |
| 브랜치별 커밋 수 | 모두 동일(stage1-review 23, stage1-structure 20, stage2-fixes 36, stage3-fixes 47, stage4-preop 58) |
| 바뀐 커밋 | 62개(첫 변경 지점 `[old-commit]`, `[old-commit]`) |
| 최신 파일 | `stage4-preop` 동일, stage2·3 은 문서 1줄·테스트 1줄, stage1-* 는 해당 문장만 |
| 리허설 브랜치 끝 커밋(참고, 실제 실행 때 달라질 수 있음) | stage1-review `[old-commit]→[old-commit]`, stage1-structure `[old-commit]→[old-commit]`, stage2-fixes `[old-commit]→[old-commit]`, stage3-fixes `[old-commit]→[old-commit]`, stage4-preop `[old-commit]→[old-commit]`(리허설 시점) |
| 소요 | 1초 미만 |

치환 규칙(종류만): 주소 일부 → 예시 주소, 가구 설명 문장 → "[redacted — HOUSEHOLD_PROFILE 비밀값으로 제공]", 체류 기간 → "[stay period redacted]", 자녀 문장 → "[redacted]", 참가 가능일 예시 → `2027-01-01`, 테스트 단언문의 우편번호 조건 삭제.

### 1-3. 실제 작업 절차(승인 후)

| 단계 | 내용 | 확인 |
|---|---|---|
| 0 | `PRIVACY_TERMS` 입력(§2) → CI privacy 이력 검사가 **실패**하는지 확인(정리 전 기준) | 실패 건수 기록 |
| 1 | 진행 중 작업 멈춤: 열린 실행 없음 확인, 이 세션 브랜치 작업 커밋·push 완료 | Actions 실행 0 |
| 2 | **백업**: `git clone --mirror` + `git bundle create us-alert-backup.bundle --all` → 소유자 개인 저장소(로컬 디스크 등, **공개 저장소·클라우드 공개 위치 금지**) | `git bundle verify` 통과, 브랜치 6개·커밋 수 기록 |
| 3 | 치환 규칙 파일을 저장소 밖 비공개 위치에 작성(소유자가 직접, 또는 이 세션의 임시 폴더) | 파일이 저장소에 없음 |
| 4 | 미러 사본에서 리허설과 같은 명령으로 재작성 | 이력 검사 0건, `main` 해시 불변, 브랜치별 커밋 수 불변 |
| 5 | 강제 push(브랜치 5개만): `git push --force-with-lease origin <새해시>:refs/heads/<브랜치>` 를 브랜치별로. `main` 은 push 하지 않음 | GitHub 의 각 브랜치 끝 해시 = 새 해시 |
| 6 | `stage1-structure` 삭제(A안 포함 항목) | 브랜치 목록 5개 |
| 7 | CI privacy 이력 검사 재실행 → **통과** | 종료 코드 0 |
| 8 | GitHub 지원 요청(§1-5) | 접수 번호 |
| 9 | 이 세션·다른 PC 의 옛 클론 폐기 후 새로 클론(옛 클론에서 push 하면 옛 이력이 되살아남) | — |
| 10 | 백업 보관 기간 결정(예: 지원 처리 완료 + 30일 후 안전 삭제) | — |

### 1-4. 영향
- 커밋 해시가 바뀌어 지금까지의 검증 자료(REVIEW-1~7)의 커밋 링크는 옛 해시를 가리킨다 → 재작성 후 "옛 해시 → 새 해시" 대응표(`filter-repo/commit-map`, 해시만)를 문서로 남긴다.
- GitHub Actions 실행 기록은 옛 해시에 묶여 남는다. 실행 로그에는 코드 내용이 없지만, 옛 커밋 참조를 줄이려면 옛 실행 기록 삭제를 함께 검토(별도 승인).
- 리허설로 확인한 범위에서는 `main`·운영 Worker·D1·한국 시스템 영향 없음.

### 1-5. 복구 방법
- 재작성 결과에 문제가 있으면: 백업 미러에서 해당 브랜치만 `git push --force-with-lease` 로 되돌린다. **되돌리면 개인정보도 다시 공개**되므로 비상시에만.
- 백업 번들은 개인정보를 포함하므로 공개 위치에 올리지 않는다.

### 1-6. GitHub 지원 요청 내용(초안)
요청처: https://support.github.com/contact (Privacy·sensitive data 관련 요청). 참고 문서: [Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)

> Repository: hyuckkk/us-alert-collector (public).
> We rewrote the history of branches stage1-review, stage2-fixes, stage3-fixes, stage4-preop and deleted stage1-structure to remove personal information (home address fragment and household details). No forks, no pull requests, no tags.
> Please (1) remove cached views of the old commits and (2) run garbage collection so the dereferenced commits are no longer reachable by SHA.
> Affected old commits: attached list of old commit SHAs (commit-map, 62 commits; first changed: [old-commit]…, [old-commit]…; old branch tips [old-commit], [old-commit], [old-commit], [old-commit], <stage4-preop 끝>).

첨부할 것: 옛 해시 목록(해시만, 내용 없음). 개인정보 값 자체는 지원 요청에도 쓰지 않는다.

---

## 2. `PRIVACY_TERMS` 직접 설정 방법

1. https://github.com/hyuckkk/us-alert-collector → **Settings → Secrets and variables → Actions → New repository secret**
2. Name: `PRIVACY_TERMS`
3. Secret: 한 줄에 하나(또는 쉼표 구분). 대소문자 무시, **4글자 이상만** 검사됨.
   - 넣을 것: 번지+거리명, 가족 이름(영문·한글), 자녀 학교명, 소유자·지인 텔레그램 숫자 ID, 가족 참가 가능일(YYYY-MM-DD), `HOUSEHOLD_PROFILE` 에 쓴 고유한 문구.
   - 피할 것: 도시 이름(공개 행사 정보에 흔함), 우편번호 숫자만(공개 행사 주소와 겹침 — 꼭 넣으려면 "zip 80…" 처럼 문구로), 3글자 이하 낱말(무시됨).
4. 저장 후 Actions → test → 최근 실행 **Re-run**.
   - 현재 파일 검사: 통과해야 정상.
   - 이력 검사: §1 정리 전에는 **실패가 정상**(실제 과거 노출). 정리 후 통과.
- 명령줄을 쓴다면 `gh secret set PRIVACY_TERMS --repo hyuckkk/us-alert-collector` (값을 묻는 창에 입력 — 셸 기록에 남지 않음).
- 값은 채팅·이슈·파일에 쓰지 않는다. 검사 결과에는 "몇 번째 항목"만 나온다.

---

## 3. 실제 행사 기준 목록(원문 확인)

### 3-1. 방법
- `collector/verify_ground_truth.py`(AI 없음, 토큰 0)를 GitHub Actions 에서 실행: 30건 원문 페이지 본문, 감시 중 정보원 26곳 목록 대조, 감시하지 않는 공식 정보원 5곳 추가 시도.
- 원문 문장: `eval/ground_truth_pages.json` · 정리 결과: `eval/ground_truth.json`(유효 18건 + 제외 13건, 원문에 없는 값은 "미확인").
- 오늘 기준 2026-10-21. 사람의 표본 재확인은 아직.

### 3-2. 유효 행사 18건

| # | 행사 | 날짜·시간 | 장소 | 비용 | 대상·신청 | 감시 정보원 |
|---|---|---|---|---|---|---|
| 1 | Car Seat Clinic(Westminster 소방서) | 10/24(토) 8:30–11:30 시간대 | 미확인 | 무료 | 카시트 점검, 예약 방식 미확인 | 발견 |
| 2 | Trunk or Treat(Downtown Westminster) | 10/24(토) 12–2pm | Kokopelli Beer Co. 주차장 | 무료 | 전 연령 | 발견 |
| 3 | Fire Safety Booth(Westminster Sports Center) | 10/27(화) 10am–noon | 6051 W. 95th Ave, Westminster | 무료(연기 감지기 배포) | 신청 언급 없음, 10/28·10/29 다른 장소 | 발견 |
| 4 | SIP 시음 공연(Catamounts) | 10/2–10/25 목–일 저녁 | Orchard Town Center | 유료(금액 미확인) | 성인 | 발견 |
| 5 | USAFA 홈스쿨: Outer Solar System | 10/30(금) 10:30–11:45 | USAFA Planetarium | 미확인 | RSVP — **정원 마감**, 기지 출입증 | 발견 |
| 6 | USAFA 미식축구 vs UConn | 10/31(토) 1–4pm | Falcon Stadium | 유료(금액 미확인) | 티켓 | 발견 |
| 7 | USAFA 홈스쿨: 3rd Rock | 11/9(월) 10:30–11:45 | USAFA Planetarium | 무료 | RSVP, 기지 출입증 | 발견 |
| 8 | USAFA 미식축구 vs San Jose State | 11/14(토) 5–9pm | Falcon Stadium | 유료(금액 미확인) | 티켓 | 발견 |
| 9 | USAFA 홈스쿨: Earth–Sun–Moon | 11/18(수) **10:45**–noon | USAFA Planetarium | 미확인 | RSVP, 기지 출입증 | 발견 |
| 10 | Holly & Ivy Concert(USAFA 군악대) | 12/4(금) 7pm(12/5·6 추가) | Arnold Hall Theater | 무료 | 티켓·출입 추후 공지 | 발견 |
| 11 | Mornings at the Museum(CU 자연사박물관) | 특정 화요일 9:30–11(날짜 미확인) | 1030 Broadway, Boulder | 무료 | 2–5세+보호자, 사전 등록 | 발견 |
| 12 | Campus Life 전시(CU 지구과학도서관) | ~2026년 12월 | 2200 Colorado Ave, Boulder | 미확인 | 공개 여부 미확인 | 발견 |
| 13 | Thornton WinterFest | 12/11–12/13 | Carpenter Park Fields | 입장료 미확인, 산타 아침식사 $6/$4 | 감각 친화 산타 예약 11/16 시작 | **못 찾음(미감시)** |
| 14 | Thornton 겨울·봄 프로그램 등록 | 11/24 7am 시작, 수영 11/30(주민)·12/1(비주민), 체조 12/2·12/3 | 온라인·현장 | 미확인 | 접수 시작 알림 대상 | **못 찾음(미감시)** |
| 15 | DAM 무료 입장일 + Night at the Museums | 11/7 10am–10pm | Denver Art Museum | 무료(DIVA 제외) | 날짜 선택 등록 | **못 찾음(미감시)** |
| 16 | DAM 무료 입장일 | 12/8 10am–8pm | Denver Art Museum | 무료(DIVA 제외) | 날짜 선택 등록 | **못 찾음(미감시)** |
| 17 | DAM Día del Niño | 2027-04-25 10am–5pm | Denver Art Museum | 무료 | 세부 2027년 봄 | **못 찾음(미감시)** |
| 18 | Broomfield Camp Explorer | 여름 8주+2주(날짜 미확인) | 미확인 | 미확인 | 5–14세 견학 캠프 | **못 찾음(미감시)** |

### 3-3. 제외 13건
- 지난 행사 5: Westminster 10/20 2건, Thornton 10/17 Trunk or Treat · 4/4 · 7/4(연례 — 2027년 날짜 미확인).
- 참가 제한 4: USAFA 가족 영화 2건(USAFA 상주 인원·가족 전용), CU 학생 전용 1, 교직원 전용 1.
- 원문 확인 불가 3: USAFA 연극(날짜 추출 실패), CU 전시 2건(기간 추출 실패).
- 날짜 없는 민간 유료 투어 1.
- 원 초안 30건 + 추가 1건(Thornton 등록) = 31건.

### 3-4. 발견율(정보원 단계, AI 이전)

| 구분 | 유효 | 감시 정보원 목록에 지금 실제로 나옴 |
|---|---|---|
| 감시 중 정보원 출처 | 12 | **12 (100%)** |
| 감시하지 않는 공식 정보원 출처 | 6 | **0 (0%)** |
| 전체 | 18 | **12 (67%)** |

- 해석: 감시 중인 곳은 목록 단계에서 놓치지 않았다. 반면 **Thornton·Denver Art Museum·Broomfield 캠프처럼 감시하지 않는 공식 정보원의 행사는 전혀 잡히지 않는다.** 특히 Thornton 등록 시작일은 이 시스템의 핵심 알림 대상이다.
- 한계:
  - 초안을 감시 중 정보원 목록에서 뽑았으므로 감시 출처의 100% 는 높게 나오는 편향이 있다.
  - AI 추출·판정·알림까지의 최종 발견율은 아직 재지 않았다(유료 AI 건식 실행 필요).
  - 키워드 검색·AI 발굴이 미감시 정보원을 일부 찾을 수 있으나 이번 측정에는 포함되지 않았다.
- 추가 시도한 미감시 정보원: Thornton 축제 목록(25건 수집), Federal Heights(0건), Northglenn·Rocky Mountain Arsenal(404), Adams County(403).
- 제안(승인 대상, 코드 변경 최소):
  - 정보원 추가: Thornton 축제·행사 목록, Thornton 활동 등록 안내, Denver Art Museum 무료 입장일 페이지, Broomfield Camp Explorer 페이지. 모두 이미 수집기에 있는 html 방식으로 설정만 추가.
  - 이후 AI 건식 실행 1회(추정 $1–2)로 최종 발견율 측정.

---

## 4. 소유자 전용 실운영 시험 — 배포 절차와 확인 항목(실행하지 않음)

### 4-1. 사전 조건
| 항목 | 상태 |
|---|---|
| `PRIVACY_TERMS` 입력, 현재 파일 검사 통과 | 대기(소유자) |
| 이력 정리 A안 실행 여부 결정(병합 전 권장 — 병합 후 재작성하면 main 도 다시 써야 함) | 대기(승인) |
| GitHub 비밀값: 봇 토큰 3개, `OWNER_CHAT_ID`, `ANTHROPIC_API_KEY`, `HOUSEHOLD_PROFILE`, `FAMILY_AVAILABLE_FROM`, Cloudflare 토큰·계정 | 기존 입력분 확인 |
| `DELIVERY_MODE=owner-only`(wrangler.toml 기본) | 유지 |
| 예산 월 $20(실운영 예상 약 $45 — 월 중 AI 단계 정지 가능, 수집 항목은 대기열 보존) | 유지 |
| 소유자가 3개 봇에 `/start` | 확인 필요 |

### 4-2. 배포 순서
1. **PR 생성 → 검토 → main 병합**(승인). 병합 순간 main 의 예약 작업(collect 매일 12:30 + 4시간 간격 light, dispatch 매시)이 켜진다.
2. **deploy-worker 자동 실행**: D1 0003 적용(기준선 '시작 대기' 상태 기록) → Worker 0.3.x 배포 → 비밀값 동기화 → 웹훅 재등록.
   - 이 사이 예약 collect·dispatch 는 준비 관문(`worker_ready.sh`)에서 건너뛴다.
3. **배포 확인**:
   - `admin` → `status`: 버전, 웹훅 3개.
   - `admin` → `baseline`: `awaiting_start: true`.
4. **소유자 텔레그램 확인**: `admin` → `test-send` → 3개 봇 DM 수신 → 버튼 클릭 → `admin` → `clicks` 에 기록.
5. **첫 수집**: 예약 실행을 기다리거나 `collect` 를 수동 실행(mode full, dry_run false). 실행 보고서에서 다음을 확인한다.
   - 정보원 성공률
   - 대기열 건수
   - AI 사용액
   - 행사 수
   - 알림 대기열 종류: 모두 요약(digest)이어야 한다.
6. **기준선 시작**: `admin` → `baseline-start`(36시간 고정. 72시간이 필요하면 워크플로 입력 추가가 필요 — 현재 없음).
7. **다음 날 아침(소유자 시간대 8시) 요약 수신 확인** → 기간 만료 또는 `baseline-stop`.
8. **3~7일 자동 운영**(소유자 전용).

### 4-3. 매일 확인 항목
| 항목 | 기준 | 위치 |
|---|---|---|
| collect 성공 | 매일 full 1회 + light 5회, 실패 시 사유 | Actions, `admin` → `status` |
| 수집 감시(watchdog) | 26시간 이상 공백 없음 | dispatch 실행 주석 |
| 정보원 상태 | 연속 실패 정보원 목록 | 소유자 봇 `/report`(주간 보고) |
| 대기열 | 오래된 대기·보류(stuck) 건수 증가 없음 | 실행 보고서 `queue_*` |
| AI 사용액 | 하루 약 $1.5 이하, 월 $20 에서 정지 | `usage`, 50/80/100% 알림 |
| 알림 | 중복 0, 새 정보 없는 날 0통, 요약 1일 1회(10건 단위 분할) | 텔레그램, `outbox` 상태 |
| 시간 알림 | 접수 시작·마감 전날 ⏰ 정상(기준선 후) | 텔레그램 |
| 발견율 | 기준 목록 18건 중 수집·알림된 수 | `collector/evaluate.py` — 운영 Worker 행사 목록이 필요하며 **Actions 실행 경로는 아직 없음**(운영 시작 전 준비 필요) |

### 4-4. 중단·복구
- **즉시 멈춤**: Actions 에서 `collect`·`dispatch` 워크플로 Disable(또는 `gh workflow disable`). Worker 는 그대로 두어도 발송 경로가 없다.
- **Worker 되돌리기**: `npx wrangler rollback`(이전 배포로). D1 0003 은 열·표 추가뿐이라 이전 Worker 와도 충돌 없음.
- 한국 Worker·D1·예약 작업은 어떤 단계에서도 건드리지 않는다.

### 4-5. 승인 요청 순서(제안)
1. `PRIVACY_TERMS` 입력(소유자)
2. 이력 정리 A안 실행 승인(병합 전)
3. 미감시 정보원 4곳 추가 + AI 건식 실행 1회(최종 발견율 측정, 약 $1–2)
4. main 병합·운영 배포(소유자 전용)
5. 3~7일 운영 후 지인 발송·예산 결정
