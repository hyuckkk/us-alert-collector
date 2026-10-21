"""시험 전후 운영 자원 무변경 확인(6차 승인 조건). Cloudflare 계정의 Worker·D1 목록 '메타데이터'만 읽는다
(이름, ID, 수정 시각, 테이블 수 — DB 내용·Worker 코드는 읽지 않음). 시험 자원(-stg-)은 비교에서 뺀다.

python scripts/cf_snapshot.py save before.json
python scripts/cf_snapshot.py compare before.json     # 지금 상태와 비교, 결과를 주석으로 출력
조회 실패는 경고만 남기고 0 으로 끝낸다(시험·정리 흐름을 막지 않음). 비교에서 달라진 운영 자원이 있으면 오류 주석 + 종료 코드 1.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

API = "https://api.cloudflare.com/client/v4"


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{API}/{path}", headers={"authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def snapshot() -> dict:
    acct = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    w = _get(f"accounts/{acct}/workers/scripts")
    d = _get(f"accounts/{acct}/d1/database?per_page=100")
    if not (w.get("success") and d.get("success")):
        raise RuntimeError(f"list failed: {str(w.get('errors'))[:120]} {str(d.get('errors'))[:120]}")
    return {
        "workers": {x["id"]: x.get("modified_on") for x in w["result"] if "-stg-" not in x["id"]},
        "d1": {x["name"]: {"uuid": x.get("uuid"), "num_tables": x.get("num_tables"), "version": x.get("version")}
               for x in d["result"] if "-stg-" not in x["name"]},
    }


def main(argv: list[str]) -> int:
    op, path = argv[1], argv[2]
    try:
        now = snapshot()
    except Exception as e:  # noqa: BLE001
        print(f"::warning title=prod-snapshot::{op} 조회 실패 {type(e).__name__}: {str(e)[:160]}")
        return 0
    if op == "save":
        json.dump(now, open(path, "w"))
        print(f"::notice title=prod-snapshot before::Worker {len(now['workers'])}개, D1 {len(now['d1'])}개(시험 자원 제외) 기록")
        return 0
    try:
        before = json.load(open(path))
    except Exception as e:  # noqa: BLE001
        print(f"::warning title=prod-snapshot::시험 전 기록 없음({type(e).__name__}) — 비교 불가")
        return 0
    diff = []
    for kind in ("workers", "d1"):
        for k in sorted(set(before[kind]) | set(now[kind])):
            if before[kind].get(k) != now[kind].get(k):
                diff.append(f"{kind}:{k} {before[kind].get(k)} → {now[kind].get(k)}")
    if diff:
        print(f"::error title=prod-snapshot changed::{'; '.join(diff)[:900]}")
        return 1
    print(f"::notice title=prod-snapshot unchanged::Worker {len(now['workers'])}개(수정 시각 동일), D1 {len(now['d1'])}개(ID·테이블 수 동일) — 시험 전후 변화 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
