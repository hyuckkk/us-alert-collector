# 0단계 설치 안내

비밀값은 **GitHub 한 곳**에만 넣습니다. Worker(Cloudflare) 쪽 비밀값은 `deploy-worker` 워크플로가 자동으로 복사하고,
Actions ↔ Worker 관리 키는 Cloudflare 토큰에서 자동으로 만들어지므로 따로 입력하지 않습니다.

입력 위치: **https://github.com/hyuckkk/us-alert-collector/settings/secrets/actions → New repository secret**

| Name | 값 | 구하는 곳 |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare API 토큰 | 아래 1 |
| `ANTHROPIC_API_KEY` | colorado-alerts 워크스페이스 키 | 아래 2 |
| `BOT_TOKEN_LIFE` | @ColoradoLife_bot 토큰 | 텔레그램 @BotFather → `/mybots` → 봇 선택 → API Token |
| `BOT_TOKEN_FAMILY` | @ColoradoFamily_bot 토큰 | 〃 |
| `BOT_TOKEN_KIDS` | @ColoradoKids_bot 토큰 | 〃 |
| `OWNER_CHAT_ID` | 내 텔레그램 숫자 ID | 한국 시스템과 같은 값. 모르면 아래 4 |
| `CLOUDFLARE_ACCOUNT_ID` | (선택) | Cloudflare 계정이 여러 개일 때만 |

명령줄을 쓰면(값을 물어보므로 셸 기록에 남지 않음):
```bash
for s in CLOUDFLARE_API_TOKEN ANTHROPIC_API_KEY BOT_TOKEN_LIFE BOT_TOKEN_FAMILY BOT_TOKEN_KIDS OWNER_CHAT_ID; do
  gh secret set "$s" -R hyuckkk/us-alert-collector
done
```

## 1. Cloudflare API 토큰
dash.cloudflare.com → 오른쪽 위 프로필 → **My Profile → API Tokens → Create Token**
→ 템플릿 **Edit Cloudflare Workers** → *Use template*
→ Permissions 에 한 줄 추가: **Account · D1 · Edit**
→ Continue → Create → 토큰 복사(다시 볼 수 없음)

처음 쓰는 Cloudflare 계정이면 **Workers & Pages** 화면을 한 번 열어 workers.dev 주소(서브도메인)를 만들어 둡니다.

## 2. Anthropic API 키
platform.claude.com → Settings → **Workspaces → colorado-alerts** → API keys → Create key. 만료일을 메모해 둡니다(6에서 등록).

## 3. 배포
GitHub → **Actions → deploy-worker → Run workflow**
D1 `us-alert-db` 생성·스키마 적용 → Worker `us-alert-worker` 배포 → 비밀값 복사 → 텔레그램 웹훅 등록까지 한 번에 합니다.

## 4. 봇 시작 → 테스트
1. 텔레그램에서 세 봇을 각각 열고 **시작(/start)**. "소유자로 확인되었습니다" 답이 오면 정상.
   - `OWNER_CHAT_ID` 를 몰라 비워 두었다면 봇이 "내 텔레그램 ID: …" 를 알려 줍니다 → 그 값을 넣고 3 을 다시 실행
2. **Actions → admin → Run workflow → action: `test-send`** → 세 봇에서 테스트 알림 1건씩 도착
3. 버튼을 누르면 화면 위에 `✅ 확인 처리했습니다. (D1 기록 #1)` 처럼 **D1 기록 번호**가 뜨고 버튼에 ✓ 가 붙음
4. 기록 확인(아무거나): 봇에 `/clicks` · **Actions → admin → `clicks`** · Cloudflare **D1 → us-alert-db → Console** 에서 `SELECT * FROM clicks ORDER BY id DESC;`

## 5. API 키 점검
**Actions → key-check → Run workflow** 1회 → 봇에 `/status` → `API 키 점검: 정상`.
이후 매일 자동 점검(토큰 0). 키가 거부되면 즉시 DM. 같은 실행에서 만료일 알림도 확인합니다.

## 6. 만료일 등록 (만료 2주 전 DM)
@ColoradoLife_bot 에 보냅니다.
```
/expiry anthropic 2027-04-20 colorado-alerts 워크스페이스 키
/expiry cloudflare-token 2027-10-20      ← 1에서 TTL 을 정했다면
/expiry                                  ← 목록
/expiry del cloudflare-token             ← 삭제
```
매일 09:05(한국) 확인해 **14·7·3·1·0일 전**에 한 번씩, 만료 후에는 매일 알립니다.

## 키 교체
- Anthropic: GitHub 비밀값 `ANTHROPIC_API_KEY` 를 새 값으로 바꾸고 `/expiry anthropic <새 만료일>`.
- 봇 토큰·소유자 ID: GitHub 비밀값을 바꾸고 deploy-worker 다시 실행.
- Cloudflare 토큰: GitHub 비밀값을 바꾸고 deploy-worker 다시 실행(관리 키도 함께 새로 만들어짐).

## 0단계 완료 기준
- [ ] deploy-worker 성공(모든 단계 초록)
- [ ] 봇 3개 모두 `/start` → 소유자 안내 수신
- [ ] `test-send` → 세 봇에서 테스트 알림 수신
- [ ] 버튼 3종 클릭 → `D1 기록 #n` 표시, `/clicks` 에 기록
- [ ] key-check 1회 성공, `/expiry anthropic …` 등록
