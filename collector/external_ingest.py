"""외부 검색(예: 다른 AI 의 예약 검색)이 찾은 콜로라도 행사를 기존 파이프라인으로 받아 미국 Worker 에 등록한다.

경로: 외부 → GitHub repository_dispatch(event_type=external-events) → 이 스크립트(GitHub Actions) → Worker /admin/enqueue.
- Worker 관리 키는 GitHub 비밀값에서 실행 중에만 만들고 출력하지 않는다(scripts/cf_env.sh 가 가림 처리).
- 받은 내용을 그대로 믿지 않는다: 주소(https)·제목을 검사하고, 공식 페이지를 다시 읽어 수집기와 같은 추출(Haiku) → 규칙 → 정밀 판정 →
  추천 판정(Opus) → 같은 행사 판별(기존 행사와 같으면 같은 키, 다시 알리지 않음)을 거친다.
- 메시지 형식·버튼·발송 대기열·소유자 전용 설정은 다른 행사와 똑같다. 메시지에 출처를 표시하지 않는다.

입력(JSON): {"events": [{"title": "...", "url": "https://...", "start_date": "YYYY-MM-DD", "end_date": "...", "time": "...",
             "location": "...", "cost": "...", "age": "...", "registration_opens": "YYYY-MM-DD", "deadline": "YYYY-MM-DD",
             "description": "...", "audience": "family|kids|life"}], "test": false}
  python collector/external_ingest.py --payload-env EXTERNAL_PAYLOAD
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
import identity  # noqa: E402
import rules  # noqa: E402
from fetch import Entry, fetch_detail, fetch_source, new_client, normalize_url  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAX_EVENTS = 30
SOURCE_ID = "ext-search"
TEXT_FIELDS = ("start_date", "end_date", "time", "location", "cost", "age", "registration_opens", "deadline", "audience", "description")


def validate(raw: dict) -> tuple[list[dict], list[str]]:
    """받은 항목 검사: https 주소·제목 필수, 문자열 길이 제한, 알 수 없는 필드 버림."""
    ok, bad = [], []
    items = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [], ["events 목록이 없음"]
    for i, x in enumerate(items[:MAX_EVENTS]):
        if not isinstance(x, dict):
            bad.append(f"{i}: 형식 오류")
            continue
        url, title = str(x.get("url") or "").strip(), str(x.get("title") or "").strip()
        if not re.match(r"^https://[^\s/]+\.[^\s]+$", url) or len(url) > 1000:
            bad.append(f"{i}: 주소가 https 가 아님")
            continue
        if not title or len(title) > 300:
            bad.append(f"{i}: 제목 없음")
            continue
        clean = {"url": url, "title": title}
        for k in TEXT_FIELDS:
            v = x.get(k)
            if v is not None and str(v).strip():
                clean[k] = str(v).strip()[:1500 if k == "description" else 200]
        ok.append(clean)
    if len(items) > MAX_EVENTS:
        bad.append(f"{len(items) - MAX_EVENTS}건 초과분은 받지 않음(한 번에 {MAX_EVENTS}건)")
    return ok, bad


def to_entry(x: dict) -> Entry:
    # 외부가 정리한 내용은 '참고'로만 붙이고, 공식 페이지 본문을 다시 읽어 함께 추출에 넘긴다
    note = " ".join(f"{k}: {x[k]}." for k in TEXT_FIELDS if k in x and k != "description")
    text = f"{x.get('description', '')} {note}".strip()
    return Entry(SOURCE_ID, normalize_url(x["url"]), x["title"], text, None, {"official": False, "external": True})


def registered_hosts(api: WorkerAPI) -> set[str]:
    srcs = json.loads((ROOT / "config/sources.json").read_text())["sources"]
    try:
        srcs += api.sources_extra()
    except Exception:  # noqa: BLE001
        pass
    return {identity._host(x["url"]) for x in srcs if x.get("url")}


def check_gaps(evs: list[dict], items: list[dict], by_key: dict, api: WorkerAPI, client, models: dict, usage) -> dict:
    """외부가 찾은 행사마다: found(우리 정보원·검색이 이미 등록) / queued(수집했지만 AI 처리 전) / registered-missed(등록 정보원 사이트인데 못 찾음)
    / unregistered(정보원 없음). 놓친 것은 큰 모델이 원인과 보완(감시할 공식 페이지·검색어)을 정하고, 공식 페이지는 실제로 조회되는지 확인 뒤 정보원에 추가한다."""
    hosts = registered_hosts(api)
    queued = {normalize_url(x["url"]) for x in api.queue_find([ev["url"] for ev in evs])}
    rows, todo = [], []
    for ev in evs:
        prev = by_key.get(ev["event_key"])
        host = identity._host(ev["url"])
        if prev and prev.get("source_id") and prev["source_id"] != SOURCE_ID:
            found = "found"
        elif normalize_url(ev["url"]) in queued:
            found = "queued"
        elif host in hosts or any(host.endswith("." + h) or h.endswith("." + host) for h in hosts):
            found = "registered-missed"
        else:
            found = "unregistered"
        row = {"event_key": ev["event_key"], "url": ev["url"], "title": ev.get("title_ko") or ev.get("title"), "host": host, "found": found}
        rows.append(row)
        if found in ("registered-missed", "unregistered"):
            x = items[ev["_entry_index"]]
            todo.append({"index": len(todo), "title": ev.get("title"), "url": ev["url"], "found": found, "start_date": ev.get("start_date"),
                         "org": ev.get("org"), "description": x.get("description", "")[:400]})
    sources, queries = [], []
    fixes = ai.gap_analysis(client, models, todo, sorted(hosts), usage) if todo else {}
    with new_client() as hc:
        for t in todo:
            row = next(r for r in rows if r["url"] == t["url"])
            g = fixes.get(t["index"])
            if not g:
                row["cause"] = "원인 분석 실패(다음 입력 때 다시)"
                continue
            row["cause"] = g.cause_ko.strip()[:200]
            acts = []
            if g.monitor_url and g.monitor_url.startswith("https://") and identity._host(g.monitor_url) not in ("", None):
                src = {"id": "gap-check", "type": g.monitor_type, "url": g.monitor_url}
                if g.monitor_mode == "page-change":
                    src["mode"] = "page-change"
                r = fetch_source(src, client=hc)
                if r.ok and (r.entries or g.monitor_mode == "page-change"):
                    sources.append({"url": g.monitor_url, "name": (g.monitor_name or row["title"] or "")[:80], "type": g.monitor_type,
                                    "mode": "page-change" if g.monitor_mode == "page-change" else None, "reason": row["cause"],
                                    "bots": [ev_bot(evs, t["url"])]})
                    acts.append(f"정보원 추가: {g.monitor_url}")
                else:
                    acts.append(f"정보원 후보 조회 실패({r.error or '항목 없음'}): {g.monitor_url}")
            for q in g.queries[:3]:
                if q.strip():
                    queries.append({"query": q.strip()[:120], "bot": g.query_bot, "reason": row["cause"]})
                    acts.append(f"검색어 추가: {q.strip()[:60]}")
            row["action"] = " / ".join(acts)[:500] or "보완 제안 없음"
    api.gaps(rows, sources, queries)
    return {"checked": len(rows), "by_found": {k: sum(1 for r in rows if r["found"] == k) for k in ("found", "queued", "registered-missed", "unregistered")},
            "sources_added": len(sources), "queries_added": len(queries), "rows": [{k: r.get(k) for k in ("title", "found", "cause", "action")} for r in rows]}


def ev_bot(evs: list[dict], url: str) -> str:
    return next((e.get("bot") for e in evs if e["url"] == url), "family")


def ingest(raw: dict, api: WorkerAPI, client, models: dict, today: str) -> dict:
    items, bad = validate(raw)
    report = {"received": len(raw.get("events") or []) if isinstance(raw, dict) else 0, "valid": len(items), "rejected": bad}
    if not items:
        return report
    entries = [to_entry(x) for x in items]
    with new_client() as hc:
        for e, x in zip(entries, items):
            hint = e.text
            try:
                fetch_detail(e, hc, chars=4000)
            except Exception:  # noqa: BLE001
                pass
            if hint and hint not in (e.text or ""):
                e.text = f"{e.text or ''}\n[참고 요약] {hint}"[:5500]
    usage = ai.Usage(models.get("prices", {}))
    out = ai.extract(client, models, entries, today, usage)
    extracted = [out.results[i] for i in sorted(out.results)]
    evs, dropped = rules.finalize(extracted, entries, datetime.fromisoformat(today).date())
    review_idx = [i for i, ev in enumerate(evs) if ev.get("needs_review")]
    if review_idx:
        jo = ai.judge(client, models, [(rules.for_worker(evs[i]), entries[evs[i]["_entry_index"]].text or "") for i in review_idx], usage)
        held = {evs[review_idx[j]]["_entry_index"] for j in jo.missing}  # 판정이 빠진 것은 보내지 않음
        evs, removed = rules.apply_verdicts(evs, review_idx, jo.verdicts)
        evs = [ev for ev in evs if ev["_entry_index"] not in held]
        dropped["judge-removed"] = removed
    # 외부가 정한 대상(가족·어린이·일상)은 추천 판정의 참고로만 쓴다(판정이 최종)
    for ev in evs:
        aud = items[ev["_entry_index"]].get("audience", "").lower()
        if aud in ("family", "kids", "life", "adult", "adults"):
            ev["bot"] = "life" if aud.startswith("adult") else aud
    ro = ai.recommend(client, models, [(rules.for_worker(ev), entries[ev["_entry_index"]].text or "") for ev in evs], today, usage) if evs else ai.RecommendOutcome()
    for j, r in ro.recs.items():
        ai.apply_recommendation(evs[j], r)
    evs = [ev for j, ev in enumerate(evs) if j in ro.recs]
    evs, _ = identity.collapse_same(evs)
    index = api.event_index()
    by_key = {x["event_key"]: x for x in index}
    report["identity"] = identity.assign_keys(evs, index)
    evs, dup = identity.dedupe(evs)
    payload = [rules.for_worker(ev) for ev in evs]
    res = api.enqueue(payload) if payload else {}
    # 우리 수집이 이 행사를 이미 찾았는지 대조하고, 놓쳤으면 원인 분석 → 정보원·검색어 자동 보완
    try:
        report["gaps"] = check_gaps(evs, items, by_key, api, client, models, usage)
    except Exception as e:  # noqa: BLE001  대조가 실패해도 등록·발송은 그대로
        report["gaps_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    api.add_usage(usage.by_model, float(models.get("limits", {}).get("monthly_budget_usd", 20)))
    report.update({"extracted_events": len(extracted), "dropped": dropped, "recommend_missing": len(ro.missing), "duplicates": dup,
                   "enqueued": len(payload), "worker": {k: res.get(k) for k in ("new", "changed", "same", "outbox")} if res else {},
                   "levels": {lv: sum(1 for p in payload if p.get("rec_level") == lv) for lv in ("push", "interest", "none")},
                   "event_keys": [p["event_key"] for p in payload], "usd": round(usage.usd, 4)})
    # 시험 요청이면 등록한 행사를 소유자에게 실제 형식으로 한 통씩 보낸다(소유자 전용)
    if raw.get("test") is True and payload:
        report["test_send"] = [api._post("/admin/preview", {"bot": p["bot"], "keys": [p["event_key"]], "n": 1}) for p in payload[:3]]
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload-env", default="EXTERNAL_PAYLOAD")
    ap.add_argument("--payload-file")
    a = ap.parse_args(argv)
    text = Path(a.payload_file).read_text() if a.payload_file else os.environ.get(a.payload_env, "")
    try:
        raw = json.loads(text or "{}")
    except json.JSONDecodeError:
        print("::error title=external events::JSON 형식 오류")
        return 1
    models = json.loads((ROOT / "config/models.json").read_text())
    today = datetime.now(ZoneInfo("America/Denver")).date().isoformat()
    report = ingest(raw, WorkerAPI.from_env(), ai.get_client(), models, today)
    report["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # 주소·키는 출력하지 않는다. 건수와 행사 키만
    shown = {k: v for k, v in report.items() if k not in ("test_send",)}
    if "test_send" in report:
        shown["test_send"] = [{"sent": t.get("sent"), "ok": t.get("ok")} for t in report["test_send"]]
    print("::notice title=external events::" + json.dumps(shown, ensure_ascii=False)[:3800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
