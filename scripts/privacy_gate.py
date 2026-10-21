"""개인정보 검사 관문. 실제 코드 오류와 '아직 끝나지 않은 개인정보 정리'를 구분한다.

python scripts/privacy_gate.py dev      # 개발 브랜치 push
python scripts/privacy_gate.py strict   # main 으로의 PR·main push·배포 전

| 결과                                   | dev                 | strict |
|----------------------------------------|---------------------|--------|
| 현재 파일에 비밀값·개인정보(검사 결과 1) | 실패                | 실패   |
| PRIVACY_TERMS 미설정(검사 결과 2)        | 경고 + '병합 불가'   | 실패   |
| 과거 이력에 개인정보(이력 검사 결과 1)    | 경고 + '병합 불가'   | 실패   |
| 모두 통과                                | 통과                | 통과   |

경고는 숨기지 않는다: 실행 주석(::warning)과 실행 요약(GITHUB_STEP_SUMMARY)에 '병합·배포 불가' 사유를 매번 남긴다.
검사어 값은 출력하지 않는다(privacy_scan.py 와 같음).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCAN = Path(__file__).resolve().parent / "privacy_scan.py"


def scan(*args: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(SCAN), *args], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def summary(lines: list[str]) -> None:
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "dev"
    if mode not in ("dev", "strict"):
        print("usage: privacy_gate.py dev|strict")
        return 2
    cur, cur_out = scan()
    hist, hist_out = scan("--history")
    # 현재 파일의 실제 노출은 어떤 모드에서도 실패. 검사 출력(파일·줄·몇 번째 항목)은 그대로 보여 준다.
    hard = cur == 1
    # 실제 노출(1)은 오류 그대로, 검사어 미설정(2)은 개발 브랜치에서 경고로 표시(내용은 같음)
    print(cur_out if hard or mode == "strict" else cur_out.replace("::error", "::warning"), end="")
    pending = []
    if cur == 2 or hist == 2:
        pending.append("PRIVACY_TERMS 미설정 — 주소·가족 정보 검사를 하지 못함")
    if hist == 1:
        pending.append("과거 커밋 이력에 개인정보 남음 — 이력 정리(A안) 전")
        # 이력 검사 상세는 개발 브랜치에서도 경고로 보여 준다(어느 커밋·파일인지)
        print(hist_out.replace("::error", "::warning" if mode == "dev" else "::error"), end="")
    if hard:
        verdict = "실패 — 현재 코드에 비밀값·개인정보"
    elif pending and mode == "strict":
        verdict = "실패 — 병합·배포 전 필수 조건 미충족"
    elif pending:
        verdict = "통과(경고) — 개발 브랜치. **main 병합·배포 불가**"
    else:
        verdict = "통과 — 병합·배포 조건 충족"
    for p in pending:
        print(f"::{'error' if mode == 'strict' else 'warning'} title=privacy-pending::{p} → main 병합·배포 불가")
    summary([f"### 개인정보 검사 ({mode})", "", "| 항목 | 결과 |", "|---|---|",
             f"| 현재 파일 | {'문제 있음' if cur == 1 else '검사어 없음(형식 검사만)' if cur == 2 else '통과'} |",
             f"| 과거 이력 | {'문제 있음' if hist == 1 else '검사어 없음' if hist == 2 else '통과'} |",
             f"| 판정 | {verdict} |"] + [f"- 남은 일: {p}" for p in pending])
    print(f"판정: {verdict}")
    return 1 if hard or (pending and mode == "strict") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
