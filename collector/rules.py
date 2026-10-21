"""규칙 처리(토큰 0): AI 가 뽑은 행사를 검증·정리한다.

- 링크 환각 방지: 행사 url 은 그 항목을 수집할 때 받은 주소 중 하나여야 한다. 아니면 수집 주소로 바꾼다.
- 날짜 검증: YYYY-MM-DD 가 아니면 버리고, 이미 끝난 행사는 뺀다.
- 유급 활동(취업이 허용되지 않는 체류 신분)은 뺀다.
- 긴급도: 마감 7일 이내 / 선착순·추첨 접수가 7일 안에 열림 / 취소·연기 → 즉시(now), 그 외 요약(weekly).
- 행사 키는 identity.py 가 정한다(여기서는 식별 기준 _anchor 만 붙인다).
- 처리 우선순위(토큰 0): AI 로 넘기기 전, 접수·마감 관련 낱말과 가까운 날짜가 보이면 먼저 처리한다.
"""
from __future__ import annotations

import hashlib
import re

import identity
from datetime import date, datetime, timedelta, timezone

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URGENT_DAYS = 7
OLD_POST_DAYS = 120  # 연도를 추정한 날짜인데 게시일이 이보다 오래면 지난 행사 글로 본다


def _d(s: str | None) -> date | None:
    if not s or not DATE.match(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def published_date(s: str | None) -> date | None:
    """RSS(RFC 822)·ISO 형식 게시일을 날짜로. 모르면 None."""
    if not s:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError, IndexError):
        pass
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", s)
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except ValueError:
        return None


def entry_key(e) -> str:
    return hashlib.sha1(f"{e.source_id}|{e.url}|{e.title if '#' in e.url else ''}".encode()).hexdigest()[:20]


def entry_hash(e) -> str:
    if e.extra.get("page_hash"):
        return e.extra["page_hash"]
    return hashlib.sha1(f"{e.title}|{e.text}|{e.published}".encode()).hexdigest()[:12]


MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
REG_WORDS = re.compile(r"regist|sign[- ]?up|enroll|deadline|apply|application|lottery|limited|spots|waitlist|sold out|tickets? on sale|opens?\b|last chance", re.I)
DATE_MD = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)
DATE_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")


def near_dates(text: str, today: date, days: int = 14) -> int:
    """본문에 오늘부터 days 일 안의 날짜가 몇 번 보이는지(연도 없는 '월 일'은 다가오는 날짜로 본다)."""
    n = 0
    for m, d in DATE_MD.findall(text or ""):
        try:
            cand = date(today.year, MONTHS[m[:3].lower()], int(d))
        except (ValueError, KeyError):
            continue
        if cand < today:
            cand = cand.replace(year=today.year + 1)
        n += cand <= today + timedelta(days=days)
    for y, mo, d in DATE_ISO.findall(text or ""):
        try:
            n += today <= date(int(y), int(mo), int(d)) <= today + timedelta(days=days)
        except ValueError:
            pass
    return n


def priority(entry, today: date, official: bool) -> int:
    """대기열 처리 순서 점수(높을수록 먼저). 정확한 마감일은 AI 추출 전에는 모르므로 낱말·날짜로 추정한다."""
    text = f"{entry.title} {entry.text or ''}"
    score = 3 if official else 0
    if REG_WORDS.search(text):
        score += 3
    score += min(near_dates(text, today), 3) * 2
    if entry.extra.get("hint"):
        score += 4  # 이미 알려진 행사의 상세 페이지 변경
    return score


def urgency(ev: dict, today: date) -> str:
    dl = _d(ev.get("deadline"))
    if dl and today <= dl <= today + timedelta(days=URGENT_DAYS):
        return "now"
    opens = _d(ev.get("registration_opens"))
    if ev.get("signup") in ("fcfs", "lottery") and opens and today <= opens <= today + timedelta(days=URGENT_DAYS):
        return "now"
    if ev.get("registration_status") in ("cancelled", "postponed"):
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
    """extracted: ai.EntryResult 목록(index 는 entries 기준). 반환: (행사 목록, 버린 이유별 건수).
    행사 키는 아직 없다(identity.assign_keys 가 기존 행사 목록과 대조해 정한다)."""
    checked_at = checked_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    dropped: dict[str, int] = {}
    drop = lambda why: dropped.__setitem__(why, dropped.get(why, 0) + 1)  # noqa: E731
    out: list[dict] = []
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
            # 연도가 원문에 없어 추정한 날짜(2차 재검증 H): 오래된 게시글이면 버리고, 아니면 '연도 추정'으로 표시하고 급하게 알리지 않는다
            if d.get("year_basis") == "assumed":
                pub = published_date(entry.published)
                if pub and (today - pub).days > OLD_POST_DAYS:
                    drop("old-post")
                    continue
                d["date_uncertain"] = True
                d["status_note"] = "; ".join(x for x in [d.get("status_note"), "날짜의 연도는 추정 — 원문 확인 필요"] if x)
            last = _d(d["end_date"]) or _d(d["start_date"])
            if last and last < today:
                drop("past")
                continue
            if d.get("registration_status") in ("closed", "soldout"):
                drop("closed")  # 신청할 수 없는 행사는 보내지 않는다(대기자 명단 waitlist 는 보냄)
                continue
            if not d["start_date"] and not d["end_date"] and not d["annual"] and not d.get("registration_opens") and not d.get("deadline"):
                # 날짜·접수일이 전혀 없으면 지난 행사인지 알 수 없으므로 보내지 않는다(10차 시험: 지난 행사 3건 오발송).
                # 같은 페이지가 바뀌면(상세 재검사·페이지 변경 감시) 다시 추출된다.
                drop("no-date")
                continue
            d["urgency"] = "weekly" if d.get("date_uncertain") else urgency(d, today)
            d["year_label"] = year_label(d, today)
            d["source_id"] = entry.source_id
            d["checked_at"] = checked_at
            d["_entry_index"] = r.index
            d["_anchor"] = identity.anchor(d, entry, official=bool(entry.extra.get("official")))
            out.append(d)
    return out, dropped


def apply_verdicts(events: list[dict], review_idx: list[int], verdicts) -> tuple[list[dict], int]:
    """큰 모델 판단을 반영. review_idx[i] = verdict.index i 가 가리키는 events 위치. verdicts 는 목록 또는 {index: Verdict}.
    판정이 없는 행사는 여기서 다루지 않는다(호출하는 쪽이 보류)."""
    remove = set()
    for v in (verdicts.values() if isinstance(verdicts, dict) else verdicts):
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
    keep = ("event_key", "identity", "bot", "title", "title_ko", "org", "start_date", "end_date", "time_text", "event_tz", "location", "city",
            "age_text", "english", "cost_text", "signup", "registration_opens", "registration_status", "deadline", "eligibility", "status_note",
            "url", "checked_at", "urgency", "tags", "annual", "year_label", "source_id", "date_uncertain")
    return {k: ev.get(k) for k in keep}
