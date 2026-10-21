"""AI 입력 확인(토큰 0): 기준 행사마다 수집기가 실제로 AI 에 넘길 글을 공식 페이지에서 만들고,
원문에 있는 날짜·비용·접수일이 그 글에 들어 있는지 본다(AI 결과가 틀릴 때 입력 문제인지 AI 문제인지 가르기 위함).
결과: eval/ai_inputs.json"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run  # noqa: E402
from fetch import Entry, fetch_detail, fetch_source, new_client, normalize_url  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def date_tokens(ymd: str | None) -> list[str]:
    if not ymd:
        return []
    d = date.fromisoformat(ymd)
    m = MONTHS[d.month - 1]
    return [f"{m} {d.day}", f"{m[:3]} {d.day}", f"{m[:3]}. {d.day}", f"{d.month}/{d.day}"]


def has_any(text: str, toks: list[str]) -> bool | None:
    if not toks:
        return None
    low = text.lower()
    return any(re.search(r"\b" + re.escape(t.lower()) + r"\b", low) for t in toks)


def main() -> int:
    gt = json.loads((ROOT / "eval/ground_truth.json").read_text())
    sources = {s["id"]: s for s in json.loads((ROOT / "config/sources.json").read_text())["sources"]}
    feeds: dict[str, list[Entry]] = {}
    rows = []
    with new_client() as hc:
        for t in gt["events"]:
            sid = (t.get("listed_by_monitored_now") or [None])[0] or t.get("panel_source")
            src = sources.get(sid)
            if not src:
                rows.append({"id": t["id"], "source": sid, "path": "not-registered"})
                continue
            if src.get("mode") == "page-change" or src["type"] in ("rss", "ical", "json"):
                if sid not in feeds:
                    feeds[sid] = fetch_source(src).entries
                match = [e for e in feeds[sid] if normalize_url(e.url) == normalize_url(t["url"])]
                e = match[0] if match else Entry(sid, t["url"], t["title"])
                if src["type"] in ("rss", "ical", "json") and len(e.text or "") < run.RSS_DETAIL_MIN_CHARS:
                    e.extra.update(needs_detail=True, feed_short=True)
                    run.Run._detail(e, hc)
                path = "page-change" if src.get("mode") == "page-change" else f"{src['type']}+detail" if e.extra.get("feed_short") else src["type"]
            else:
                e = Entry(sid, t["url"], t["title"])
                fetch_detail(e, hc)
                path = "html-detail"
            text = (e.text or "")[:4500]
            truth_date = t.get("start_date")
            cost = t.get("cost_text") or ""
            cost_toks = re.findall(r"\$\s?\d+", cost) + (["free"] if "무료" in cost else [])
            rows.append({"id": t["id"], "source": sid, "path": path, "chars": len(text),
                         "date_in_input": has_any(text, date_tokens(truth_date)) if t.get("date_confirmed") else None,
                         "cost_in_input": has_any(text, cost_toks), "head": text[:200]})
    judged = [r for r in rows if r.get("path") != "not-registered"]
    summary = {"events": len(rows), "registered_path": len(judged),
               "date_in_input": f"{sum(1 for r in judged if r.get('date_in_input'))}/{sum(1 for r in judged if r.get('date_in_input') is not None)}",
               "cost_in_input": f"{sum(1 for r in judged if r.get('cost_in_input'))}/{sum(1 for r in judged if r.get('cost_in_input') is not None)}"}
    (ROOT / "eval/ai_inputs.json").write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
