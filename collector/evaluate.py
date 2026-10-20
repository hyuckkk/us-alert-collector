"""실제 발견율 평가(1차 검증 10): 사람이 확인한 기준 목록(eval/ground_truth.json)과 시스템이 찾은 행사(Worker 행사 목록)를 비교한다.

python collector/evaluate.py [--events reports/dry-run.json] [--out reports/eval.json]
  --events 를 주면 그 보고서의 events 를, 없으면 Worker /admin/event-index 를 쓴다(US_WORKER_URL·US_ADMIN_KEY 필요).
지표: 발견율(recall), 발견 지연(공고 게시 → 첫 수집), 필드 정확도(날짜·장소·비용), 기관별 누락 목록.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import identity  # noqa: E402
from fetch import normalize_url  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def match(truth: dict, found: list[dict]) -> dict | None:
    tu = normalize_url(truth["url"]) if truth.get("url") else None
    for f in found:
        if tu and f.get("url") and normalize_url(f["url"]) == tu:
            return f
    return next((f for f in found if identity.same_event(truth, f)), None)


def evaluate(truth: list[dict], found: list[dict]) -> dict:
    rows, by_org = [], {}
    for t in truth:
        f = match(t, found)
        row = {"id": t["id"], "org": t.get("org"), "title": t["title"], "found": bool(f)}
        if f:
            row["event_key"] = f.get("event_key")
            for k in ("start_date", "location", "cost_text"):
                if t.get(k) is not None:
                    row[f"{k}_ok"] = (str(f.get(k) or "").strip().lower() == str(t[k]).strip().lower())
            if t.get("posted_at") and f.get("first_seen"):
                row["latency_days"] = (date.fromisoformat(f["first_seen"][:10]) - date.fromisoformat(t["posted_at"][:10])).days
        rows.append(row)
        o = by_org.setdefault(t.get("org") or "?", {"total": 0, "found": 0})
        o["total"] += 1
        o["found"] += int(bool(f))
    n = len(rows)
    found_rows = [r for r in rows if r["found"]]
    acc = {k: (sum(r[f"{k}_ok"] for r in found_rows if f"{k}_ok" in r), sum(1 for r in found_rows if f"{k}_ok" in r)) for k in ("start_date", "location", "cost_text")}
    lat = [r["latency_days"] for r in found_rows if "latency_days" in r]
    return {
        "truth": n, "found": len(found_rows), "recall": round(len(found_rows) / n, 3) if n else None,
        "accuracy": {k: (f"{a}/{b}" if b else "n/a") for k, (a, b) in acc.items()},
        "latency_days_median": sorted(lat)[len(lat) // 2] if lat else None,
        "by_org": by_org, "missed": [r for r in rows if not r["found"]], "rows": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    truth = [e for e in json.loads((ROOT / "eval/ground_truth.json").read_text())["events"] if not e.get("example")]
    if a.events:
        found = json.loads(Path(a.events).read_text())["events"]
    else:
        from worker_api import WorkerAPI
        found = WorkerAPI.from_env().event_index()
    res = evaluate(truth, found)
    if a.out:
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
