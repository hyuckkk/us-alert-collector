"""개발용 AI 예산 관리(운영비와 별도). 기록: eval/dev_ai_spend.jsonl (실행마다 한 줄, 실패·중단 실행 포함).

python scripts/dev_budget.py check --cap 5       # 남은 예산으로 이번 실행 상한을 정해 출력. 남은 돈이 부족하면 종료 코드 1(실행 안 함)
python scripts/dev_budget.py record --run ID --kind fullsystem --cap 5 --usage reports/usage-live.json --status success
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "eval/dev_ai_spend.jsonl"
TOTAL = 40.0          # 승인된 개발 예산 총액(지금까지 사용분 포함)
PER_RUN = 5.0         # 1회 상한
MIN_RUN = 1.0         # 남은 돈이 이보다 적으면 실행하지 않음


def spent() -> float:
    if not LEDGER.exists():
        return 0.0
    return round(sum(float(json.loads(l).get("usd") or 0) for l in LEDGER.read_text().splitlines() if l.strip()), 4)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("op", choices=["check", "record"])
    ap.add_argument("--cap", type=float, default=PER_RUN)
    ap.add_argument("--run")
    ap.add_argument("--kind", default="fullsystem")
    ap.add_argument("--usage")
    ap.add_argument("--status", default="unknown")
    a = ap.parse_args(argv)
    used = spent()
    left = round(TOTAL - used, 4)
    if a.op == "check":
        cap = round(min(a.cap, PER_RUN, left), 2)
        if cap < MIN_RUN:
            print(f"::error title=dev-budget::개발 예산 소진 — 사용 ${used} / 총 ${TOTAL}, 남은 ${left}. 실행하지 않음")
            return 1
        print(f"::notice title=dev-budget::사용 ${used} / 총 ${TOTAL}, 남은 ${left} → 이번 실행 상한 ${cap}")
        print(f"CAP={cap}")
        return 0
    u = {}
    if a.usage and Path(a.usage).exists():
        u = json.loads(Path(a.usage).read_text())
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run": a.run, "kind": a.kind, "status": a.status,
           "usd": round(float(u.get("usd", 0)), 4), "cap": a.cap, "calls": u.get("calls"), "usage_recorded": bool(u)}
    with LEDGER.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"::notice title=dev-budget::이번 실행 ${row['usd']} ({a.status}) — 누적 ${round(used + row['usd'], 4)} / ${TOTAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
