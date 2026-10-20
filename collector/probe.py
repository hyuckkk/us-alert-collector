"""정보원 시험 조회(probe): config/sources.json 의 각 주소가 GitHub Actions 에서 열리는지 확인한다.

결과는 reports/probe.json 에 남긴다(공개 정보만: 상태 코드, 형식, 크기, 앞부분 일부).
설계안 '구현 전 기술적 위험'의 첫 항목(미국 사이트가 Actions 의 자동 조회를 막는지) 확인용.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import USER_AGENT, fetch_source  # noqa: E402


def main() -> int:
    sources = json.loads((ROOT / "config/sources.json").read_text())["sources"]
    queries = json.loads((ROOT / "config/queries.json").read_text())
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user_agent": USER_AGENT, "sources": [], "gnews": None}
    for s in sources:
        t0 = time.time()
        r = fetch_source(s, secrets=os.environ, probe=True)
        out["sources"].append({
            "id": s["id"], "type": s["type"], "url": s["url"],
            "ok": r.ok, "status": r.status, "content_type": r.content_type, "bytes": r.size,
            "items": len(r.entries), "error": r.error, "seconds": round(time.time() - t0, 1),
            "sample_titles": [e.title for e in r.entries[:3]],
            "snippet": r.snippet,
        })
        print(f"{'OK ' if r.ok else 'ERR'} {r.status} {len(r.entries):3d} {s['id']} {r.error or ''}")
    g = fetch_source({"id": "gnews", "type": "gnews", "query": queries["daily"][0]}, probe=True)
    out["gnews"] = {"ok": g.ok, "status": g.status, "items": len(g.entries), "error": g.error, "sample_titles": [e.title for e in g.entries[:3]]}
    print(f"gnews {g.status} {len(g.entries)} {g.error or ''}")
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports/probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
