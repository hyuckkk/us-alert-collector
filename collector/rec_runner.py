"""맥북 Codex 판정기: ChatGPT 구독 사용량으로 미국 알림의 AI 단계를 처리한다.

소유자 결정(2026-11-25):
- 추천 판정: 수집 실행(GitHub)이 넘긴 '추천 대기(await_rec)' 행사를 Codex 로 판정해 Worker 에 등록한다.
- 추출·정밀 판정: API 크레딧이 없는 동안(12/7 무렵까지) 아직 추출 안 된 대기 항목(queued)도 맥북이 맡는다.
  수집 코드(run.py process_queue)를 그대로 쓰고, AI 호출만 Codex 대역(ai.CodexClient)으로 바꿔 끼운다.
  크레딧이 돌아오면 config models.json 의 recommend_codex.mac_extract 를 false 로 바꿔 추출은 GitHub(Claude Haiku)로 되돌린다.
- 맥북이 꺼져 오래(fallback_minutes) 남은 추천 대기는 GitHub 실행이 Claude 로 대신 판정한다(run.py rec_fallback).

필요한 환경 변수(맥북 실행 스크립트가 키체인에서 넣는다): US_WORKER_URL, REC_RUNNER_KEY(/rec/ 경로 전용 키). 관리 키는 쓰지 않는다.
공개 저장소라 이 판정기를 GitHub Actions 에서 돌리지 않는다(ChatGPT 로그인 정보를 공개 실행 환경에 두지 않음).

  python collector/rec_runner.py [--max-rounds 5] [--extract-max 60]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def mac_models(cfg: dict) -> dict:
    """맥북에서 쓸 설정: 추천은 바로(추천 대기로 넘기지 않음), 배치 없음, Codex 호출 수를 줄이려고 묶음을 크게."""
    c = cfg["recommend_codex"]
    out = {**cfg, "recommend_codex": {**c, "enabled": False}}
    out["extract"] = {**cfg["extract"], "batch": c.get("extract_batch", 15), "parallel": c.get("parallel", 2), "reserve_usd": 0}
    for st in ("judge", "recommend"):
        out[st] = {**cfg[st], "batch": c.get("batch", 30), "reserve_usd": 0, "batch_wait_min": None}
    return out


def extract_queued(cfg: dict, api: WorkerAPI, client, limit: int) -> dict:
    """추출 전 대기 항목을 수집 실행과 같은 코드로 처리(상세 조회 → 추출 → 정밀 판정 → 같은 행사 판별 → 추천 → 등록)."""
    import run
    os.environ.setdefault("ANTHROPIC_API_KEY", "codex-subscription")  # run.ai_ready 가 키 유무를 본다(실제 호출은 Codex)
    args = SimpleNamespace(mode="mac", dry_run=False, no_ai=False, only=None, max=limit, out=None, all_sources=False, usd_cap=None, trace=None)
    r = run.Run(args, client=client, api=api)
    r.models = mac_models(r.models)
    r.stage("ai", r.process_queue)
    r.stage("enqueue", r.enqueue_events)
    keep = ("ai_items", "ai_failed", "ai_deferred", "events_out", "outbox", "rec_push", "rec_interest", "rec_none", "detail_missing")
    return {"stages": {k: {kk: vv for kk, vv in v.items() if kk in ("ok", "taken", "events", "error", "seconds")} for k, v in r.stages.items()},
            "metrics": {k: r.m.get(k, 0) for k in keep}, "alerts": r.alerts[-5:]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rounds", type=int, default=5, help="추천 대기를 꺼내 처리하는 최대 횟수(회당 최대 60건)")
    ap.add_argument("--extract-max", type=int, default=60, help="한 번 실행에서 추출까지 맡을 대기 항목 수(0 이면 추출 안 함)")
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
    report = {"at": datetime.now().isoformat(timespec="seconds"), "await_rec": total}
    failed = bool(total["rows"]) and total["missing_rows"] == total["rows"]
    if cfg["recommend_codex"].get("mac_extract") and a.extract_max > 0 and not failed:
        client = ai.CodexClient(cfg["recommend_codex"])
        try:
            report["queued"] = extract_queued(cfg, api, client, a.extract_max)
        finally:
            client.close()
        q = report["queued"]
        failed = failed or (q["metrics"]["ai_failed"] > 0 and q["metrics"]["ai_items"] == 0)
    print(json.dumps(report, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
