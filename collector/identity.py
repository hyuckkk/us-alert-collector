"""행사 식별(1차 검증 1-1): 날짜·장소·제목 일부가 바뀌어도 같은 행사는 같은 키, 다른 연도·회차·기관은 다른 키.

키 = 기준(anchor) + 연도 + 회차.
- 기준: ① 정보원이 준 고유 ID(iCal UID 등) ② 공식 정보원의 행사 상세 주소 ③ 주최기관 + 정규화한 제목(유니코드 그대로, 한글 포함)
  날짜·장소는 기준에 넣지 않는다 → 일정·장소가 바뀌어도 키 유지.
- 연도: 시작일(없으면 접수 시작일·마감일)의 연도 → 2027년 개최와 2028년 개최 구분.
- 회차: 같은 기준에서 한 번에 여러 행사가 나오면(같은 페이지의 여러 회차) 날짜 순 번호, 제목 기준 행사가 기존 것과 45일 넘게 떨어지면 시작 월.
- 기존 행사와 대조: 기준이 달라도(제목 일부 수정, 주소 변경, 다른 사이트 중복 게시) 같은 연도이고
  제목 낱말이 충분히 겹치면서 주최기관·시작일·주소 중 하나가 맞으면 기존 키를 쓴다.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import urllib.parse
from datetime import date

STOP = {"the", "a", "an", "and", "of", "for", "at", "in", "on", "to", "with", "by", "annual", "event", "events"}
WORD = re.compile(r"\w+", re.UNICODE)
GENERIC_PATH = re.compile(r"^/?(events?|calendar|programs?|classes|news|index(\.\w+)?)?/?$", re.I)
SAME_EVENT_JACCARD = 0.6
OCCURRENCE_GAP_DAYS = 45


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    words = [w for w in WORD.findall(s) if w not in STOP and not re.fullmatch(r"(19|20)\d\d", w) and not re.fullmatch(r"\d+(st|nd|rd|th)", w)]
    return " ".join(words)


def tokens(s: str | None) -> set[str]:
    return set(norm(s).split())


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _url_anchor(url: str) -> str | None:
    p = urllib.parse.urlsplit(url)
    if GENERIC_PATH.match(p.path) and not p.query:
        return None  # 목록 페이지 자체는 행사를 가리키지 않음
    q = urllib.parse.urlencode(sorted((k, v) for k, v in urllib.parse.parse_qsl(p.query) if re.search(r"id|event|eid|sid", k, re.I)))
    host = p.netloc.lower().removeprefix("www.")
    return f"url:{host}{p.path.rstrip('/')}" + (f"?{q}" if q else "")


def anchor(ev: dict, entry=None, official: bool = False) -> str:
    extra = getattr(entry, "extra", {}) or {}
    if extra.get("uid"):
        return f"uid:{entry.source_id}:{extra['uid']}"
    if official and ev.get("url"):
        a = _url_anchor(ev["url"])
        if a:
            return a
    return f"title:{norm(ev.get('org'))}|{norm(ev.get('title'))}"


def year_of(ev: dict) -> str:
    for k in ("start_date", "registration_opens", "deadline"):
        v = ev.get(k)
        if v and re.match(r"^\d{4}-", v):
            return v[:4]
    return "any"


def make_key(anchor_s: str, year: str, occurrence: str = "") -> str:
    return "e" + hashlib.sha1(f"{anchor_s}|{year}|{occurrence}".encode()).hexdigest()[:15]


def _days(a: str | None, b: str | None) -> int | None:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return None


def _host(url: str | None) -> str:
    return urllib.parse.urlsplit(url or "").netloc.lower().removeprefix("www.")


def _norm_url(u: str) -> str:
    p = urllib.parse.urlsplit(u.strip())
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}?{p.query}"


def same_event(ev: dict, other: dict, strict_date: bool = False) -> bool:
    """기준이 다른 두 행사가 같은 행사인지(제목 일부 수정·주소 변경·다른 사이트 중복).
    strict_date: 같은 페이지에 회차가 여럿인 행사는 시작일까지 맞아야 같은 것으로 본다."""
    if year_of(ev) != year_of(other) and "any" not in (year_of(ev), year_of(other)):
        return False
    j = jaccard(tokens(ev.get("title")), tokens(other.get("title")))
    if j < SAME_EVENT_JACCARD:
        return False
    oa, ob = norm(ev.get("org")), norm(other.get("org"))
    if oa and ob and oa not in ob and ob not in oa:
        return False  # 주최기관이 서로 다르면 같은 이름이어도 다른 행사
    org_match = bool(oa) and bool(ob)
    d = _days(ev.get("start_date"), other.get("start_date"))
    date_match = d is not None and d <= 1
    if d is not None and d > 1 and ev.get("url") and other.get("url") and _norm_url(ev["url"]) != _norm_url(other["url"]):
        return False  # 다른 페이지 + 다른 날짜 = 다른 행사(10차 시험: 무료입장일 11/7·12/8 이 합쳐짐). 같은 페이지의 날짜 변경은 위 조건에 안 걸림
    host_match = _host(ev.get("url")) == _host(other.get("url"))
    if strict_date and not date_match:
        return False
    if not (org_match or date_match or host_match):
        return False
    # 같은 제목이라도 시작일이 멀면 다른 회차
    return d is None or d <= OCCURRENCE_GAP_DAYS


def assign_keys(events: list[dict], index: list[dict]) -> dict:
    """events: rules.finalize 결과(각각 _anchor 포함). index: Worker 의 최근 행사 목록.
    events 에 event_key·identity 를 채운다. 반환: 통계."""
    stats = {"exact": 0, "matched": 0, "new": 0, "occurrence": 0, "merged_in_run": 0}
    by_key = {x["event_key"]: x for x in index}
    # 같은 기준이 한 번에 여러 개면 회차 번호(날짜 순)
    groups: dict[tuple, list[dict]] = {}
    for ev in events:
        groups.setdefault((ev["_anchor"], year_of(ev)), []).append(ev)
    for (anc, yr), evs in groups.items():
        evs.sort(key=lambda e: e.get("start_date") or "9999")
        for i, ev in enumerate(evs):
            occ = str(i + 1) if len(evs) > 1 else ""
            ev["identity"] = f"{anc}|{yr}|{occ}"
            ev["event_key"] = make_key(anc, yr, occ)

    sizes = {k: len(v) for k, v in groups.items()}
    done: list[dict] = []
    for ev in events:
        strict = sizes[(ev["_anchor"], year_of(ev))] > 1
        key = ev["event_key"]
        if key in by_key:
            prev = by_key[key]
            d = _days(ev.get("start_date"), prev.get("start_date"))
            if ev["_anchor"].startswith("title:") and d is not None and d > OCCURRENCE_GAP_DAYS:
                # 제목 기준 행사가 기존 것과 날짜가 많이 다르면 다른 회차
                ev["event_key"] = make_key(ev["_anchor"], year_of(ev), "@" + (ev.get("start_date") or "")[:7])
                ev["identity"] += "@" + (ev.get("start_date") or "")[:7]
                stats["occurrence"] += 1
            else:
                stats["exact"] += 1
        else:
            match = next((x for x in index if same_event(ev, x, strict)), None)
            if match:
                ev["event_key"] = match["event_key"]
                stats["matched"] += 1
            else:
                stats["new"] += 1
        # 같은 실행 안에서 다른 사이트에 중복 게시된 것
        twin = next((x for x in done if x["event_key"] != ev["event_key"] and x["_anchor"] != ev["_anchor"] and same_event(ev, x, strict)), None)
        if twin:
            ev["event_key"] = twin["event_key"]
            stats["merged_in_run"] += 1
        done.append(ev)
    return stats


def collapse_same(events: list[dict]) -> tuple[list[dict], int]:
    """키를 정하기 전에 같은 행사(같은 기준 + 같은 제목 + 같은 날짜들)를 하나로. 같은 페이지의 여러 행사에 회차 번호를 매길 때
    중복이 서로 다른 회차로 잡히는 것을 막는다."""
    out: dict[tuple, dict] = {}
    for ev in events:
        k = (ev.get("_anchor"), norm(ev.get("title")), ev.get("start_date"), ev.get("registration_opens"), ev.get("deadline"))
        out.setdefault(k, ev)
    return list(out.values()), len(events) - len(out)


def dedupe(events: list[dict]) -> tuple[list[dict], int]:
    """같은 키가 여러 번이면 공식 정보원 쪽 하나만 남긴다(검색 결과·발굴보다 우선)."""
    rank = lambda e: (str(e.get("source_id", "")).startswith(("gnews", "ai-discover")), e.get("_entry_index", 0))  # noqa: E731
    out: dict[str, dict] = {}
    for ev in sorted(events, key=rank):
        out.setdefault(ev["event_key"], ev)
    return list(out.values()), len(events) - len(out)
