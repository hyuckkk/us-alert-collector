"""개인정보 검사 관문(privacy_gate.py) 시험: 개발 브랜치 경고와 실제 오류 구분, 엄격 모드 필수 조건."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE_TOKEN = "123456789:AA" + "x" * 33          # 텔레그램 토큰 형식(가짜)


def repo(tmp: Path, files_history: dict, files_now: dict) -> Path:
    r = tmp / "r"
    (r / "scripts").mkdir(parents=True)
    for f in ("privacy_scan.py", "privacy_gate.py"):
        shutil.copy(ROOT / "scripts" / f, r / "scripts" / f)
    g = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)  # noqa: E731
    g("init", "-q"); g("config", "user.email", "t@example.com"); g("config", "user.name", "t")
    for name, text in files_history.items():
        (r / name).write_text(text)
    g("add", "-A"); g("commit", "-qm", "one")
    for name, text in files_now.items():
        (r / name).write_text(text)
    g("add", "-A"); g("commit", "-qm", "two", "--allow-empty")
    return r


def gate(r: Path, mode: str, terms: str | None, summary: Path) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k != "PRIVACY_TERMS"}
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    if terms is not None:
        env["PRIVACY_TERMS"] = terms
    p = subprocess.run([sys.executable, "scripts/privacy_gate.py", mode], cwd=r, env=env, capture_output=True, text=True)
    return p.returncode, p.stdout


@pytest.mark.parametrize("mode,code", [("dev", 0), ("strict", 1)])
def test_missing_terms_is_warning_on_dev_and_failure_on_strict(tmp_path, mode, code):
    r = repo(tmp_path, {"a.txt": "hello"}, {})
    rc, out = gate(r, mode, None, tmp_path / "s.md")
    assert rc == code
    assert "PRIVACY_TERMS 미설정" in out and "병합·배포 불가" in out          # 숨기지 않음
    assert ("::warning" if mode == "dev" else "::error") in out
    assert "병합" in (tmp_path / "s.md").read_text()


@pytest.mark.parametrize("mode", ["dev", "strict"])
def test_secret_in_current_files_fails_in_every_mode(tmp_path, mode):
    r = repo(tmp_path, {"a.txt": "hello"}, {"b.txt": f"token={FAKE_TOKEN}"})
    rc, out = gate(r, mode, "unrelated term", tmp_path / "s.md")
    assert rc == 1 and "telegram-bot-token" in out


@pytest.mark.parametrize("mode,code", [("dev", 0), ("strict", 1)])
def test_history_only_exposure_is_warning_on_dev_and_failure_on_strict(tmp_path, mode, code):
    r = repo(tmp_path, {"a.txt": "my street 999 elmwood"}, {"a.txt": "redacted"})
    rc, out = gate(r, mode, "999 elmwood", tmp_path / "s.md")
    assert rc == code
    assert "과거 커밋 이력" in out
    assert "999 elmwood" not in out                                           # 검사어 값은 출력하지 않음


def test_all_clean_passes_strict(tmp_path):
    r = repo(tmp_path, {"a.txt": "hello"}, {"b.txt": "world"})
    rc, out = gate(r, "strict", "999 elmwood", tmp_path / "s.md")
    assert rc == 0 and "병합·배포 조건 충족" in out
