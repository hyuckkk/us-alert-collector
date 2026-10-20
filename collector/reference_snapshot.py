"""기준 목록 후보 만들기(1차 검증 10, 2차 재검증 5). AI 없이 공식 게시판 패널(eval/reference_panel.json)을 읽어
eval/reference_candidates.json 을 만든다. 이 후보는 '검증 전'이다 — 사람이 원문을 열어 확인한 것만 ground_truth.json 으로 옮긴다."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import fetch_source  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PER_SOURCE = 15


def main() -> int:
    panel = json.loads((ROOT / "eval/reference_panel.json").read_text())["sources"]
    out = {"_comment": "검증 전 후보. 사람이 공식 원문을 확인한 항목만 ground_truth.json 으로 옮긴다(날짜·장소·비용도 원문 그대로).",
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": [], "candidates": []}
    for s in panel:
        r = fetch_source(s)
        out["sources"].append({"id": s["id"], "monitored": s["monitored"], "ok": r.ok, "status": r.status, "items": len(r.entries), "error": r.error})
        for e in r.entries[:PER_SOURCE]:
            out["candidates"].append({"id": f"{s['id']}:{len(out['candidates'])}", "panel_source": s["id"], "monitored_source": s["monitored"],
                                      "title": e.title, "url": e.url, "date_raw": e.published, "verified": False})
        print(f"{'OK ' if r.ok else 'ERR'} {r.status} {len(r.entries):4d} {s['id']} {r.error or ''}")
    (ROOT / "eval/reference_candidates.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print(f"후보 {len(out['candidates'])}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
