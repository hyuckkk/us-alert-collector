# us-alert-collector

미국 체류(콜로라도, 2027-06 ~ 2028-08) 가족활동 알림 시스템. 설계안: 「미국 체류 가족활동 알림 시스템 — Claude 교차검증 및 통합 설계안」.

한국 시스템(`alert-collector`, `telegram-relay`, 한국 Worker)과 **코드 패턴만 참고**하고 저장소·Worker·DB·봇·비밀값은 하나도 공유하지 않는다.

| 역할 | 위치 |
|---|---|
| 수집·AI 판정 | 이 저장소 + GitHub Actions (`collector/`) |
| 발송·버튼·DB | Cloudflare Worker `us-alert-worker` + D1 `us-alert-db` (`worker/`). 비밀값은 GitHub 에만 넣고 배포 때 Worker 로 복사 |
| 봇 | @ColoradoLife_bot(성인·시스템 알림), @ColoradoFamily_bot(가족), @ColoradoKids_bot(어린이) |
| AI | `collector/ai.py` 한 곳에서만 호출. 키는 colorado-alerts 워크스페이스 키(GitHub 비밀값 `ANTHROPIC_API_KEY`) |

## 0단계 (현재)
- Worker·D1 뼈대, 봇 3개 웹훅, 소유자 DM 테스트 발송
- 버튼 `[✅ 확인] [🚫 관심없음] [🔕 이 태그 그만]` → D1 `clicks`·`item_state`·`muted_tags` 기록 (다시 누르면 취소)
- 만료일 알림: `/expiry <이름> <YYYY-MM-DD>` 로 등록 → 14·7·3·1·0일 전에 @ColoradoLife_bot DM
- API 키 점검: 매일 `key-check` 워크플로(토큰 0) → 키가 거부되면 DM. 같은 워크플로가 만료일 점검도 실행(Cloudflare 무료 cron 한도는 한국 Worker 가 사용 중)

설치·비밀값 입력: [docs/SETUP-0.md](docs/SETUP-0.md)

## 워크플로
| 이름 | 언제 | 하는 일 |
|---|---|---|
| deploy-worker | `worker/` push, 수동 | D1 생성·스키마 적용, Worker 배포, 비밀값 복사, 웹훅 등록 |
| admin | 수동(action 선택), `ticks/admin` push | test-send / clicks / status / setup / run-cron |
| key-check | 매일 00:05 UTC(09:05 한국), 수동 | Anthropic 키 점검, 만료일 알림·웹훅 점검 실행(run-cron), 30일 무활동 방지 커밋 |

## 로컬 검사
```bash
cd worker && npm ci && npm test   # 가짜 텔레그램 서버 + 로컬 D1, 실제로는 아무것도 보내지 않음
```

## 개인정보 원칙
주소·아이 정보·텔레그램 ID·토큰은 이 저장소에 넣지 않는다. 수신자 ID 는 D1, 토큰은 Cloudflare/GitHub 비밀값에만 둔다.
