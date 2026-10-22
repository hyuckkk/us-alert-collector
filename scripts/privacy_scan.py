"""공개 저장소 개인정보·비밀값 검사(1차 검증 9, 2차 재검증 3). CI 에서 모든 push 마다 실행.

1) 형식으로 알 수 있는 비밀값: 텔레그램 봇 토큰, Anthropic 키, GitHub 토큰, 개인 키 블록
2) 개인 정보: GitHub 비밀값 PRIVACY_TERMS(줄바꿈·쉼표 구분: 집 주소, 가족 이름, 텔레그램 ID 등)에 든 낱말.
   낱말 자체는 저장소·로그에 남기지 않고, 찾으면 파일·줄 번호(또는 커밋)와 '몇 번째 항목'만 출력한다.
3) 공개 실행 보고서(reports/*.json)에 텔레그램 chat_id 같은 수신자 정보가 없는지.

--history: 현재 파일뿐 아니라 모든 브랜치의 모든 커밋에 들어 있던 파일 내용까지 검사(과거 노출 이력).
종료 코드: 0 문제 없음 / 1 문제 발견 / 2 검사 불완전(PRIVACY_TERMS 미설정 — 개인정보 검사를 '통과'로 보지 않음)
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


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def scan_text(label: str, text: str, terms: list[str]) -> int:
    bad = 0
    low = text.lower()
    for i, line in enumerate(text.splitlines(), 1):
        for name, pat in PATTERNS.items():
            if pat.search(line):
                print(f"::error title=privacy::{label}:{i} {name} 형식의 값")
                bad += 1
    for n, t in enumerate(terms):
        if t in low:
            line = low[: low.index(t)].count("\n") + 1
            print(f"::error title=privacy::{label}:{line} 개인정보 목록의 {n + 1}번째 항목")
            bad += 1
    if "reports/" in label and re.search(r'"chat_id"\s*:', text):
        print(f"::error title=privacy::{label} 공개 보고서에 chat_id")
        bad += 1
    return bad


def main() -> int:
    history = "--history" in sys.argv
    terms = [t.strip().lower() for t in re.split(r"[\n,]", os.environ.get("PRIVACY_TERMS", "")) if len(t.strip()) >= 4]
    bad = 0
    if history:
        # 모든 커밋의 모든 파일 내용(blob)을 한 번씩 검사. 같은 내용은 한 번만.
        seen = set()
        blobs = 0
        for commit in git("rev-list", "--all").split():
            for row in git("ls-tree", "-r", commit).splitlines():
                meta, path = row.split("\t", 1)
                sha = meta.split()[2]
                if sha in seen or path in SKIP:
                    continue
                seen.add(sha)
                try:
                    text = git("cat-file", "-p", sha)
                except (subprocess.CalledProcessError, UnicodeDecodeError):
                    continue
                blobs += 1
                bad += scan_text(f"{commit[:7]}:{path}", text, terms)
        print(f"이력 검사: 커밋 {len(git('rev-list', '--all').split())}개, 파일 내용 {blobs}개, 개인정보 목록 {len(terms)}개, 문제 {bad}건")
    else:
        files = [f for f in git("ls-files").split() if f not in SKIP]
        for f in files:
            try:
                text = open(f, encoding="utf-8").read()
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                continue
            bad += scan_text(f, text, terms)
        print(f"검사 파일 {len(files)}개, 개인정보 목록 {len(terms)}개, 문제 {bad}건")
    if bad:
        return 1
    if not terms:
        print("::error title=privacy::PRIVACY_TERMS 비밀값이 비어 있어 개인정보(주소·가족 정보) 검사를 하지 못했습니다. 비밀값 형식 검사만 통과 — 완전한 통과가 아닙니다.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
