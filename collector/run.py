"""하루 1회 수집 실행: 무토큰 수집 → 이미 본 것 제외 → AI 추출 → 규칙 처리 → 애매한 건만 큰 모델 → Worker 로 보내기.

python collector/run.py [--dry-run] [--no-ai] [--only SOURCE_ID] [--max N] [--out reports/run.json]
  --dry-run  Worker 에 아무것도 쓰지 않는다(이미 본 항목 확인도 하지 않음). AI 는 키가 있으면 호출한다.
  --no-ai    AI 를 부르지 않는다(수집·규칙만 점검).
설계안 처리 흐름: 정보원 등록부 → 무토큰 수집 → 변경분 추출 → 규칙 처리 → 발송 → 피드백.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import annual  # noqa: E402
import rules  # noqa: E402
from fetch import fetch_detail, fetch_source, new_client  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DENVER = ZoneInfo("America/Denver")
MAX_DETAIL_FETCH = 60  # 실행당 상세 페이지 조회 상한
THEMES = {  # 설계안 요일별 심층 탐색(월=0)
    0: "kids after-school programs, camps, youth sports and arts/science classes",
    1: "adult hobby and skills classes (crafts, cooking, tech, language exchange, sports leagues)",
    2: "Westminster / Adams County local government, schools (Adams 12, Westminster Public Schools) and community events",
    3: "aviation, military and special facilities open to the public (air shows, base open houses, Air Force Academy, space)",
    4: "nature, state parks and national parks (ranger programs, reservations, seasonal openings)",
    5: "universities near Denver (DU, CU Boulder, CSU, Mines): public lectures, games, family days",
    6: "family travel from Denver: unusual flight deals, cruise family programs and kids-sail-free promotions, road-trip festivals",
}


def load(name: str) -> dict:
    return json.loads((ROOT / "config" / name).read_text())


def is_due(source: dict, today: date) -> bool:
    n = max(int(source.get("every_days", 1)), 1)
    return (today.toordinal() + zlib.crc32(source["id"].encode())) % n == 0


def main(argv=None, client=None, api: WorkerAPI | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--max", type=int)
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    today = datetime.now(DENVER).date()
    checked_at = started.isoformat(timespec="seconds")
    models = load("models.json")
    api = api or WorkerAPI.from_env(args.dry_run)
    m: dict = {"sources_total": 0, "sources_ok": 0, "queries": 0, "entries_fetched": 0, "entries_new": 0, "entries_seen": 0,
               "ai_items": 0, "events_out": 0, "outbox": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "searches": 0}
    alerts: list[str] = []
    report: dict = {"run_id": run_id, "today": today.isoformat(), "dry_run": api.dry_run, "sources": [], "dropped": {}, "events": []}

    # 1) 정보원 목록: 저장소 등록부 + 소유자가 승격한 후보
    sources = [s for s in load("sources.json")["sources"] if s.get("enabled", True)]
    html_link_sources = set()
    try:
        sources += api.sources_extra()
    except Exception as e:  # noqa: BLE001
        alerts.append(f"sources-extra 조회 실패: {e}")
    if args.only:
        sources = [s for s in sources if s["id"] == args.only]
    due = [s for s in sources if args.only or is_due(s, today)]

    # 2) 무토큰 수집
    entries = []
    for s in due:
        if s["type"] == "html" and s.get("mode") != "page-change":
            html_link_sources.add(s["id"])
        r = fetch_source(s, secrets=os.environ)
        m["sources_total"] += 1
        m["sources_ok"] += int(r.ok)
        report["sources"].append({"id": s["id"], "ok": r.ok, "status": r.status, "items": len(r.entries), "error": r.error})
        for e in r.entries:
            e.extra["bots"] = s.get("bots", [])
        entries += r.entries
    if args.only is None:
        q = load("queries.json")
        for query in q["daily"] + q["weekday"].get(str(today.weekday()), []):
            r = fetch_source({"id": "gnews:" + query[:40], "type": "gnews", "query": query})
            m["queries"] += 1
            entries += r.entries[:15]
    if m["sources_total"] and m["sources_ok"] / m["sources_total"] < 0.5:
        alerts.append(f"정보원 조회 성공률 {m['sources_ok']}/{m['sources_total']}")

    # 3) 이미 본 것 제외 (주소+내용 해시)
    by_key = {}
    for e in entries:
        by_key.setdefault(rules.entry_key(e), e)
    m["entries_fetched"] = len(by_key)
    hashes = {k: rules.entry_hash(e) for k, e in by_key.items()}  # 확인할 때의 해시를 표시할 때도 그대로 쓴다
    items = [{"key": k, "hash": hashes[k], "source_id": e.source_id} for k, e in by_key.items()]
    new, changed = api.seen_check(items)
    fresh_keys = [k for k in by_key if k in new or k in changed]
    m["entries_seen"] = len(by_key) - len(fresh_keys)
    # 공식 정보원 먼저, 검색 결과는 뒤로
    fresh_keys.sort(key=lambda k: by_key[k].source_id.startswith("gnews"))
    cap = args.max or models["limits"]["max_new_entries_per_run"]
    if len(fresh_keys) > cap:
        alerts.append(f"신규 {len(fresh_keys)}건 중 {cap}건만 처리(실행당 상한)")
        fresh_keys = fresh_keys[:cap]
    fresh = [by_key[k] for k in fresh_keys]
    m["entries_new"] = len(fresh)
    # html 링크 항목은 상세 페이지 본문을 붙인다(토큰 0)
    targets = [e for e in fresh if e.source_id in html_link_sources and not e.text][:MAX_DETAIL_FETCH]
    if targets:
        with new_client() as hc:
            m["details_ok"] = sum(fetch_detail(e, hc) for e in targets)
        m["details_total"] = len(targets)

    events: list[dict] = []
    processed: list[str] = []
    usage = None
    if not args.no_ai and (fresh or models["discover"]["enabled"]) and os.environ.get("ANTHROPIC_API_KEY"):
        import ai  # 키가 있을 때만 SDK 를 불러온다
        usage = ai.Usage(models["prices"], models.get("web_search_usd", 0.01))
        client = client or ai.get_client()
        budget = models["limits"]["monthly_budget_usd"]
        spent = api.month_usd()
        if spent >= budget:
            alerts.append(f"이번 달 AI 사용액 ${spent:.2f} ≥ 예산 ${budget} — AI 단계 건너뜀")
        else:
            if spent >= budget * models["limits"]["warn_ratio"]:
                alerts.append(f"이번 달 AI 사용액 ${spent:.2f} (예산 ${budget}의 {spent / budget:.0%})")
            try:
                # 4) 발굴(B): 오늘 요일 주제로 새 정보원·행사 페이지 찾기
                if models["discover"]["enabled"] and args.only is None:
                    known = [s["url"] for s in sources]
                    d = ai.discover(client, models, THEMES[today.weekday()], known, usage)
                    report["discover"] = {k: d[k] for k in ("dropped_urls", "result_urls", "raw_text")} | {"theme": THEMES[today.weekday()], "sources": d["sources"], "events": d["events"]}
                    api.candidates([{**s, "bots": s.get("bots") or []} for s in d["sources"]])
                    from fetch import Entry, normalize_url
                    found = {}
                    for ev in d["events"][:10]:
                        e = Entry("ai-discover", normalize_url(ev["url"]), ev.get("title") or ev["url"], ev.get("why") or "")
                        found.setdefault(rules.entry_key(e), e)
                    found = {k: e for k, e in found.items() if k not in by_key}
                    if found:
                        hashes.update({k: rules.entry_hash(e) for k, e in found.items()})
                        n2, c2 = api.seen_check([{"key": k, "hash": hashes[k], "source_id": e.source_id} for k, e in found.items()])
                        for k, e in found.items():
                            if k in n2 or k in c2:
                                by_key[k] = e
                                fresh.append(e)
                # 5) 추출(작은 모델) → 규칙 → 애매한 건만 판단(큰 모델)
                extracted = ai.extract(client, models, fresh, today.isoformat(), usage) if fresh else []
                m["ai_items"] = len(fresh)
                events, dropped = rules.finalize(extracted, fresh, today, checked_at)
                review_idx = [i for i, ev in enumerate(events) if ev.get("needs_review")]
                if review_idx:
                    verdicts = ai.judge(client, models, [(rules.for_worker(events[i]), fresh[events[i]["_entry_index"]].text or "") for i in review_idx], usage)
                    events, removed = rules.apply_verdicts(events, review_idx, verdicts)
                    dropped["judge-removed"] = removed
                report["dropped"] = dropped
                processed = [rules.entry_key(e) for e in fresh]
            except Exception as e:  # noqa: BLE001
                alerts.append(f"AI 단계 오류: {type(e).__name__}: {str(e)[:200]}")
                report["ai_error"] = traceback.format_exc()[-2000:]
    elif fresh and not args.no_ai:
        alerts.append("ANTHROPIC_API_KEY 없음 — AI 단계 건너뜀")

    # 6) 연간 일정표(토큰 0)
    events += annual.due_items(load("annual.json"), today, checked_at)

    # 7) Worker 로 보내기 → 사람별 대기열 → 즉시 알림 발송
    out = [rules.for_worker(e) for e in events]
    m["events_out"] = len(out)
    report["events"] = out
    if out:
        r = api.enqueue(out)
        m["outbox"] = r.get("outbox", 0)
        report["enqueue"] = r
    if processed:
        api.seen_mark([{"key": k, "hash": hashes[k], "source_id": by_key[k].source_id} for k in processed])
    report["dispatch"] = api.dispatch()

    if usage:
        api.add_usage(usage.by_model)
        m.update(usage.totals())
        m["usd"] = round(usage.usd, 4)
        report["usage"] = usage.by_model
    report["metrics"] = m
    report["alerts"] = alerts
    api.run_log({"run_id": run_id, "started_at": started.isoformat(timespec="seconds"),
                 "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "metrics": m, "alerts": alerts})
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n")
    print(json.dumps({"run_id": run_id, "metrics": m, "alerts": alerts, "dropped": report["dropped"]}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    main()
