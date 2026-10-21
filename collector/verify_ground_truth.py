"""기준 목록 원문 확인 자료 만들기(7차: 실제 행사 발견율 검증). AI 없음(토큰 0), Worker·텔레그램에 아무것도 쓰지 않음.

1) eval/ground_truth_draft.json 의 각 행사 원문 페이지를 열어, 날짜·시간·장소·비용·연령·신청 관련 문장만 뽑는다.
2) 수집기가 감시하는 정보원(config/sources.json, 사용 중인 것)의 목록을 지금 읽어, 각 행사 URL 이 그 목록에 실제로 나오는지 본다(정보원 단계 발견 여부).
3) 수집기가 감시하지 않는 공식 정보원 몇 곳(EXTRA)의 목록에서 후보를 더 뽑아 같은 방식으로 원문 문장을 모은다.
결과: eval/ground_truth_pages.json — 공개 웹 페이지의 공개 행사 정보만 담는다. 사람이(또는 검토자가) 이 문장을 근거로 유효 여부·필드를 확정한다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import fetch_source, fingerprint, new_client, normalize_url, page_text  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXTRA = [  # 수집기가 감시하지 않는 공식 정보원(시·카운티·연방 기관)
    {"id": "northglenn-calendar", "type": "rss", "url": "https://www.northglenn.org/RSSFeed.aspx?ModID=58&CID=All-calendar.xml"},
    {"id": "federal-heights-calendar", "type": "rss", "url": "https://www.fedheights.org/RSSFeed.aspx?ModID=58&CID=All-calendar.xml"},
    {"id": "adams-county-calendar", "type": "rss", "url": "https://adcogov.org/RSSFeed.aspx?ModID=58&CID=All-calendar.xml"},
    {"id": "thornton-festivals", "type": "html", "url": "https://www.thorntonco.gov/community-culture/festivals-events"},
    {"id": "rm-arsenal-refuge", "type": "html", "url": "https://www.fws.gov/refuge/rocky-mountain-arsenal/events"},
]
PER_EXTRA = 6


def facts(client, url: str, title: str) -> dict:
    try:
        status, text = page_text(client, url)
    except Exception as e:  # noqa: BLE001
        return {"status": None, "error": type(e).__name__}
    if not text:
        return {"status": status, "error": f"HTTP {status}"}
    i = text.lower().find(title.lower()[:30]) if title else -1
    body = text[i if i > 0 else 0:]
    return {"status": status, "title_found": i >= 0, "head": body[:1200], "relevant": fingerprint(body[:8000])[1][:1800]}


def main() -> int:
    draft = json.loads((ROOT / "eval/ground_truth_draft.json").read_text())["events"]
    sources = [s for s in json.loads((ROOT / "config/sources.json").read_text())["sources"]
               if s.get("enabled", True) and s["type"] != "gnews" and not s.get("requires_secret")]
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "monitored_sources": [], "events": [], "extra_sources": [], "extra_events": []}
    listed: dict[str, list[str]] = {}
    for s in sources:
        r = fetch_source(s)
        out["monitored_sources"].append({"id": s["id"], "ok": r.ok, "items": len(r.entries), "error": r.error})
        for e in r.entries:
            listed.setdefault(normalize_url(e.url), []).append(s["id"])
        print(f"{'OK ' if r.ok else 'ERR'} {len(r.entries):4d} {s['id']}")
    with new_client() as c:
        for e in draft:
            f = facts(c, e["url"], e["title"])
            out["events"].append({"id": e["id"], "panel_source": e["panel_source"], "monitored_source": e["monitored_source"], "title": e["title"],
                                  "url": e["url"], "listed_by": listed.get(normalize_url(e["url"]), []), **f})
            print(f"{f.get('status')} {e['id']}")
        for s in EXTRA:
            r = fetch_source(s)
            out["extra_sources"].append({"id": s["id"], "url": s["url"], "ok": r.ok, "items": len(r.entries), "error": r.error})
            for x in r.entries[:PER_EXTRA]:
                f = facts(c, x.url, x.title)
                out["extra_events"].append({"source": s["id"], "title": x.title, "url": x.url, "date_raw": x.published,
                                            "listed_by": listed.get(normalize_url(x.url), []), **f})
            print(f"{'OK ' if r.ok else 'ERR'} {len(r.entries):4d} extra {s['id']} {r.error or ''}")
    (ROOT / "eval/ground_truth_pages.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
