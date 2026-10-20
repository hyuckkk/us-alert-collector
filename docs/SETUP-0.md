# 0단계 설치 안내 — 비밀값 입력 위치와 명령어

비밀값은 **두 곳에만** 들어갑니다. 저장소 파일·채팅·커밋에는 넣지 않습니다.

| 비밀값 | GitHub (us-alert-collector → Actions secrets) | Cloudflare (us-alert-worker → Secrets) |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | ○ | |
| `US_ADMIN_KEY` (임의 문자열) | ○ | ○ 이름은 `ADMIN_KEY`, **같은 값** |
| `ANTHROPIC_API_KEY` (colorado-alerts 키) | ○ | |
| `BOT_TOKEN_LIFE` / `BOT_TOKEN_FAMILY` / `BOT_TOKEN_KIDS` | | ○ |
| `OWNER_CHAT_ID` (내 텔레그램 숫자 ID) | | ○ |
| `CLOUDFLARE_ACCOUNT_ID` (선택) | ○ 계정이 여러 개일 때만 | |

---

## 1. GitHub 저장소 만들기
github.com/new → 이름 `us-alert-collector`, **Private**, README 없이 빈 저장소로 생성.
(생성 후 Claude 에게 알려 주면 코드를 push 합니다.)

## 2. Cloudflare API 토큰 (GitHub Actions 가 배포에 사용)
dash.cloudflare.com → 오른쪽 위 프로필 → **My Profile → API Tokens → Create Token**
→ 템플릿 **Edit Cloudflare Workers** → *Use template*
→ Permissions 에 한 줄 추가: **Account · D1 · Edit**
→ Account Resources: 내 계정 / Zone Resources: All zones (템플릿 기본값)
→ (선택) TTL 에 만료일을 정했다면 7단계에서 그 날짜도 등록
→ Create → 표시된 토큰 복사(다시 볼 수 없음)

## 3. 관리 키(US_ADMIN_KEY) 만들기
GitHub Actions 가 Worker 관리 경로를 부를 때 쓰는 임의 문자열입니다. 웹훅 검증값도 여기서 자동으로 만들어집니다.

```bash
# Mac / Linux / Git Bash
openssl rand -hex 32
```
```powershell
# Windows PowerShell
[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
```

## 4. Anthropic API 키
platform.claude.com → Settings → **Workspaces → colorado-alerts** → API keys → Create key.
만료일을 정했다면 그 날짜를 메모해 둡니다(7단계에서 등록).

## 5. GitHub 비밀값 입력
github.com/hyuckkk/us-alert-collector → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `CLOUDFLARE_API_TOKEN` | 2단계 토큰 |
| `US_ADMIN_KEY` | 3단계 문자열 |
| `ANTHROPIC_API_KEY` | 4단계 키 |

명령줄을 쓰면(값을 물어보므로 셸 기록에 남지 않음):
```bash
gh secret set CLOUDFLARE_API_TOKEN -R hyuckkk/us-alert-collector
gh secret set US_ADMIN_KEY         -R hyuckkk/us-alert-collector
gh secret set ANTHROPIC_API_KEY    -R hyuckkk/us-alert-collector
```

## 6. 첫 배포 → Worker 비밀값 입력
1. GitHub → **Actions → deploy-worker → Run workflow**. D1 `us-alert-db` 와 Worker `us-alert-worker` 가 만들어집니다.
   (봇 토큰이 아직 없어서 마지막 "Register Telegram webhooks" 단계에 경고가 뜨는 것은 정상)
2. dash.cloudflare.com → **Workers & Pages → us-alert-worker → Settings → Variables and Secrets → Add**
   - Type 은 반드시 **Secret** (Text 로 넣으면 다음 배포 때 지워집니다)

| Variable name | Value |
|---|---|
| `BOT_TOKEN_LIFE` | @ColoradoLife_bot 토큰 (BotFather) |
| `BOT_TOKEN_FAMILY` | @ColoradoFamily_bot 토큰 |
| `BOT_TOKEN_KIDS` | @ColoradoKids_bot 토큰 |
| `OWNER_CHAT_ID` | 내 텔레그램 숫자 ID (한국 시스템의 `CHAT_ID` 와 같은 값) |
| `ADMIN_KEY` | 3단계 문자열 (GitHub `US_ADMIN_KEY` 와 같은 값) |

   → **Deploy** 누르기.

명령줄을 쓰면(값을 물어봄):
```bash
cd us-alert-collector/worker && npm ci
for s in BOT_TOKEN_LIFE BOT_TOKEN_FAMILY BOT_TOKEN_KIDS OWNER_CHAT_ID ADMIN_KEY; do npx wrangler secret put "$s"; done
```

> 숫자 ID 를 모르면: `OWNER_CHAT_ID` 만 비워 둔 채 7단계까지 진행하고 아무 봇에 `/myid` 를 보내면 ID 를 알려 줍니다. 그 값을 넣고 다시 Deploy.

## 7. 웹훅 등록 → 봇 시작 → 테스트
1. GitHub → **Actions → admin → Run workflow → action: `setup`** → 결과 요약에서 봇 3개 `ok: true` 확인
2. 텔레그램에서 세 봇을 각각 열고 **시작(/start)** — 텔레그램 규칙상 먼저 말을 걸어야 봇이 DM 을 보낼 수 있음.
   "소유자로 확인되었습니다" 답이 오면 정상
3. **Actions → admin → action: `test-send`** → 세 봇에서 테스트 알림 1건씩 도착
4. 버튼을 누르면 화면 위에 `✅ 확인 처리했습니다. (D1 기록 #1)` 처럼 **D1 기록 번호**가 뜨고 버튼에 ✓ 가 붙음
5. 기록 확인(셋 중 아무거나)
   - 봇에 `/clicks`
   - **Actions → admin → action: `clicks`**
   - Cloudflare → **Storage & Databases → D1 → us-alert-db → Console**: `SELECT * FROM clicks ORDER BY id DESC;`

## 8. 만료일 등록 (만료 2주 전 DM)
@ColoradoLife_bot 에 보냅니다. 이름은 자유, 날짜는 YYYY-MM-DD.
```
/expiry anthropic 2027-04-20 colorado-alerts 워크스페이스 키
/expiry cloudflare-token 2027-10-20      ← 2단계에서 TTL 을 정했다면
/expiry                                  ← 목록
/expiry del cloudflare-token             ← 삭제
```
매일 09:05(한국) 점검해서 **14·7·3·1·0일 전**에 한 번씩, 만료 후에는 매일 알립니다.
키를 바꾸면 5단계 위치에 새 값을 넣고 `/expiry anthropic <새 만료일>` 로 갱신합니다.

추가로 **Actions → key-check** 가 매일 키를 실제로 확인합니다(모델 목록 조회, 토큰 0). 키가 거부되면 즉시 DM, 점검 보고가 3일 넘게 끊겨도 DM.
설치 직후 한 번 **Actions → key-check → Run workflow** 를 눌러 봇에 `/status` 를 보내면 `API 키 점검: 정상` 이 보여야 합니다.

## 0단계 완료 기준
- [ ] 봇 3개 모두 `/start` → 소유자 안내 수신
- [ ] `test-send` → 세 봇에서 테스트 알림 수신
- [ ] 버튼 3종 클릭 → `D1 기록 #n` 표시, `/clicks` 에 기록
- [ ] `/expiry anthropic ...` 등록, `/status` 에 표시
- [ ] key-check 1회 성공, `/status` 에 `API 키 점검: 정상`
