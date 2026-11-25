"""맥북 추천 판정기: 추천 대기(await_rec) 항목을 Codex(ChatGPT 구독)로 판정해 Worker 에 등록한다.

소유자 결정(2026-11-25): API 크레딧 대신 남는 ChatGPT 구독 사용량으로 추천 판정. 수집 실행(GitHub)은 추출·정밀 판정까지 하고
추천 대기로 넘기며, 이 판정기가 맥북에서 30분마다 처리한다. 맥북이 꺼져 있어 오래(fallback_minutes) 남은 것은 GitHub 실행이
Claude 로 대신 판정한다(run.py rec_fallback).

필요한 환경 변수(맥북 실행 스크립트가 키체인에서 넣는다): US_WORKER_URL, REC_RUNNER_KEY(/rec/ 경로 전용 키). 관리 키는 쓰지 않는다.
공개 저장소라 이 판정기를 GitHub Actions 에서 돌리지 않는다(ChatGPT 로그인 정보를 공개 실행 환경에 두지 않음).

  python collector/rec_runner.py [--max-rounds 5]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rounds", type=int, default=5, help="한 번 실행에서 꺼내 처리하는 최대 횟수(회당 최대 60건)")
    a = ap.parse_args(argv)
    cfg = json.loads((ROOT / "config/models.json").read_text())
    api = WorkerAPI.rec_runner_from_env()
    if api.dry_run:
        print("US_WORKER_URL / REC_RUNNER_KEY 가 없어 멈춤", file=sys.stderr)
        return 2
    today = datetime.now(ZoneInfo("America/Denver")).date().isoformat()
    total = {"rounds": 0, "rows": 0, "events": 0, "missing_rows": 0, "levels": {}}
    for _ in range(max(a.max_rounds, 1)):
        rows = api.queue_take_rec(60, lease_minutes=60)
        if not rows:
            break
        out = ai.process_await_rec(api, rows, lambda items: ai.recommend_codex(cfg, items, today), found_by="mac_codex")
        total["rounds"] += 1
        for k in ("rows", "events", "missing_rows"):
            total[k] += out[k]
        for k, v in out["levels"].items():
            total["levels"][k] = total["levels"].get(k, 0) + v
        if out["missing_rows"] == out["rows"]:
            break  # 이번 묶음이 전부 실패(사용 한도 등): 더 꺼내지 않고 다음 실행에서 다시
    print(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **total}, ensure_ascii=False))
    return 1 if total["rows"] and total["missing_rows"] == total["rows"] else 0


if __name__ == "__main__":
    sys.exit(main())
