"""전체 수집 성능 평가(9차): 기준 행사별로 어느 단계에서 발견됐는지, 날짜·신청조건이 맞는지, 최종 알림 대상이 되는지.

입력: eval/ground_truth.json, run.py --dry-run --trace 가 만든 기록 파일.
  python collector/run.py --mode full --dry-run --max 1500 --out reports/run.json --trace reports/trace.json   (유료: AI 발굴·추출·판정)
  python collector/eval_stages.py reports/trace.json --out eval/stages.json [--owner-from YYYY-MM-DD]
단계: sources(등록 정보원) · search(키워드 검색) · discover(AI 신규 발굴) → ai(추출·판정) → event(최종 행사) → notify(알림 대상).
일치 판정: 원문 URL 이 같거나, 제목 유사도(뉴스·발굴 결과처럼 URL 이 다른 경우). 알림 대상은 Worker 규칙(지난 행사 제외, 참가 가능 시작일,
연례 예고)을 단순화해 계산하며, 기준선(첫날 조용히 등록)은 반영하지 않는다(운영 첫날 이후 기준).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import identity  # noqa: E402
from fetch import normalize_url  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STAGES = ("sources", "search", "discover")


def title_match(truth: str, cand: str) -> bool:
    a, b = identity.tokens(truth), identity.tokens(cand)
    if len(a) < 2 or not b:
        return False
    return identity.jaccard(a, b) >= 0.5 or len(a & b) / len(a) >= 0.8


def same(truth: dict, url: str | None, title: str | None) -> bool:
    return bool((url and normalize_url(url) == normalize_url(truth["url"])) or (title and title_match(truth["title"], title)))


def cmp_date(truth: str | None, got: str | None) -> str:
    if not truth:
        return "기준 미확인"
    if not got:
        return "누락"
    return "일치" if got[:10] == truth[:10] else f"불일치({got[:10]})"


def cmp_cost(truth: str | None, got: str | None) -> str:
    if not truth or truth.startswith("미확인"):
        return "기준 미확인"
    if not got:
        return "누락"
    t_free, g_free = "무료" in truth, bool(re.search(r"free|무료|\$0\b", got, re.I))
    if t_free != g_free:
        return f"불일치({got[:40]})"
    t_nums, g_nums = set(re.findall(r"\$\s?(\d+)", truth)), set(re.findall(r"\$\s?(\d+)", got))
    return "일치" if not t_nums or t_nums & g_nums else f"불일치({got[:40]})"


def cmp_age(truth: str | None, got: str | None) -> str:
    if not truth:
        return "제한 없음"
    nums = re.findall(r"\d+", truth)
    if not nums:
        return "기준 문구만(비교 생략)"
    if not got:
        return "누락"
    return "일치" if all(n in re.findall(r"\d+", got) for n in nums) else f"불일치({got[:40]})"


def notify(ev: dict | None, today: date, avail_from: str | None) -> str:
    if not ev:
        return "아님(행사 없음)"
    ref = ev.get("end_date") or ev.get("start_date")
    if ref and ref < today.isoformat():
        return "아님(지난 행사)"
    if avail_from and ref and ref < avail_from:
        return "예고(연례)" if ev.get("annual") else "아님(참가 가능일 이전)"
    return "대상(즉시)" if ev.get("urgency") == "now" else "대상(요약)"


def evaluate(gt: dict, trace: dict, today: date, owner_from: str | None) -> dict:
    rows = []
    for t in gt["events"] + [dict(x, url=x.get("url") or "", title=x.get("title") or "") for x in gt.get("excluded", [])]:
        if not t.get("url") and not t.get("title"):
            continue
        found = sorted({e["stage"] for e in trace["entries"] if same(t, e.get("url"), e.get("title"))})
        ai = [a for a in trace.get("ai", []) if same(t, a.get("url"), None)]
        evs = [e for e in trace.get("events", []) if same(t, e.get("url"), e.get("title"))]
        ev = evs[0] if evs else None
        is_valid = t.get("participation") in ("open", "full", "registration_opening")
        truth_date = t.get("start_date") if t.get("date_confirmed") else None
        rows.append({
            "id": t["id"], "valid": is_valid, "participation": t.get("participation"),
            "stages": found, "ai": [{"status": a["status"], "events": a["events"], "error": a.get("error")} for a in ai],
            "event": bool(ev), "bot": ev.get("bot") if ev else None,
            "date": cmp_date(truth_date, (ev or {}).get("registration_opens") if t.get("participation") == "registration_opening" else (ev or {}).get("start_date")),
            "cost": cmp_cost(t.get("cost_text"), (ev or {}).get("cost_text")),
            "age": cmp_age(t.get("age_limit"), " ".join(str((ev or {}).get(k) or "") for k in ("age_text", "eligibility"))),
            "notify_friend": notify(ev, today, None),
            "notify_owner": notify(ev, today, owner_from) if owner_from else "소유자 참가 가능일(비밀값) 필요",
        })
    valid = [r for r in rows if r["valid"]]
    invalid = [r for r in rows if not r["valid"]]
    n = len(valid) or 1
    pct = lambda k: round(100 * k / n)  # noqa: E731
    by_stage = {s: sum(1 for r in valid if s in r["stages"]) for s in STAGES}
    any_found = sum(1 for r in valid if r["stages"])
    extracted = sum(1 for r in valid if r["event"])
    notified = sum(1 for r in valid if r["notify_friend"].startswith("대상"))
    summary = {
        "valid_events": len(valid),
        "found_by_stage": {s: f"{v}/{len(valid)} ({pct(v)}%)" for s, v in by_stage.items()},
        "found_only_by": {s: sum(1 for r in valid if r["stages"] == [s]) for s in STAGES},
        "found_any_stage": f"{any_found}/{len(valid)} ({pct(any_found)}%)",
        "became_event": f"{extracted}/{len(valid)} ({pct(extracted)}%)",
        "notify_eligible_friend_profile": f"{notified}/{len(valid)} ({pct(notified)}%)",
        "date_correct": f"{sum(1 for r in valid if r['date'] == '일치')}/{sum(1 for r in valid if r['date'] not in ('기준 미확인',))}",
        "cost_correct": f"{sum(1 for r in valid if r['cost'] == '일치')}/{sum(1 for r in valid if r['cost'] != '기준 미확인')}",
        "age_correct": f"{sum(1 for r in valid if r['age'] == '일치')}/{sum(1 for r in valid if r['age'] not in ('제한 없음', '기준 문구만(비교 생략)'))}",
        "excluded_wrongly_notified": f"{sum(1 for r in invalid if r['notify_friend'].startswith('대상'))}/{len(invalid)}",
    }
    return {"summary": summary, "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--out")
    ap.add_argument("--today")
    ap.add_argument("--owner-from")
    a = ap.parse_args(argv)
    gt = json.loads((ROOT / "eval/ground_truth.json").read_text())
    trace = json.loads(Path(a.trace).read_text())
    res = evaluate(gt, trace, date.fromisoformat(a.today) if a.today else date.today(), a.owner_from)
    if a.out:
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(res["summary"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
