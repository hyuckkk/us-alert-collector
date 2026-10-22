#!/usr/bin/env bash
# GitHub 지원 처리 결과 확인(9차 지시: 캐시·과거 커밋 삭제를 가정하지 말고 실제로 확인).
# 사용: bash scripts/check_old_commits.sh <비공개 옛 커밋 목록 파일>   (history_rewrite_check.sh 가 만든 old-commits.private.txt)
# 로그인하지 않은 상태로 세 경로를 시험하고 '아직 열리는 커밋 수'만 출력한다(커밋 번호는 화면에 내지 않음).
#   1) 웹 페이지 https://github.com/<repo>/commit/<sha>       — 404 여야 함
#   2) API     https://api.github.com/repos/<repo>/commits/<sha> — 404/422 여야 함(시간당 60회 제한 → 표본)
#   3) git     새 클론에서 git fetch origin <sha>               — 실패해야 함
set -uo pipefail
LIST=${1:?private list of old commit SHAs}; REPO=${REPO:-hyuckkk/us-alert-collector}; SAMPLE=${SAMPLE:-40}
mapfile -t SHAS < <(grep -E '^[0-9a-f]{40}$' "$LIST")
total=${#SHAS[@]}; [ "$total" -gt 0 ] || { echo "목록이 비어 있음"; exit 2; }
web=0; api=0; git_ok=0; n=0
tmp=$(mktemp -d); git clone -q --bare "https://github.com/$REPO.git" "$tmp/r.git"
for s in "${SHAS[@]}"; do
  c=$(curl -s -o /dev/null -w '%{http_code}' "https://github.com/$REPO/commit/$s"); [ "$c" = 200 ] && web=$((web + 1))
  if [ "$n" -lt "$SAMPLE" ]; then
    c=$(curl -s -o /dev/null -w '%{http_code}' "https://api.github.com/repos/$REPO/commits/$s"); [ "$c" = 200 ] && api=$((api + 1))
    git -C "$tmp/r.git" fetch -q origin "$s" 2>/dev/null && git_ok=$((git_ok + 1))
  fi
  n=$((n + 1))
done
rm -rf "$tmp"
s=$((total < SAMPLE ? total : SAMPLE))
echo "확인 시각: $(date -u +%FT%TZ) · 옛 커밋 ${total}개"
echo "웹 페이지로 아직 열림: ${web}/${total}"
echo "API 로 아직 조회됨(표본): ${api}/${s}"
echo "git fetch 로 아직 받아짐(표본): ${git_ok}/${s}"
[ $((web + api + git_ok)) -eq 0 ] && echo "판정: 옛 커밋 접근 불가 확인" || { echo "판정: 아직 접근 가능 — 지원 요청 후속 필요"; exit 1; }
