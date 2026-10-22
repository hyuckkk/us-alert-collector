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
from fetch import Entry, fetch_detail, new_client, normalize_url  # noqa: E402
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
    report["identity"] = identity.assign_keys(evs, api.event_index())
    evs, dup = identity.dedupe(evs)
    payload = [rules.for_worker(ev) for ev in evs]
    res = api.enqueue(payload) if payload else {}
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
