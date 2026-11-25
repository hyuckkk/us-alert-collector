"""추천 판정의 생각 깊이(effort)를 낮춰도 결과가 같은지 비교한다(유료, 배치라 반값). DB 에는 아무것도 쓰지 않는다.

소유자 결정(2026-11-25): 비용 절감 순서 1) 배치 2) effort 한 단계씩 낮추기(결과가 같으면) 3) 그래도 모자라면 Sonnet 검토.
같은 표본을 지금 설정(기준)과 낮춘 설정으로 각각 다시 판정해, 적극 알림(push) 여부·수준·봇이 얼마나 일치하는지와 비용을 비교한다.
기준도 다시 돌리는 이유: 같은 설정으로 두 번 돌려도 조금씩 달라지므로, 그 흔들림보다 차이가 큰지를 봐야 한다.

  python collector/effort_compare.py --n 40 --efforts high,medium,low --out eval/effort_compare.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
from fetch import Entry, fetch_detail, new_client  # noqa: E402
from recommend_backfill import SHOW, dump  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def compare(a: dict, b: dict) -> dict:
    """두 판정 묶음(event_key → (level, bot))의 일치율."""
    keys = sorted(set(a) & set(b))
    n = len(keys) or 1
    return {"n": len(keys),
            "push_same": round(sum((a[k][0] == "push") == (b[k][0] == "push") for k in keys) / n, 3),
            "level_same": round(sum(a[k][0] == b[k][0] for k in keys) / n, 3),
            "bot_same": round(sum(a[k][1] == b[k][1] for k in keys) / n, 3),
            "push_lost": [k for k in keys if a[k][0] == "push" and b[k][0] != "push"],
            "push_added": [k for k in keys if a[k][0] != "push" and b[k][0] == "push"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40, help="표본 행사 수(최근 날짜순, 이미 판정이 있는 것)")
    ap.add_argument("--efforts", default="high,medium,low", help="첫 값이 기준(지금 설정)")
    ap.add_argument("--repeat-base", action="store_true", default=True, help="기준 설정을 한 번 더 돌려 흔들림을 잰다")
    ap.add_argument("--wait-min", type=float, default=50, help="배치 대기 최대 분(넘으면 남은 것은 즉시 호출)")
    ap.add_argument("--usd-cap", type=float, default=5.0)
    ap.add_argument("--out", default=str(ROOT / "eval/effort_compare.json"))
    a = ap.parse_args(argv)
    cfg = json.loads((ROOT / "config/models.json").read_text())
    api = WorkerAPI.from_env()
    today = datetime.now(ZoneInfo("America/Denver")).date().isoformat()
    events = []
    for bot in ("life", "family", "kids"):
        events += [e for e in dump(api, bot) if e.get("rec_level")]
    events = [e for e in events if max(str(e.get(k) or "") for k in ("end_date", "start_date", "registration_opens", "deadline")) >= today]
    events = sorted(events, key=lambda x: x.get("start_date") or x.get("registration_opens") or "9999")
    # 수준이 고르게 섞이게: push 와 나머지를 번갈아
    push = [e for e in events if e["rec_level"] == "push"]
    rest = [e for e in events if e["rec_level"] != "push"]
    sample = [x for pair in zip(push, rest) for x in pair][:a.n]
    if len(sample) < a.n:
        sample += [e for e in events if e not in sample][:a.n - len(sample)]

    def page(e):
        x = Entry(e.get("source_id") or "compare", e["url"], e.get("title") or "")
        try:
            fetch_detail(x, hc, chars=3000)
        except Exception:  # noqa: BLE001
            x.text = ""
        return x.text or ""

    with new_client() as hc, ThreadPoolExecutor(8) as ex:
        texts = list(ex.map(page, sample))
    items = [({k: e.get(k) for k in SHOW}, t) for e, t in zip(sample, texts)]
    client = ai.get_client()
    efforts = a.efforts.split(",")
    runs = [(efforts[0], "base")] + ([(efforts[0], "base-repeat")] if a.repeat_base else []) + [(x, x) for x in efforts[1:]]
    out: dict = {"date": today, "n": len(items), "stored": {}, "runs": {}}
    stored = {e["event_key"]: (e["rec_level"], e.get("bot")) for e in sample}
    out["stored"] = dict(Counter(v[0] for v in stored.values()))
    got: dict = {}
    for effort, name in runs:
        usage = ai.Usage(cfg.get("prices", {}), cap=a.usd_cap)
        c = {**cfg, "recommend": {**cfg["recommend"], "effort": effort}}
        ro = ai.recommend(client, c, items, today, usage, batch_wait_min=a.wait_min)
        got[name] = {sample[j]["event_key"]: (r.level, r.bot) for j, r in ro.recs.items()}
        out["runs"][name] = {"effort": effort, "usd": round(usage.usd, 4), "missing": len(ro.missing),
                             "levels": dict(Counter(v[0] for v in got[name].values())), **usage.totals()}
        print(f"{name}: effort={effort} usd={usage.usd:.3f} judged={len(ro.recs)}", flush=True)
    out["vs_base"] = {name: compare(got["base"], got[name]) for name in got if name != "base"}
    out["base_vs_stored"] = compare(stored, got["base"])
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({k: out[k] for k in ("runs", "vs_base")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
