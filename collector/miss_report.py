"""경로별 누락 보고(매일): GitHub 수집기·Claude 독립 검색·ChatGPT 검색이 각각 무엇을 못 찾았는지 소유자에게 알리고,
못 찾은 이유를 바탕으로 다음 검색 범위를 넓힌다.

- 대상: 최근 26시간 안에 처음 등록된 '적극 알림' 행사(발송 대상인 것만. 우리 기준으로 알리지 않는 행사는 놓쳐도 누락이 아님).
- 그 시간 안에 실제로 돈 경로(작업량 기록이 있는 경로)만 평가한다. 돌지 않은 경로는 "실행 기록 없음"으로 따로 적는다.
- GitHub 수집기가 놓친 것: 외부 입력 때 이미 원인 분석·정보원/검색어 자동 추가·재발견 확인을 했다(ext_gaps). 그 결과를 보고한다.
- Claude·ChatGPT 가 놓친 것: 큰 모델이 이유와 보완(검색어·감시할 공식 페이지)을 정하고 external-inbox/LESSONS.md 에 쌓는다.
  두 검색은 매번 이 파일을 먼저 읽고 검색 범위에 넣는다(지시 파일에 명시).
- 텔레그램 보고는 소유자에게만, 하루 한 번, 누락이 있을 때만 보낸다. 지인에게는 보내지 않는다. 출처 표시는 보고에만 있고 행사 알림에는 없다.

  python collector/miss_report.py [--hours 26] [--lessons external-inbox/LESSONS.md] [--no-send]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PATHS = ("github_collector", "claude", "chatgpt")
NAME = {"github_collector": "GitHub 수집기", "claude": "Claude 검색", "chatgpt": "ChatGPT 검색"}
FIX_KO = {"github_collector": "정보원·검색어 자동 보완", "claude": "다음 Claude 검색 범위에 반영", "chatgpt": "다음 ChatGPT 검색 범위에 반영"}
MAX_LESSONS = 80


# ---------------------------------------------------------------- Claude 지난 제출과 대조(이미 올린 행사를 'Claude 누락'으로 적지 않게)
def norm_url(u: str | None) -> str | None:
    """비교용 주소: 호스트(www. 제거, 소문자)+경로(끝 / 제거). %2E 같은 인코딩은 풀고, news.google 경유는 원 주소로 바꾼다.
    원 주소를 알 수 없는 news.google 기사 주소는 None(비교하지 않음)."""
    if not u:
        return None
    u = str(u).strip()
    for _ in range(3):  # 이중 인코딩까지
        d = unquote(u)
        if d == u:
            break
        u = d
    pr = urlparse(u if "://" in u else "https://" + u)
    host = (pr.hostname or "").lower().removeprefix("www.")
    if host == "news.google.com":
        q = parse_qs(pr.query).get("url")
        return norm_url(q[0]) if q else None
    if not host:
        return None
    path = re.sub(r"/+", "/", pr.path or "/").rstrip("/").lower()
    return host + path


def _range(start, end):
    s = (str(start)[:10] if start else None) or None
    e = (str(end)[:10] if end else None) or s
    return s, e


def same_event(urls: list[str | None], start, end, sub: dict) -> bool:
    """같은 행사 판정: 공식 주소(호스트+경로)가 같고 날짜 범위가 겹치면 같다. 묶음 행사(같은 기관 같은 페이지에 여러 날짜)도
    날짜가 제출 범위 안이면 같은 행사로 본다. 어느 한쪽 날짜를 모르면 주소만으로 판정한다."""
    keys = {k for k in (norm_url(u) for u in urls) if k}
    if not keys or sub.get("key") not in keys:
        return False
    s1, e1 = _range(start, end)
    s2, e2 = sub.get("start"), sub.get("end")
    if not s1 or not s2:
        return True
    return s1 <= (e2 or s2) and s2 <= (e1 or s1)


def _sub_rows(doc: dict) -> list[dict]:
    out = []
    for e in (doc.get("events") or []) if isinstance(doc, dict) else []:
        k = norm_url(e.get("url"))
        if k:
            s, en = _range(e.get("start_date"), e.get("end_date"))
            out.append({"key": k, "start": s, "end": en, "title": e.get("title")})
    return out


def claude_submissions(root: Path = ROOT) -> list[dict]:
    """Claude 의 지난 제출 전체: 작업 트리의 external-inbox/claude/*.json + origin/claude/search-* 브랜치의 같은 폴더 파일."""
    rows, seen = [], set()
    for f in sorted((root / "external-inbox/claude").glob("*.json")):
        try:
            rows += _sub_rows(json.loads(f.read_text()))
            seen.add(f.name)
        except (OSError, ValueError):
            pass

    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=60)

    refs = git("for-each-ref", "--format=%(refname)", "refs/remotes/origin/claude/").stdout.split()
    for ref in (r for r in refs if "/claude/search-" in r):
        for name in git("ls-tree", "-r", "--name-only", ref, "--", "external-inbox/claude/").stdout.split():
            if not name.endswith(".json") or Path(name).name in seen:
                continue
            seen.add(Path(name).name)
            try:
                rows += _sub_rows(json.loads(git("show", f"{ref}:{name}").stdout))
            except ValueError:
                pass
    return rows


def claude_already(e: dict, subs: list[dict]) -> bool:
    urls = [f.get("url") for f in (e.get("finds") or {}).values()]
    return any(same_event(urls, e.get("start_date"), e.get("end_date"), s) for s in subs)


def compute(compare: dict, workload: list[dict], since: str, claude_subs: list[dict] | None = None) -> dict:
    """경로별 누락 계산. 반환: {ran: {path: bool}, total, found: {path: n}, missed: {path: [event]}}
    claude_subs 가 있으면 Claude 가 (다른 주소·다른 시각에) 이미 제출한 행사는 Claude 가 찾은 것으로 센다."""
    ran = {p: any(w.get("path") == p and str(w.get("started_at") or "") >= since for w in workload) for p in PATHS}
    evs = [e for e in compare.get("events", []) if e.get("final_level") == "push"
           and min(f.get("found_at") or f.get("first_seen") or "9" for f in e["finds"].values()) >= since]
    subs = claude_subs or []

    def has(p, e):
        return p in e["finds"] or (p == "claude" and bool(subs) and claude_already(e, subs))

    found = {p: sum(1 for e in evs if has(p, e)) for p in PATHS}
    missed = {p: [e for e in evs if ran[p] and not has(p, e)] for p in PATHS}
    return {"ran": ran, "total": len(evs), "found": found, "missed": missed}


def lessons_for(misses: list[tuple[str, dict]], client, models: dict, usage) -> list[dict]:
    """Claude·ChatGPT 가 놓친 행사마다 이유와 보완(검색어·감시 페이지)을 정한다."""
    items = [{"index": i, "title": e.get("title"), "url": next(iter(e["finds"].values())).get("url"), "found": f"missed by {p}",
              "start_date": None, "org": None, "description": f"Found by {', '.join(e['finds'])}; not found by {p}'s own web search.",
              "our_history": f"{p} web search did not report it"} for i, (p, e) in enumerate(misses)]
    fixes = ai.gap_analysis(client, models, items, [], usage) if items else {}
    out = []
    for i, (p, e) in enumerate(misses):
        g = fixes.get(i)
        url = items[i]["url"] or ""
        out.append({"path": p, "title": e.get("title") or "", "host": urlparse(url).netloc, "url": url,
                    "found_by": sorted(e["finds"]), "cause": g.cause_ko.strip() if g else "원인 분석 실패",
                    "queries": [q.strip() for q in (g.queries if g else []) if q.strip()][:3],
                    "monitor_url": g.monitor_url if g and g.monitor_url else None})
    return out


def _paths(x: dict) -> list[str]:
    return list(x.get("paths") or [x["path"]])


def _urls(x: dict) -> list[str]:
    return [u for u in dict.fromkeys(list(x.get("urls") or []) + [x.get("url")]) if u]


def merge_lessons(rows: list[dict]) -> list[dict]:
    """감시할 공식 페이지(monitor_url, 없으면 행사 주소) 기준으로 중복을 하나로 합친다. 최신 날짜·원인을 남기고
    누락 경로와 검색어는 합친다. 순서는 각 묶음의 가장 최근 항목 위치."""
    groups: dict[str, dict] = {}
    for x in rows:
        k = norm_url(x.get("monitor_url")) or norm_url(x.get("url")) or f"{x.get('path')}|{x.get('url')}"
        g = groups.pop(k, None)
        if g is None:
            g = dict(x, paths=_paths(x), queries=list(x.get("queries") or []), urls=_urls(x))
        else:
            ps = g["paths"] + [p for p in _paths(x) if p not in g["paths"]]
            qs = list(dict.fromkeys(list(x.get("queries") or []) + g["queries"]))[:5]
            us = list(dict.fromkeys(g["urls"] + _urls(x)))[-10:]
            g = dict(x, paths=ps, queries=qs, urls=us) if (x.get("date") or "") >= (g.get("date") or "") else dict(g, paths=ps, queries=qs, urls=us)
        g["path"] = g["paths"][0]
        groups[k] = g  # 다시 넣어 '가장 최근' 위치로
    return list(groups.values())


def prune_claude(rows: list[dict], subs: list[dict]) -> list[dict]:
    """Claude 가 이미 올린(또는 나중에 다시 찾은) 행사는 Claude 누락 교훈에서 지운다. 다른 경로 누락은 남긴다."""
    if not subs:
        return rows
    keys = {s["key"] for s in subs}
    out = []
    for x in rows:
        ps = _paths(x)
        if "claude" in ps and any(norm_url(u) in keys for u in _urls(x)):  # 행사 주소 기준(감시 페이지는 일반 달력일 수 있어 쓰지 않음)
            ps = [p for p in ps if p != "claude"]
            if not ps:
                continue
            x = dict(x, paths=ps, path=ps[0])
        out.append(x)
    return out


def write_lessons(path: Path, new: list[dict], today: str, claude_subs: list[dict] | None = None) -> int:
    """external-inbox/LESSONS.md 에 쌓는다(최근 MAX_LESSONS 개). 사람이 읽고 두 검색이 그대로 따를 수 있는 형식.
    같은 감시 페이지는 한 항목으로 합치고, Claude 가 이미 올린 항목은 지운다. 반환: 새로 생긴 항목 수."""
    old = []
    if path.exists():
        old = [json.loads(m) for m in re.findall(r"<!-- lesson (\{.*?\}) -->", path.read_text())]
    seen = {(p, x["url"]) for x in old for p in _paths(x)}
    added = [dict(x, date=today) for x in new if (x["path"], x["url"]) not in seen]
    before = len(merge_lessons(prune_claude(old, claude_subs or [])))
    rows = merge_lessons(prune_claude(old + added, claude_subs or []))[-MAX_LESSONS:]
    lines = ["# 검색 누락 교훈 (자동 갱신)", "",
             "Claude·ChatGPT 독립 검색은 매번 검색 전에 이 파일을 읽고, 아래 검색어와 감시 페이지를 검색 범위에 넣는다.",
             "이 목록은 다른 경로가 찾아서 실제로 알린 '적극 알림' 행사를 그 경로가 못 찾았을 때 쌓인다. GitHub 수집기의 누락은 정보원·검색어로 자동 보완된다.",
             "같은 감시 페이지는 한 항목으로 합치고, Claude 가 이미 올렸거나 다시 찾은 행사는 Claude 누락에서 자동으로 지운다.", ""]
    for x in reversed(rows):
        who = "·".join(NAME.get(p, p).replace(" 검색", "") for p in _paths(x)) + " 검색" if len(_paths(x)) > 1 else NAME.get(x["path"], x["path"])
        lines.append(f"- {x['date']} · **{who} 누락** · {x['title']} ({x['host']}) — {x['cause']}")
        if x.get("queries"):
            lines.append(f"  - 검색어: {' / '.join(x['queries'])}")
        if x.get("monitor_url"):
            lines.append(f"  - 감시할 공식 페이지: {x['monitor_url']}")
        lines.append(f"  <!-- lesson {json.dumps(x, ensure_ascii=False)} -->")
    path.write_text("\n".join(lines) + "\n")
    return max(0, len(rows) - before) if added else 0


def render(r: dict, gaps: dict, lessons: list[dict], day: str) -> str | None:
    if not any(r["missed"][p] for p in PATHS):
        return None
    lines = [f"🔎 검색 경로 대조 ({day}, 최근 하루 적극 알림 {r['total']}건)",
             " · ".join(f"{NAME[p]} {r['found'][p]}건" if r["ran"][p] else f"{NAME[p]} 실행 기록 없음" for p in PATHS), ""]
    for p in PATHS:
        ms = r["missed"][p]
        if not ms:
            continue
        lines.append(f"• {NAME[p]} 누락 {len(ms)}건 → {FIX_KO[p]}")
        for e in ms[:5]:
            title = (e.get("title") or e["event_key"])[:40]
            if p == "github_collector":
                g = gaps.get(e["event_key"]) or e.get("gap") or {}
                why = (g.get("cause") or "원인 미확인")[:60]
                again = {1: " · 보완 후 다시 잡힘", 0: " · 보완 후에도 안 잡힘(정보원 확인 필요)"}.get(g.get("rediscovered"), "")
                lines.append(f"  - {title}: {why}{again}")
            else:
                lz = next((x for x in lessons if x["path"] == p and x["title"] == (e.get("title") or "")), None)
                lines.append(f"  - {title}: {(lz['cause'] if lz else '원인 분석 대기')[:60]}")
        if len(ms) > 5:
            lines.append(f"  - 외 {len(ms) - 5}건")
    return "\n".join(lines)[:3800]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=26)
    ap.add_argument("--lessons", default=str(ROOT / "external-inbox/LESSONS.md"))
    ap.add_argument("--no-send", action="store_true")
    a = ap.parse_args(argv)
    api = WorkerAPI.from_env()
    since = (datetime.now(timezone.utc) - timedelta(hours=a.hours)).isoformat(timespec="seconds")
    day = datetime.now(timezone.utc).date().isoformat()
    compare = api._get(f"/admin/compare?since={since[:10]}")
    workload = api._get(f"/admin/workload?since={since[:10]}").get("rows", [])
    subs = claude_submissions()
    r = compute(compare, workload, since, subs)
    gaps = {g["event_key"]: g for g in api._get("/admin/gaps").get("gaps", [])}
    models = json.loads((ROOT / "config/models.json").read_text())
    usage = ai.Usage(models.get("prices", {}))
    ext_misses = [(p, e) for p in ("claude", "chatgpt") for e in r["missed"][p]]
    lessons = lessons_for(ext_misses, ai.get_client(), models, usage) if ext_misses else []
    # 누락이 없는 날에도 실행해, Claude 가 다시 찾은 항목을 지우고 중복을 합친다
    added = write_lessons(Path(a.lessons), lessons, day, subs)
    text = render(r, gaps, lessons, day)
    sent = None
    if text and not a.no_send:
        sent = api._post("/admin/miss-report", {"key": day, "text": text}).get("sent")
    summary = {"since": since, "ran": r["ran"], "total_push": r["total"], "found": r["found"], "missed": {p: len(r["missed"][p]) for p in PATHS},
               "lessons_added": added, "claude_submissions": len(subs), "report_sent": sent, "usd": round(usage.usd, 4)}
    if usage.by_model:
        api.add_usage(usage.by_model, float(models.get("limits", {}).get("credit_reference_usd", 0)))
    print("::notice title=miss report::" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
