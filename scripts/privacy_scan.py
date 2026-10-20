"""공개 저장소 개인정보·비밀값 검사(1차 검증 9). CI 에서 모든 push 마다 실행.

1) 형식으로 알 수 있는 비밀값: 텔레그램 봇 토큰, Anthropic 키, GitHub 토큰, 개인 키 블록
2) 개인 정보: GitHub 비밀값 PRIVACY_TERMS(줄바꿈·쉼표 구분: 집 주소, 아이 이름, 텔레그램 ID 등)에 든 낱말.
   낱말 자체는 저장소에 두지 않고, 찾으면 파일·줄 번호만 출력한다(낱말은 출력하지 않음).
3) 공개 실행 보고서(reports/*.json)에 텔레그램 chat_id 같은 수신자 정보가 없는지.
"""
import os
import re
import subprocess
import sys

PATTERNS = {
    "telegram-bot-token": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b"),
    "anthropic-key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SKIP = ("worker/package-lock.json",)


def main() -> int:
    files = [f for f in subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split() if f not in SKIP]
    terms = [t.strip().lower() for t in re.split(r"[\n,]", os.environ.get("PRIVACY_TERMS", "")) if len(t.strip()) >= 4]
    bad = 0
    for f in files:
        try:
            text = open(f, encoding="utf-8").read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        low = text.lower()
        for i, line in enumerate(text.splitlines(), 1):
            for name, pat in PATTERNS.items():
                if pat.search(line):
                    print(f"::error file={f},line={i}::{name} 형식의 값이 있습니다")
                    bad += 1
        for n, t in enumerate(terms):
            if t in low:
                line = low[: low.index(t)].count("\n") + 1
                print(f"::error file={f},line={line}::개인정보 목록의 {n + 1}번째 항목이 들어 있습니다")
                bad += 1
        if f.startswith("reports/") and re.search(r'"chat_id"\s*:', text):
            print(f"::error file={f}::공개 보고서에 chat_id 가 있습니다")
            bad += 1
    print(f"검사 파일 {len(files)}개, 개인정보 목록 {len(terms)}개, 문제 {bad}건")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
