"""Codex(ChatGPT 구독) 판정 시험용 표본을 만든다(AI 없음, 무료). DB 에는 쓰지 않는다.

지금 추천 규칙(2026-11-23 개정) 이후 Opus 가 판정한 행사 중 아직 안 끝난 것을 고르고, 원문을 다시 읽어 붙인다.
맥북에서 Codex 로 같은 입력을 판정해 Opus 결과(stored)와 비교한다(소유자 결정 2026-11-25).
결과 파일은 공개 웹 정보(행사 정보·원문)와 판정만 담는다.

  python collector/sample_dump.py --n 30 --since 2026-11-23T12:20:00 --out eval/judge_sample.json
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import Entry, fetch_detail, new_client  # noqa: E402
from recommend_backfill import SHOW, dump  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REC = ("rec_level", "bot", "category", "rec_score", "special_basis", "rec_blockers", "push_blocked", "title_ko", "summary_ko", "special_reason")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--since", default="2026-11-23T12:20:00", help="이 시각(UTC) 이후 판정된 행사만(추천 규칙 개정 이후)")
    ap.add_argument("--out", default=str(ROOT / "eval/judge_sample.json"))
    a = ap.parse_args(argv)
    api = WorkerAPI.from_env()
    today = datetime.now(ZoneInfo("America/Denver")).date().isoformat()
    events = []
    for bot in ("life", "family", "kids"):
        events += [e for e in dump(api, bot) if e.get("rec_level") and str(e.get("checked_at") or "") >= a.since]
    seen, uniq = set(), []
    for e in events:
        if e["event_key"] not in seen and max(str(e.get(k) or "") for k in ("end_date", "start_date", "registration_opens", "deadline")) >= today:
            seen.add(e["event_key"])
            uniq.append(e)
    uniq.sort(key=lambda x: x["event_key"])
    # 수준이 고르게 섞이게: push 를 먼저 절반까지, 나머지는 interest·none
    push = [e for e in uniq if e["rec_level"] == "push"][:a.n // 2]
    rest = [e for e in uniq if e["rec_level"] != "push"][:a.n - len(push)]
    sample = push + rest

    def page(e):
        x = Entry(e.get("source_id") or "sample", e["url"], e.get("title") or "")
        try:
            fetch_detail(x, hc, chars=3000)
        except Exception:  # noqa: BLE001
            x.text = ""
        return x.text or ""

    with new_client() as hc, ThreadPoolExecutor(8) as ex:
        texts = list(ex.map(page, sample))
    rows = [{"event_key": e["event_key"], "event": {k: e.get(k) for k in SHOW}, "source_text": t, "stored": {k: e.get(k) for k in REC}}
            for e, t in zip(sample, texts)]
    Path(a.out).write_text(json.dumps({"today": today, "since": a.since, "pool": len(uniq), "n": len(rows), "rows": rows},
                                      ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"pool": len(uniq), "n": len(rows), "push": len(push), "with_text": sum(bool(t) for t in texts)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
