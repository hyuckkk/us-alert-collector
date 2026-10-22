#!/usr/bin/env bash
# 개인정보 이력 정리(A안) 직전 검증 — push 하지 않는다.
# 작업 직전 최신 원격 브랜치를 새로 미러 복제해 재작성하고, 결과를 비공개 폴더에만 남긴다(9차 지시: 리허설 당시가 아니라 작업 직전 기준).
#
# 사용: PRIVACY_TERMS='...' bash scripts/history_rewrite_check.sh <비공개 치환규칙 파일> <비공개 출력 폴더>
#   - 치환규칙·출력 폴더는 저장소 밖(소유자 PC 또는 세션 임시 폴더). 출력 폴더에는 개인정보가 든 원본 미러가 있으므로 공개 금지.
#   - 옛↔새 커밋 대응표(commit-map)는 출력 폴더에만 생기며 공개 저장소에 올리지 않는다.
# 화면에는 개수·통과 여부만 출력한다(값·커밋 번호 대응 없음).
set -euo pipefail
RULES=${1:?private rules file}; OUT=${2:?private output dir}
REPO=${REPO:-https://github.com/hyuckkk/us-alert-collector.git}
BRANCHES=${BRANCHES:-"stage1-review stage1-structure stage2-fixes stage3-fixes stage4-preop"}
: "${PRIVACY_TERMS:?set PRIVACY_TERMS (same list as the GitHub secret) to verify}"
command -v git-filter-repo >/dev/null || { echo "git-filter-repo 필요 (pip install git-filter-repo==2.47.0)"; exit 2; }
case "$(cd "$(dirname "$RULES")" && pwd)/" in "$(git rev-parse --show-toplevel 2>/dev/null || echo /nonexistent)"/*) echo "치환규칙 파일이 저장소 안에 있음 — 중단"; exit 2;; esac
SCAN=$(cd "$(dirname "$0")" && pwd)/privacy_scan.py
mkdir -p "$OUT"; chmod 700 "$OUT"; cd "$OUT"
rm -rf orig.git new.git chk-orig chk-new
git clone -q --mirror "$REPO" orig.git
cp -r orig.git new.git
git -C orig.git bundle create ../backup-$(date -u +%Y%m%dT%H%M%SZ).bundle --all >/dev/null 2>&1   # 비공개 백업
refs=(); for b in $BRANCHES; do git -C orig.git rev-parse -q --verify "refs/heads/$b" >/dev/null && refs+=("$b"); done
( cd new.git && git-filter-repo --replace-text "$RULES" --prune-empty never --refs "${refs[@]}" ^main --force >/dev/null )
scan() { git clone -q "$1" "$2" && ( cd "$2" && git fetch -q origin '+refs/heads/*:refs/remotes/origin/*' && python3 "$SCAN" --history 2>/dev/null | grep -c '::error' || true ); }
before=$(scan orig.git chk-orig); after=$(scan new.git chk-new)
ok=1
echo "브랜치: ${refs[*]}"
echo "이력 개인정보 검사: 재작성 전 ${before}건 → 후 ${after}건"; [ "$after" = 0 ] || ok=0
[ "$(git -C orig.git rev-parse main)" = "$(git -C new.git rev-parse main)" ] && echo "main: 변경 없음" || { echo "main: 바뀜 — 중단 사유"; ok=0; }
for b in "${refs[@]}"; do
  a=$(git -C orig.git rev-list --count "$b"); n=$(git -C new.git rev-list --count "$b")
  files=$(git -C new.git diff --name-only "$(git -C orig.git rev-parse "$b^{tree}")" "$(git -C new.git rev-parse "$b^{tree}")" 2>/dev/null | wc -l)
  echo "$b: 커밋 $a → $n, 최신 파일 변경 ${files}개"; [ "$a" = "$n" ] || ok=0
done
echo "바뀐 커밋 수: $(awk 'NR>1 && $1!=$2' new.git/filter-repo/commit-map | wc -l) (대응표는 $OUT/new.git/filter-repo/commit-map — 비공개)"
awk 'NR>1 && $1!=$2 {print $1}' new.git/filter-repo/commit-map > old-commits.private.txt   # GitHub 지원 요청 첨부용(비공개)
[ "$ok" = 1 ] && echo "판정: 통과 — 이 결과로 강제 push 가능(별도 승인 필요)" || { echo "판정: 실패 — push 하지 말 것"; exit 1; }
