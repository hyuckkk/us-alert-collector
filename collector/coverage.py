"""수집 범위 비교(9차: 정보원 보강 전후). AI 없음(토큰 0), Worker·텔레그램에 아무것도 쓰지 않음.

기준 목록(eval/ground_truth.json)의 각 행사가 다음 무료 단계에서 잡히는지 본다.
  1) 등록 정보원 목록 — 보강 전(NEW_SOURCES 제외) / 보강 후(전체). URL 일치.
  2) 키워드 검색(Google News RSS, config/queries.json 의 매일 + 요일 심층 전체) — 제목 유사도(뉴스 링크는 원문 URL 이 아니라서).
AI 발굴·AI 판정은 유료라 여기서 재지 않는다(전체 시스템 발견율은 collector/eval_stages.py + 건식 실행으로 별도 측정).
결과: eval/coverage.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import identity  # noqa: E402
from fetch import fetch_source, normalize_url  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NEW_SOURCES = {"thornton-festivals", "thornton-registration", "dam-free-days", "broomfield-camp-explorer"}


def title_match(truth: str, cand: str) -> bool:
    a, b = identity.tokens(truth), identity.tokens(cand)
    if len(a) < 2 or not b:
        return False
    return identity.jaccard(a, b) >= 0.5 or len(a & b) / len(a) >= 0.8


def main() -> int:
    gt = json.loads((ROOT / "eval/ground_truth.json").read_text())
    events = gt["events"]
    sources = [s for s in json.loads((ROOT / "config/sources.json").read_text())["sources"]
               if s.get("enabled", True) and s["type"] != "gnews" and not s.get("requires_secret")]
    listed: dict[str, set[str]] = {}
    src_rows = []
    for s in sources:
        r = fetch_source(s)
        src_rows.append({"id": s["id"], "new": s["id"] in NEW_SOURCES, "ok": r.ok, "items": len(r.entries), "error": r.error})
        for e in r.entries:
            listed.setdefault(normalize_url(e.url), set()).add(s["id"])
        print(f"{'OK ' if r.ok else 'ERR'} {len(r.entries):4d} {s['id']}{' (new)' if s['id'] in NEW_SOURCES else ''}")
    q = json.loads((ROOT / "config/queries.json").read_text())
    queries = [s for lst in q["daily"].values() for s in lst] + [s for lst in q.get("weekday", {}).values() for s in lst]
    queries = list(dict.fromkeys(queries))
    news = []
    for query in queries:
        r = fetch_source({"id": "gnews:" + query[:40], "type": "gnews", "query": query})
        news += [{"query": query, "title": e.title} for e in r.entries[:15]]
    rows = []
    for ev in events:
        u = normalize_url(ev["url"])
        ids = listed.get(u, set())
        hits = [n for n in news if title_match(ev["title"], n["title"])]
        rows.append({"id": ev["id"], "title": ev["title"], "participation": ev.get("participation"), "age_limit": ev.get("age_limit"),
                     "sources_before": sorted(ids - NEW_SOURCES), "sources_after": sorted(ids),
                     "keyword_search_hits": len(hits), "keyword_search_example": hits[0] if hits else None})
    n = len(rows)
    count = lambda f: sum(1 for r in rows if f(r))  # noqa: E731
    summary = {
        "events": n,
        "sources_before": count(lambda r: r["sources_before"]),
        "sources_after": count(lambda r: r["sources_after"]),
        "keyword_search": count(lambda r: r["keyword_search_hits"]),
        "sources_before_or_search": count(lambda r: r["sources_before"] or r["keyword_search_hits"]),
        "sources_after_or_search": count(lambda r: r["sources_after"] or r["keyword_search_hits"]),
        "listing_items_before": sum(x["items"] for x in src_rows if not x["new"]),
        "listing_items_after": sum(x["items"] for x in src_rows),
        "sources_ok_before": f"{sum(x['ok'] for x in src_rows if not x['new'])}/{sum(1 for x in src_rows if not x['new'])}",
        "sources_ok_after": f"{sum(x['ok'] for x in src_rows)}/{len(src_rows)}",
        "queries": len(queries), "news_items": len(news),
        "not_measured": "AI 신규 발굴·AI 추출·판정·알림 대상(유료) — 건식 실행으로 별도 측정",
    }
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "summary": summary, "rows": rows, "sources": src_rows}
    (ROOT / "eval/coverage.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
