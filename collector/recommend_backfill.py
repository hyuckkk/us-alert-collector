"""운영 DB 의 기존 행사에 추천 판정(큰 모델)을 적용하고, 실제로 보냈던 일상 봇 알림을 재평가한다(유료, 개발 예산).

추천 판정 단계가 생기기 전에 저장된 행사는 판정이 없어 이제 발송되지 않는다(저장만). 이 스크립트가 원문을 다시 읽어 판정을 붙인다.
판·변경 알림·대기열은 건드리지 않는다(/admin/recommend-update 는 판정 결과만 갱신).

  python collector/recommend_backfill.py --usd-cap 5 --out eval/recommend_backfill.json [--bots life] [--only-sent]
결과: 수준·봇·분야별 건수, 실제 발송했던 일상 봇 알림의 재평가표(eval/life_sent_review.md).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
from fetch import Entry, fetch_detail, new_client  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LEVEL_KO = {"push": "적극 알림", "interest": "개인 관심 설정 시 알림", "none": "일반 알림 제외"}
SHOW = ("title", "title_ko", "org", "start_date", "end_date", "time_text", "location", "city", "age_text", "cost_text", "signup",
        "registration_opens", "registration_status", "deadline", "eligibility", "tags", "annual", "bot", "url", "source_id")


def dump(api: WorkerAPI, bot: str = "", sent: bool = False) -> list[dict]:
    out, off = [], 0
    while True:
        page = api._get(f"/admin/events-dump?bot={bot}&sent={1 if sent else 0}&offset={off}")["events"]
        out += page
        if len(page) < 300:
            return out
        off += 300


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usd-cap", type=float, default=5.0)
    ap.add_argument("--out", default=str(ROOT / "eval/recommend_backfill.json"))
    ap.add_argument("--bots", default="life,family,kids")
    ap.add_argument("--only-sent", action="store_true")
    ap.add_argument("--skip-done", action="store_true", help="이미 판정이 있는 행사는 건너뜀")
    a = ap.parse_args(argv)
    cfg = json.loads((ROOT / "config/models.json").read_text())
    usage = ai.Usage(cfg.get("prices", {}), cap=a.usd_cap)
    api = WorkerAPI.from_env()
    sent_life = {e["event_key"]: e for e in dump(api, "life", sent=True)}
    events: list[dict] = []
    for bot in a.bots.split(","):
        events += dump(api, bot, sent=a.only_sent)
    # 실제로 보냈던 일상 봇 알림을 먼저(예산이 모자라도 재평가표는 완성되게), 그다음 가까운 날짜순
    seen = set()
    ordered = []
    for e in list(sent_life.values()) + sorted(events, key=lambda x: x.get("start_date") or x.get("registration_opens") or "9999"):
        if e["event_key"] in seen or (a.skip_done and e.get("rec_level")):
            continue
        seen.add(e["event_key"])
        ordered.append(e)

    def page(e):
        x = Entry(e.get("source_id") or "backfill", e["url"], e.get("title") or "")
        try:
            fetch_detail(x, hc, chars=3000)
        except Exception:  # noqa: BLE001
            x.text = ""
        return x.text or ""

    with new_client() as hc, ThreadPoolExecutor(8) as ex:
        texts = list(ex.map(page, ordered))
    client = ai.get_client()
    items = [({k: e.get(k) for k in SHOW}, t) for e, t in zip(ordered, texts)]
    today = datetime.now(ZoneInfo("America/Denver")).date().isoformat()
    ro = ai.recommend(client, cfg, items, today, usage) if items else ai.RecommendOutcome()
    updates, rows = [], []
    for j, e in enumerate(ordered):
        r = ro.recs.get(j)
        if not r:
            continue
        # 수집기와 같은 규칙으로 적용(특별 근거가 없으면 적극 알림으로 두지 않음)
        x = ai.apply_recommendation({"title_ko": e.get("title_ko")}, r)
        updates.append({"event_key": e["event_key"], **{k: x.get(k) for k in ("rec_level", "bot", "category", "rec_score", "summary_ko", "highlights",
                                                                              "rec_reason", "special_reason", "title_ko")}})
        rows.append({"event_key": e["event_key"], "was_bot": e.get("bot"), "was_title": e.get("title_ko") or e.get("title"), "source_id": e.get("source_id"),
                     "sent_life": e["event_key"] in sent_life, **updates[-1]})
    for i in range(0, len(updates), 100):
        api._post("/admin/recommend-update", {"items": updates[i:i + 100]})
    life_rows = [r for r in rows if r["sent_life"]]
    summary = {
        "events_total": len(ordered), "judged": len(rows), "missing": len(ro.missing), "usd": round(usage.usd, 4),
        "by_level": dict(Counter(r["rec_level"] for r in rows)), "by_bot": dict(Counter(r["bot"] for r in rows)),
        "by_category": dict(Counter(r["category"] for r in rows)),
        "moved_bot": sum(1 for r in rows if r["bot"] != r["was_bot"]),
        "life_sent": {"total": len(sent_life), "judged": len(life_rows), "by_level": dict(Counter(r["rec_level"] for r in life_rows))},
    }
    Path(a.out).write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1) + "\n")
    order = {"push": 0, "interest": 1, "none": 2}
    md = ["# 일상 봇으로 실제 보냈던 알림 재평가", "", f"대상 {len(sent_life)}건 · 판정 {len(life_rows)}건 · " +
          " · ".join(f"{LEVEL_KO[k]} {v}건" for k, v in sorted(summary['life_sent']['by_level'].items(), key=lambda x: order[x[0]])), "",
          "| # | 행사(새 제목) | 판정 | 대상 봇 | 분야 | 특별 근거(적극 알림) 또는 이유 |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(sorted(life_rows, key=lambda r: (order[r["rec_level"]], -r["rec_score"])), 1):
        md.append(f"| {i} | {r['title_ko']} | {LEVEL_KO[r['rec_level']]} | {r['bot']} | {r['category']} | {r.get('special_reason') or r['rec_reason']} |".replace("\n", " "))
    if life_rows:  # 이번 실행에서 실제 발송분을 판정한 경우에만(다른 봇만 판정한 실행이 표를 비우지 않게)
        (ROOT / "eval/life_sent_review.md").write_text("\n".join(md) + "\n")
    live = os.environ.get("USAGE_LIVE_FILE")
    if live:
        Path(live).write_text(json.dumps({"usd": round(usage.usd, 4), "calls": sum(m["calls"] for m in usage.by_model.values()), "by_model": usage.by_model}))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
