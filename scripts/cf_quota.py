"""시험 전 Cloudflare 무료 한도 점검(4차 검증 1). 계정 전체(한국 Worker·D1 포함)가 함께 쓰는 하루 한도에서
오늘(UTC) 이미 쓴 양 + 이번 시험 예상치가 안전선(기본 80%)을 넘으면 시험을 시작하지 않는다.

무료 한도(2026-10 기준 Cloudflare 문서): Workers 요청 100,000/일(계정 공유), D1 읽은 행 5,000,000/일, 쓴 행 100,000/일,
D1 데이터베이스 10개/계정. 2026-09-01부터 D1 행 한도를 넘으면 그날 UTC 자정까지 계정의 모든 D1 쿼리가 실패한다.
사용량을 잴 수 없으면(분석 권한 없음 등) '안전'으로 치지 않고 멈춘다(fail-closed).

python scripts/cf_quota.py --writes 25000 --reads 500000 --requests 1000 --dbs 1
환경: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID. 종료 코드 0 안전 / 1 한도 위험 / 2 측정 불가
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

LIMITS = {"requests": 100_000, "rows_read": 5_000_000, "rows_written": 100_000, "databases": 10}
API = "https://api.cloudflare.com/client/v4"
QUERY = """query($acct: String!, $d: Date!, $t0: Time!, $t1: Time!) { viewer { accounts(filter: {accountTag: $acct}) {
  d1: d1AnalyticsAdaptiveGroups(limit: 10000, filter: {date_geq: $d, date_leq: $d}) { sum { rowsRead rowsWritten } }
  w: workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $t0, datetime_lt: $t1}) { sum { requests } }
} } }"""


def _call(path: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode() if body else None,
                                 headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def measure(token: str, acct: str, now: datetime) -> dict:
    d = now.strftime("%Y-%m-%d")
    g = _call("graphql", token, {"query": QUERY, "variables": {"acct": acct, "d": d, "t0": f"{d}T00:00:00Z", "t1": now.strftime("%Y-%m-%dT%H:%M:%SZ")}})
    if g.get("errors") or not (g.get("data") or {}).get("viewer"):
        raise RuntimeError(f"analytics: {str(g.get('errors'))[:300]}")
    a = g["data"]["viewer"]["accounts"][0]
    dbs = _call(f"accounts/{acct}/d1/database?per_page=100", token)
    if not dbs.get("success"):
        raise RuntimeError(f"d1 list: {str(dbs.get('errors'))[:300]}")
    return {
        "requests": sum(x["sum"]["requests"] for x in a["w"]),
        "rows_read": sum(x["sum"]["rowsRead"] for x in a["d1"]),
        "rows_written": sum(x["sum"]["rowsWritten"] for x in a["d1"]),
        "databases": len(dbs["result"]),
    }


USAGE_QUERY = """query($acct: String!, $d: Date!, $t0: Time!, $t1: Time!, $db: String!, $w: String!) { viewer { accounts(filter: {accountTag: $acct}) {
  d1: d1AnalyticsAdaptiveGroups(limit: 10000, filter: {date_geq: $d, date_leq: $d, databaseId: $db}) { sum { rowsRead rowsWritten readQueries writeQueries } }
  w: workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $t0, datetime_lt: $t1, scriptName: $w}) { sum { requests errors subrequests } }
} } }"""


def usage_of(token: str, acct: str, now: datetime, db_id: str, worker: str) -> dict:
    """이번 시험이 만든 D1 하나·Worker 하나만의 오늘 사용량(분석 반영에 몇 분 걸릴 수 있음)."""
    d = now.strftime("%Y-%m-%d")
    g = _call("graphql", token, {"query": USAGE_QUERY, "variables": {"acct": acct, "d": d, "t0": f"{d}T00:00:00Z",
                                                                     "t1": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "db": db_id, "w": worker}})
    if g.get("errors") or not (g.get("data") or {}).get("viewer"):
        raise RuntimeError(f"analytics: {str(g.get('errors'))[:300]}")
    a = g["data"]["viewer"]["accounts"][0]
    tot = lambda rows, k: sum(x["sum"][k] for x in rows)  # noqa: E731
    return {"d1_rows_read": tot(a["d1"], "rowsRead"), "d1_rows_written": tot(a["d1"], "rowsWritten"),
            "d1_read_queries": tot(a["d1"], "readQueries"), "d1_write_queries": tot(a["d1"], "writeQueries"),
            "worker_requests": tot(a["w"], "requests"), "worker_errors": tot(a["w"], "errors"), "worker_subrequests": tot(a["w"], "subrequests")}


def decide(used: dict, need: dict, safety: float = 0.8) -> list[str]:
    """한도를 넘을 항목 목록(비어 있으면 안전). databases 는 개수 한도(안전선 없이 남은 칸)."""
    bad = []
    for k, lim in LIMITS.items():
        cap = lim if k == "databases" else int(lim * safety)
        if used[k] + need.get(k, 0) > cap:
            bad.append(f"{k}: 오늘 {used[k]:,} + 시험 {need.get(k, 0):,} > 안전선 {cap:,} (한도 {lim:,})")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--writes", type=int, default=25_000)
    ap.add_argument("--reads", type=int, default=500_000)
    ap.add_argument("--requests", type=int, default=1_000)
    ap.add_argument("--dbs", type=int, default=1)
    ap.add_argument("--safety", type=float, default=0.8)
    ap.add_argument("--usage-db", help="보고 모드: 이 D1 ID 와 --usage-worker 의 사용량 + 계정 전체 사용량을 출력(점검 아님)")
    ap.add_argument("--usage-worker")
    ap.add_argument("--wait", type=int, default=0, help="보고 모드: 분석 값이 안정될 때까지 최대 기다릴 초")
    a = ap.parse_args(argv)
    token, acct = os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not (token and acct):
        print("::error title=quota::CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID 없음")
        return 2
    if a.usage_db:
        import time
        prev, waited, cur = None, 0, None
        try:
            while True:
                cur = usage_of(token, acct, datetime.now(timezone.utc), a.usage_db, a.usage_worker or "")
                if prev is not None and cur == prev and cur["d1_rows_written"] > 0:
                    break
                if waited >= a.wait:
                    break
                prev = cur
                time.sleep(60)
                waited += 60
            acct_used = measure(token, acct, datetime.now(timezone.utc))
        except Exception as e:  # noqa: BLE001
            print(f"::warning title=usage::사용량 조회 실패 {type(e).__name__}: {str(e)[:200]}")
            return 2
        room = {k: {"used": acct_used[k], "limit": LIMITS[k], "pct": round(100 * acct_used[k] / LIMITS[k], 1)} for k in LIMITS}
        print(f"::notice title=usage this run (waited {waited}s, stable={cur == prev})::{json.dumps(cur)}")
        print(f"::notice title=usage account today UTC::{json.dumps(room)}")
        return 0
    try:
        used = measure(token, acct, datetime.now(timezone.utc))
    except Exception as e:  # noqa: BLE001
        print(f"::error title=quota::사용량을 잴 수 없어 시험을 시작하지 않습니다(계정 공유 한도 보호). 토큰에 Account Analytics 읽기 권한이 필요할 수 있습니다. {type(e).__name__}: {str(e)[:200]}")
        return 2
    need = {"requests": a.requests, "rows_read": a.reads, "rows_written": a.writes, "databases": a.dbs}
    print(f"::notice title=quota pre-check::오늘(UTC) 계정 사용량 {json.dumps(used)} · 시험 예상 {json.dumps(need)} · 안전선 {int(a.safety * 100)}%")
    bad = decide(used, need, a.safety)
    for b in bad:
        print(f"::error title=quota::{b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
