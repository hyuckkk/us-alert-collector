"""학교·대학 경기 달력 누락 진단(이슈 #27·#43, 읽기 전용, 토큰 0).

운영 DB 의 행사(/admin/events-dump, 수신자 정보 없음)를 읽어 Worker sports.js 와 같은 기준으로
정보원별·학교급별·종목별 건수와 달력 판별 통과 여부를 센다. 무엇도 쓰거나 지우지 않는다.

  python collector/sports_diag.py --out eval/sports_diag.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# worker/src/sports.js 와 같은 규칙(2026-10-25 main 기준). 비교 진단용 사본이며 Worker 동작을 바꾸지 않는다
SPORTS = re.compile(r"\b(hockey|soccer|football|baseball|basketball|volleyball|softball|tennis|wrestling|lacrosse|rugby|gymnastics|swimming|water polo|field hockey|cross.country|track.and.field|futsal|badminton)\b|농구|배구|야구|축구|아이스하키|하키|풋볼|미식축구|레슬링|테니스|수영|체조|육상", re.I)
VS = re.compile(r"\b(?:vs\.?|v\.|versus|against)\b|(?:\s+@\s+)|\s+대\s+|대항전", re.I)
ATHLETIC_SOURCE = re.compile(r"athlet|sport.?club|club.?sports|soccer|hockey|high.?school.?sport|school.?athlet|varsity|^cu-.*club", re.I)
UNIVERSITY = re.compile(r"cu-|boulder|university.of.denver|du-athlet|csu-|colorado.state|mines-|air.force|afa-|university|college|sport.?club", re.I)
GRADE = {"elementary": re.compile(r"elementary|초등|grade.?school|little.league|u[6-9]\b", re.I),
         "middle": re.compile(r"middle.?school|junior.?high|중학", re.I),
         "high": re.compile(r"high.?school|prep.?school|고등|varsity|jv\b", re.I)}
GAME = re.compile(r"match|game|대항|경기", re.I)
MEET = re.compile(r"\b(meet|invitational|tri-?meet|quad|triangular|dual|championships?|regionals?|classic)\b", re.I)


def grade(e: dict) -> str:
    if str(e.get("school_level") or "") in ("elementary", "middle", "high", "university"):
        return e["school_level"]
    hint = " ".join(str(x) for x in [e.get("school_level"), e.get("org"), e.get("source_id"), e.get("title")] if x)
    for lv, rx in GRADE.items():
        if rx.search(hint):
            return lv
    return "university" if UNIVERSITY.search(hint) else "unknown"


def calendar_match(e: dict) -> bool:
    src = str(e.get("source_id") or "")
    title = " ".join(x for x in [e.get("title"), e.get("title_ko")] if x)
    tags = " ".join(e.get("tags") or [])
    g = grade(e)
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", str(e.get("start_date") or ""))
                and (ATHLETIC_SOURCE.search(src) or e.get("category") == "sports" or g != "unknown")
                and (SPORTS.search(title + " " + tags) or ATHLETIC_SOURCE.search(src))
                and (VS.search(title) or (e.get("home_team") and e.get("away_team")) or (GAME.search(title) and ATHLETIC_SOURCE.search(src))))


def sport(e: dict) -> str:
    m = SPORTS.search(" ".join(str(x) for x in [e.get("title"), e.get("title_ko"), " ".join(e.get("tags") or [])] if x))
    return m.group(0).lower() if m else "?"


def dump(api: WorkerAPI) -> list[dict]:
    out, off = [], 0
    while True:
        page = api._get(f"/admin/events-dump?bot=&sent=0&offset={off}")["events"]
        out += page
        if len(page) < 300:
            return out
        off += 300


def analyse(events: list[dict], today: str) -> dict:
    sportsy = [e for e in events if SPORTS.search(" ".join(str(x) for x in [e.get("title"), e.get("title_ko"), " ".join(e.get("tags") or [])] if x))
               or e.get("category") == "sports"]
    future = [e for e in sportsy if str(e.get("start_date") or "") >= today]
    cal = [e for e in future if calendar_match(e)]
    miss = [e for e in future if not calendar_match(e)]
    gym = [e for e in sportsy if re.search(r"gymnast|체조", json.dumps([e.get("title"), e.get("title_ko"), e.get("tags")], ensure_ascii=False), re.I)]
    row = lambda e: {k: e.get(k) for k in ("start_date", "title", "source_id", "url", "rec_level", "category")} | {"grade": grade(e), "calendar": calendar_match(e)}  # noqa: E731
    return {
        "today": today, "events_total": len(events), "sports_like": len(sportsy), "sports_like_future": len(future),
        "calendar_future": len(cal),
        "calendar_by_grade": dict(Counter(grade(e) for e in cal)),
        "calendar_by_sport": dict(Counter(sport(e) for e in cal).most_common()),
        "calendar_by_source": dict(Counter(e.get("source_id") for e in cal).most_common()),
        "sports_future_by_source": dict(Counter(e.get("source_id") for e in future).most_common()),
        "not_calendar_future_by_reason": dict(Counter(
            "no-vs-or-game" if not (VS.search(e.get("title") or "") or GAME.search(e.get("title") or "")) else "grade-or-source" for e in miss)),
        "not_calendar_meet_like": [row(e) for e in miss if MEET.search(e.get("title") or "")][:40],
        "not_calendar_samples": [row(e) for e in miss][:60],
        "gymnastics_all": [row(e) for e in gym][:60],
        "non_university_any": [row(e) for e in sportsy if grade(e) in ("elementary", "middle", "high")][:60],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "eval/sports_diag.json"))
    a = ap.parse_args(argv)
    r = analyse(dump(WorkerAPI.from_env()), date.today().isoformat())
    Path(a.out).write_text(json.dumps(r, ensure_ascii=False, indent=1) + "\n")
    print("::notice title=sports diag::" + json.dumps({k: r[k] for k in ("events_total", "sports_like", "sports_like_future", "calendar_future", "calendar_by_grade")}, ensure_ascii=False))
    print("::notice title=sports diag gym::" + json.dumps({"gymnastics": len(r["gymnastics_all"]), "non_university": len(r["non_university_any"]), "not_calendar": r["not_calendar_future_by_reason"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
