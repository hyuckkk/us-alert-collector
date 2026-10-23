"""누적 기록 내보내기(이슈 #20): Worker /admin/export 결과를 JSON·CSV·Markdown 으로 저장한다.

  US_WORKER_URL·US_ADMIN_KEY 환경 변수 필요(관리 워크플로가 비밀값에서 만든다). 출력 폴더 기본값 eval/issue20/.
  python scripts/export_eval.py [--since 2026-10-01] [--out eval/issue20] [--from-json export.json]
결과: export.json(전체), events.csv(행사별 3경로 대조), workload.csv(실행별 작업량·비용), gaps.csv(누락 원인), summary.md(요약).
텔레그램으로 보내지 않는다.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FINDERS = ("github_collector", "claude", "chatgpt")
ID = re.compile(r"(?<!\d)-?\d{9,}(?!\d)")  # 텔레그램 ID 같은 긴 숫자는 공개 저장소·실행 기록에 남기지 않는다


def fetch(since: str) -> dict:
    req = urllib.request.Request(f"{os.environ['US_WORKER_URL']}/admin/export?since={since}",
                                 headers={"authorization": f"Bearer {os.environ['US_ADMIN_KEY']}", "user-agent": "us-alert-export"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def flat(d, prefix=""):
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flat(v, key + "."))
        elif isinstance(v, list):
            out[key] = len(v) if k not in ("origin",) else ",".join(map(str, v))
        else:
            out[key] = v
    return out


def redact(v, k=None):
    """문자열 안의 긴 숫자(텔레그램 ID 형태)와 ID 필드만 가린다. 토큰 수 같은 숫자 값은 그대로 둔다."""
    if isinstance(v, dict):
        return {kk: redact(vv, kk) for kk, vv in v.items()}
    if isinstance(v, list):
        return [redact(vv) for vv in v]
    if k in ("chat_id", "from_id", "user_id") and v is not None:
        return "<id>"
    return ID.sub("<id>", v) if isinstance(v, str) else v


def write_csv(path: Path, rows: list[dict]) -> None:
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols or ["empty"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def build(x: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    x = redact(x)
    (out / "export.json").write_text(json.dumps(x, ensure_ascii=False, indent=1) + "\n")
    events = x["compare"]["events"]
    ev_rows = []
    for e in events:
        r = {"event_key": e["event_key"], "title": e.get("title"), "bot": e.get("bot"), "final_level": e.get("final_level"),
             "special_reason": e.get("special_reason"), "first_finder": e.get("first_finder"), "sent_at": e.get("sent_at"), "sent_via": e.get("sent_via"),
             "gap_found": (e.get("gap") or {}).get("found"), "cause_code": (e.get("gap") or {}).get("cause_code"),
             "rediscovered": (e.get("gap") or {}).get("rediscovered")}
        for f in FINDERS:
            fd = e["finds"].get(f) or {}
            r.update({f"{f}.first_seen": fd.get("first_seen"), f"{f}.found_at": fd.get("found_at"), f"{f}.url": fd.get("url"),
                      f"{f}.verified": fd.get("verified"), f"{f}.rec_level": fd.get("rec_level"), f"{f}.ext_level": fd.get("ext_level")})
        ev_rows.append(r)
    write_csv(out / "events.csv", ev_rows)
    write_csv(out / "workload.csv", [flat(w) for w in x["workload"]])
    write_csv(out / "gaps.csv", x["gaps"])
    s = x["compare"]["summary"]
    by_path = {p: [w for w in x["workload"] if w.get("path") == p] for p in FINDERS}

    def cost(ws):
        tot = 0.0
        for w in ws:
            v = w.get("total_usd") if w.get("path") == "github_collector" else (w.get("recheck_cost_usd") or {}).get("total")
            tot += v if isinstance(v, (int, float)) else 0
        return round(tot, 2)
    push = Counter(e["first_finder"] for e in events if e.get("final_level") == "push")
    md = [f"# 3경로 대조·작업량 누적 ({x['compare']['since']} ~ {x['generated_at'][:10]})", "",
          "| 경로 | 찾은 행사 | 단독 발견 | 최초 발견 | 적극 알림 대상(최초 발견 기준) | 발송을 일으킴 | 실행 수 | API 비용(재확인 포함) |", "|---|---|---|---|---|---|---|---|"]
    for p in FINDERS:
        md.append(f"| {p} | {s['by_finder'].get(p, 0)} | {s['only'].get(p, 0)} | {s['first'].get(p, 0)} | {push.get(p, 0)} | {s['sent_via'].get(p, 0)} | "
                  f"{len(by_path[p])} | ${cost(by_path[p])} |")
    md += ["", f"- 행사 {s['events']}건, 겹침: " + (", ".join(f"{k} {v}" for k, v in s["overlap"].items()) or "없음"),
           "- 외부 검색(Claude·ChatGPT) 자체 비용은 구독 사용량이라 API 비용이 아니다(not_applicable). 표의 비용은 우리 쪽 재확인(추출·판정·추천) API 비용이다.",
           "- 누락 원인: " + (", ".join(f"{k} {v}" for k, v in sorted(s["causes"].items(), key=lambda kv: -kv[1])) or "없음"),
           f"- 재발견 확인: 예 {sum(1 for g in x['gaps'] if g.get('rediscovered') == 1)} · 아니오 {sum(1 for g in x['gaps'] if g.get('rediscovered') == 0)} · 확인 못 함 "
           f"{sum(1 for g in x['gaps'] if g.get('rediscovered') is None and g.get('found') not in ('found',))}",
           f"- 자동 추가 정보원 {len(x.get('sources', []))}개 · 검색어 {len(x.get('queries', []))}개 · 연속 실패 정보원 {len(x.get('failing_sources', []))}개",
           "", "## 월별 API 사용액", "", "| 월 | 모델 | 호출 | 웹 검색 | 사용액 |", "|---|---|---|---|---|"]
    md += [f"| {u['month']} | {u['model']} | {u['calls']} | {u.get('searches', 0)} | ${u['usd']:.2f} |" for u in x["usage"]]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    return {"events": len(events), "workload_runs": len(x["workload"]), "gaps": len(x["gaps"]), "out": str(out)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-01")
    ap.add_argument("--out", default=str(ROOT / "eval/issue20"))
    ap.add_argument("--from-json")
    a = ap.parse_args(argv)
    x = json.loads(Path(a.from_json).read_text()) if a.from_json else fetch(a.since)
    print(json.dumps(build(x, Path(a.out)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
