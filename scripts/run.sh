#!/usr/bin/env bash
# 명령을 실행하고, 실패하면 출력 끝부분을 GitHub 주석(::error)으로 남긴다. 실행 기록 화면과 API 에서 바로 보인다.
set -uo pipefail
out=$(mktemp)
"$@" 2>&1 | tee "$out"
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
  tail -n 25 "$out" | sed 's/\x1b\[[0-9;]*m//g' | grep -v '^\s*$' | tail -n 12 | while IFS= read -r line; do echo "::error title=$1 $2::${line:0:300}"; done
fi
rm -f "$out"
exit "$rc"
