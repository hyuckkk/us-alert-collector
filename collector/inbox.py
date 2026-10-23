"""외부 입력 파일 처리(이슈 #20): 외부(ChatGPT·Claude)가 저장소에 새로 만든 external-inbox/<finder>/<고유이름>.json 을 받아 external_ingest 로 넘긴다.

- 이번 push 에서 '새로 추가된' 파일만 받는다(수정·삭제는 무시). 목록은 워크플로가 git diff --diff-filter=A 로 만들어 INBOX_PATHS 로 넘긴다.
- 경로 형식·크기(200KB)·한 번에 파일 수(10)·파일당 행사 수(30, external_ingest)를 제한한다. 파일 내용은 Python 에서만 읽고 명령에 끼워 넣지 않는다.
- 같은 경로·같은 내용(sha256)은 Worker 의 inbox_files 기록으로 한 번만 처리한다(워크플로 재실행·중복 push 에도 다시 보내지 않음).
- 발견 경로(finder)는 폴더 이름으로 정한다: external-inbox/chatgpt/… → chatgpt, external-inbox/claude/… → claude.
  python collector/inbox.py            (INBOX_PATHS 환경 변수: 줄마다 경로 하나)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
import external_ingest as xi  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PATH_RE = re.compile(r"^external-inbox/(chatgpt|claude)/([A-Za-z0-9._-]{1,100})\.json$")
MAX_BYTES = 200_000
MAX_FILES = 10


def process(paths: list[str], api: WorkerAPI, client, models: dict, today: str, root: Path = ROOT) -> list[dict]:
    out = []
    for p in paths[:MAX_FILES]:
        m = PATH_RE.match(p)
        if not m or ".." in p:
            out.append({"path": p[:120], "status": "rejected", "reason": "경로 형식"})
            continue
        f = root / p
        if not f.is_file() or f.is_symlink():
            out.append({"path": p, "status": "rejected", "reason": "파일 없음"})
            continue
        data = f.read_bytes()
        if len(data) > MAX_BYTES:
            out.append({"path": p, "status": "rejected", "reason": f"크기 초과({len(data)} > {MAX_BYTES})"})
            continue
        sha = hashlib.sha256(data).hexdigest()
        finder, stem = m.group(1), m.group(2)
        claim = api.inbox("claim", p, sha)
        if not claim.get("claimed"):
            out.append({"path": p, "status": "skipped", "reason": f"이미 처리됨({claim.get('status')})"})
            continue
        try:
            raw = json.loads(data.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("최상위가 객체가 아님")
            rep = xi.ingest(raw, api, client, models, today, finder=finder, run_id=f"{finder}-{stem}"[:80])
            api.inbox("done", p, sha, xi.shown(rep))
            out.append({"path": p, "status": "done", "report": xi.shown(rep)})
        except Exception as e:  # noqa: BLE001  한 파일의 오류가 다른 파일을 막지 않음. 실패로 기록 → 같은 파일을 다시 처리할 수 있음
            api.inbox("done", p, sha, {"error": f"{type(e).__name__}: {str(e)[:200]}"}, failed=True)
            out.append({"path": p, "status": "failed", "reason": f"{type(e).__name__}: {str(e)[:200]}"})
    for p in paths[MAX_FILES:]:
        out.append({"path": p[:120], "status": "rejected", "reason": f"한 번에 {MAX_FILES}개까지"})
    return out


def main() -> int:
    paths = [x.strip() for x in os.environ.get("INBOX_PATHS", "").splitlines() if x.strip()]
    if not paths:
        print("::notice title=external inbox::새 입력 파일 없음")
        return 0
    models = json.loads((ROOT / "config/models.json").read_text())
    today = datetime.now(ZoneInfo("America/Denver")).date().isoformat()
    res = process(paths, WorkerAPI.from_env(), ai.get_client(), models, today)
    for r in res:  # 주소·키·비밀값은 출력하지 않는다(건수·상태만)
        print("::notice title=external inbox::" + json.dumps(r, ensure_ascii=False)[:3800])
    return 1 if any(r["status"] == "failed" for r in res) else 0


if __name__ == "__main__":
    sys.exit(main())
