"""규칙 처리(토큰 0): AI 가 뽑은 행사를 검증·정리한다.

- 링크 환각 방지: 행사 url 은 그 항목을 수집할 때 받은 주소 중 하나여야 한다. 아니면 수집 주소로 바꾼다.
- 날짜 검증: YYYY-MM-DD 가 아니면 버리고, 이미 끝난 행사는 뺀다.
- 유급 활동(F-2 취업 불가)은 뺀다.
- 긴급도: 마감 7일 이내 / 선착순 접수가 7일 안에 열림 → 즉시(now), 그 외 주간 요약(weekly).
- 행사 키: 영문 제목 + 시작일 + 도시로 만든 짧은 해시. 같은 행사가 여러 정보원에서 와도 한 건이 된다.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URGENT_DAYS = 7


def _d(s: str | None) -> date | None:
    if not s or not DATE.match(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def entry_key(e) -> str:
    return hashlib.sha1(f"{e.source_id}|{e.url}|{e.title if '#' in e.url else ''}".encode()).hexdigest()[:20]


def entry_hash(e) -> str:
    if e.extra.get("page_hash"):
        return e.extra["page_hash"]
    return hashlib.sha1(f"{e.title}|{e.text}|{e.published}".encode()).hexdigest()[:12]


def event_key(title: str, start: str | None, city: str | None) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    norm = re.sub(r"\b(the|a|an|20\d\d)\b", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return "e" + hashlib.sha1(f"{norm}|{start or ''}|{(city or '').lower()}".encode()).hexdigest()[:15]


def urgency(ev: dict, today: date) -> str:
    dl = _d(ev.get("deadline"))
    if dl and today <= dl <= today + timedelta(days=URGENT_DAYS):
        return "now"
    opens = _d(ev.get("registration_opens"))
    if ev.get("signup") in ("fcfs", "lottery") and opens and today <= opens <= today + timedelta(days=URGENT_DAYS):
        return "now"
    return "weekly"


def year_label(ev: dict, today: date) -> str | None:
    s = _d(ev.get("start_date"))
    if s and s.year > today.year:
        return f"{s.year}년 행사"
    if ev.get("annual"):
        return "연례행사 — 내년에도 열릴 가능성"
    return None


def finalize(extracted, entries: list, today: date, checked_at: str | None = None) -> tuple[list[dict], dict]:
    """extracted: ai.EntryResult 목록(index 는 entries 기준). 반환: (보낼 행사 목록, 버린 이유별 건수)."""
    checked_at = checked_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    dropped: dict[str, int] = {}
    drop = lambda why: dropped.__setitem__(why, dropped.get(why, 0) + 1)  # noqa: E731
    out: dict[str, dict] = {}
    for r in extracted:
        if not r.relevant:
            drop("not-relevant")
            continue
        entry = entries[r.index]
        allowed = {entry.url}
        for ev in r.events:
            d = ev.model_dump()
            if d["url"] not in allowed:
                drop("url-replaced")  # 버리지는 않고 수집 주소로 바꾼다
                d["url"] = entry.url
            for k in ("start_date", "end_date", "deadline", "registration_opens"):
                if d.get(k) and not _d(d[k]):
                    d[k] = None
            if d["paid_work"]:
                drop("paid-work")
                continue
            last = _d(d["end_date"]) or _d(d["start_date"])
            if last and last < today:
                drop("past")
                continue
            if not d["start_date"] and not d["annual"] and not d.get("registration_opens") and not d.get("deadline"):
                # 날짜가 전혀 없는 항목은 생활정보로만 주간 요약에 넣는다
                d["tags"] = list(dict.fromkeys([*d["tags"], "info"]))[:5]
            d["event_key"] = event_key(d["title"], d["start_date"], d["city"])
            d["urgency"] = urgency(d, today)
            d["year_label"] = year_label(d, today)
            d["source_id"] = entry.source_id
            d["checked_at"] = checked_at
            d["_entry_index"] = r.index
            prev = out.get(d["event_key"])
            if prev:
                drop("duplicate")
                # 공식 정보원 쪽을 남긴다(검색 결과보다 우선)
                if prev["source_id"].startswith("gnews") and not d["source_id"].startswith("gnews"):
                    out[d["event_key"]] = d
                continue
            out[d["event_key"]] = d
    return list(out.values()), dropped


def apply_verdicts(events: list[dict], review_idx: list[int], verdicts) -> tuple[list[dict], int]:
    """큰 모델 판단을 반영. review_idx[i] = verdict.index i 가 가리키는 events 위치."""
    remove = set()
    for v in verdicts:
        if not (0 <= v.index < len(review_idx)):
            continue
        ev = events[review_idx[v.index]]
        if not v.keep:
            remove.add(review_idx[v.index])
            continue
        ev.update({"bot": v.bot, "age_text": v.age_text or ev.get("age_text"), "status_note": v.status_note or ev.get("status_note"),
                   "english": v.english or ev.get("english", []), "review_reason": v.reason})
    return [e for i, e in enumerate(events) if i not in remove], len(remove)


def for_worker(ev: dict) -> dict:
    """Worker 로 보내는 필드만 남긴다."""
    keep = ("event_key", "bot", "title", "title_ko", "org", "start_date", "end_date", "time_text", "event_tz", "location", "city",
            "age_text", "english", "cost_text", "signup", "deadline", "status_note", "url", "checked_at", "urgency", "tags", "annual",
            "year_label", "source_id", "registration_opens")
    return {k: ev.get(k) for k in keep}
