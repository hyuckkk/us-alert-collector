"""경로별 누락 보고(매일): GitHub 수집기·Claude 독립 검색·ChatGPT 검색이 각각 무엇을 못 찾았는지 소유자에게 알리고,
못 찾은 이유를 바탕으로 다음 검색 범위를 넓힌다.

- 대상: 최근 26시간 안에 처음 등록된 '적극 알림' 행사(발송 대상인 것만. 우리 기준으로 알리지 않는 행사는 놓쳐도 누락이 아님).
- 그 시간 안에 실제로 돈 경로(작업량 기록이 있는 경로)만 평가한다. 돌지 않은 경로는 "실행 기록 없음"으로 따로 적는다.
- GitHub 수집기가 놓친 것: 외부 입력 때 이미 원인 분석·정보원/검색어 자동 추가·재발견 확인을 했다(ext_gaps). 그 결과를 보고한다.
- Claude·ChatGPT 가 놓친 것: 큰 모델이 이유와 보완(검색어·감시할 공식 페이지)을 정하고 external-inbox/LESSONS.md 에 쌓는다.
  두 검색은 매번 이 파일을 먼저 읽고 검색 범위에 넣는다(지시 파일에 명시).
- 텔레그램 보고는 소유자에게만, 하루 한 번, 누락이 있을 때만 보낸다. 지인에게는 보내지 않는다. 출처 표시는 보고에만 있고 행사 알림에는 없다.

  python collector/miss_report.py [--hours 26] [--lessons external-inbox/LESSONS.md] [--no-send]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PATHS = ("github_collector", "claude", "chatgpt")
NAME = {"github_collector": "GitHub 수집기", "claude": "Claude 검색", "chatgpt": "ChatGPT 검색"}
FIX_KO = {"github_collector": "정보원·검색어 자동 보완", "claude": "다음 Claude 검색 범위에 반영", "chatgpt": "다음 ChatGPT 검색 범위에 반영"}
MAX_LESSONS = 80


def compute(compare: dict, workload: list[dict], since: str) -> dict:
    """경로별 누락 계산. 반환: {ran: {path: bool}, total, found: {path: n}, missed: {path: [event]}}"""
    ran = {p: any(w.get("path") == p and str(w.get("started_at") or "") >= since for w in workload) for p in PATHS}
    evs = [e for e in compare.get("events", []) if e.get("final_level") == "push"
           and min(f.get("found_at") or f.get("first_seen") or "9" for f in e["finds"].values()) >= since]
    found = {p: sum(1 for e in evs if p in e["finds"]) for p in PATHS}
    missed = {p: [e for e in evs if ran[p] and p not in e["finds"]] for p in PATHS}
    return {"ran": ran, "total": len(evs), "found": found, "missed": missed}


def lessons_for(misses: list[tuple[str, dict]], client, models: dict, usage) -> list[dict]:
    """Claude·ChatGPT 가 놓친 행사마다 이유와 보완(검색어·감시 페이지)을 정한다."""
    items = [{"index": i, "title": e.get("title"), "url": next(iter(e["finds"].values())).get("url"), "found": f"missed by {p}",
              "start_date": None, "org": None, "description": f"Found by {', '.join(e['finds'])}; not found by {p}'s own web search.",
              "our_history": f"{p} web search did not report it"} for i, (p, e) in enumerate(misses)]
    fixes = ai.gap_analysis(client, models, items, [], usage) if items else {}
    out = []
    for i, (p, e) in enumerate(misses):
        g = fixes.get(i)
        url = items[i]["url"] or ""
        out.append({"path": p, "title": e.get("title") or "", "host": urlparse(url).netloc, "url": url,
                    "found_by": sorted(e["finds"]), "cause": g.cause_ko.strip() if g else "원인 분석 실패",
                    "queries": [q.strip() for q in (g.queries if g else []) if q.strip()][:3],
                    "monitor_url": g.monitor_url if g and g.monitor_url else None})
    return out


def write_lessons(path: Path, new: list[dict], today: str) -> int:
    """external-inbox/LESSONS.md 에 쌓는다(최근 MAX_LESSONS 개). 사람이 읽고 두 검색이 그대로 따를 수 있는 형식."""
    old = []
    if path.exists():
        old = [json.loads(m) for m in re.findall(r"<!-- lesson (\{.*?\}) -->", path.read_text())]
    seen = {(x["path"], x["url"]) for x in old}
    added = [dict(x, date=today) for x in new if (x["path"], x["url"]) not in seen]
    rows = (old + added)[-MAX_LESSONS:]
    lines = ["# 검색 누락 교훈 (자동 갱신)", "",
             "Claude·ChatGPT 독립 검색은 매번 검색 전에 이 파일을 읽고, 아래 검색어와 감시 페이지를 검색 범위에 넣는다.",
             "이 목록은 다른 경로가 찾아서 실제로 알린 '적극 알림' 행사를 그 경로가 못 찾았을 때 쌓인다. GitHub 수집기의 누락은 정보원·검색어로 자동 보완된다.", ""]
    for x in reversed(rows):
        lines.append(f"- {x['date']} · **{NAME.get(x['path'], x['path'])} 누락** · {x['title']} ({x['host']}) — {x['cause']}")
        if x.get("queries"):
            lines.append(f"  - 검색어: {' / '.join(x['queries'])}")
        if x.get("monitor_url"):
            lines.append(f"  - 감시할 공식 페이지: {x['monitor_url']}")
        lines.append(f"  <!-- lesson {json.dumps(x, ensure_ascii=False)} -->")
    path.write_text("\n".join(lines) + "\n")
    return len(added)


def render(r: dict, gaps: dict, lessons: list[dict], day: str) -> str | None:
    if not any(r["missed"][p] for p in PATHS):
        return None
    lines = [f"🔎 검색 경로 대조 ({day}, 최근 하루 적극 알림 {r['total']}건)",
             " · ".join(f"{NAME[p]} {r['found'][p]}건" if r["ran"][p] else f"{NAME[p]} 실행 기록 없음" for p in PATHS), ""]
    for p in PATHS:
        ms = r["missed"][p]
        if not ms:
            continue
        lines.append(f"• {NAME[p]} 누락 {len(ms)}건 → {FIX_KO[p]}")
        for e in ms[:5]:
            title = (e.get("title") or e["event_key"])[:40]
            if p == "github_collector":
                g = gaps.get(e["event_key"]) or e.get("gap") or {}
                why = (g.get("cause") or "원인 미확인")[:60]
                again = {1: " · 보완 후 다시 잡힘", 0: " · 보완 후에도 안 잡힘(정보원 확인 필요)"}.get(g.get("rediscovered"), "")
                lines.append(f"  - {title}: {why}{again}")
            else:
                lz = next((x for x in lessons if x["path"] == p and x["title"] == (e.get("title") or "")), None)
                lines.append(f"  - {title}: {(lz['cause'] if lz else '원인 분석 대기')[:60]}")
        if len(ms) > 5:
            lines.append(f"  - 외 {len(ms) - 5}건")
    return "\n".join(lines)[:3800]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=26)
    ap.add_argument("--lessons", default=str(ROOT / "external-inbox/LESSONS.md"))
    ap.add_argument("--no-send", action="store_true")
    a = ap.parse_args(argv)
    api = WorkerAPI.from_env()
    since = (datetime.now(timezone.utc) - timedelta(hours=a.hours)).isoformat(timespec="seconds")
    day = datetime.now(timezone.utc).date().isoformat()
    compare = api._get(f"/admin/compare?since={since[:10]}")
    workload = api._get(f"/admin/workload?since={since[:10]}").get("rows", [])
    r = compute(compare, workload, since)
    gaps = {g["event_key"]: g for g in api._get("/admin/gaps").get("gaps", [])}
    models = json.loads((ROOT / "config/models.json").read_text())
    usage = ai.Usage(models.get("prices", {}))
    ext_misses = [(p, e) for p in ("claude", "chatgpt") for e in r["missed"][p]]
    lessons = lessons_for(ext_misses, ai.get_client(), models, usage) if ext_misses else []
    added = write_lessons(Path(a.lessons), lessons, day) if lessons else 0
    text = render(r, gaps, lessons, day)
    sent = None
    if text and not a.no_send:
        sent = api._post("/admin/miss-report", {"key": day, "text": text}).get("sent")
    summary = {"since": since, "ran": r["ran"], "total_push": r["total"], "found": r["found"], "missed": {p: len(r["missed"][p]) for p in PATHS},
               "lessons_added": added, "report_sent": sent, "usd": round(usage.usd, 4)}
    if usage.by_model:
        api.add_usage(usage.by_model, float(models.get("limits", {}).get("credit_reference_usd", 0)))
    print("::notice title=miss report::" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
