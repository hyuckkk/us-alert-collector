"""정보원 확충 후보 시험(토큰 0): eval/source_candidates.json 의 후보를 실제로 조회해 행사·프로그램이 나오는지 본다.
결과: eval/source_candidates_result.json (항목 수, 날짜·신청 문장이 있는 항목 비율, 예시 제목)."""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import RELEVANT, fetch_source  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EVENT_WORDS = re.compile(r"event|festival|camp|class|program|regist|concert|workshop|story|tour|lesson|league|market|show|night|day|walk|hike|fair|parade|exhibit", re.I)


def check(c: dict) -> dict:
    r = fetch_source(c)
    items = r.entries
    eventish = sum(1 for e in items if EVENT_WORDS.search(e.title) or RELEVANT.search(e.title + " " + (e.text or "")[:300]))
    return {**c, "ok": r.ok, "status": r.status, "error": r.error, "items": len(items), "eventish": eventish,
            "sample": [e.title[:80] for e in items[:6]], "sample_urls": [e.url for e in items[:3]]}


def main() -> int:
    cands = json.loads((ROOT / "eval/source_candidates.json").read_text())["candidates"]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(check, cands))
    good = [r for r in res if r["ok"] and r["items"] >= 3 and r["eventish"] >= 2]
    (ROOT / "eval/source_candidates_result.json").write_text(json.dumps({"tested": len(res), "good": len(good), "results": res}, ensure_ascii=False, indent=1) + "\n")
    for r in res:
        print(f"{'GOOD' if r in good else ('OK  ' if r['ok'] else 'ERR ')} {r['items']:4d} {r['eventish']:4d} {r['id']} {r['error'] or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
