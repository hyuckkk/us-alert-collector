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


YEAR_CONFIRM_DAYS = 180
CLOSED_MARK = re.compile(r"\b(?:rsvps?|registrations?|sign-?ups?|enrollment)\s+(?:is\s+|are\s+|now\s+)?closed\b|\bcurrently\s+full\b|\bsold\s+out\b|\bat\s+capacity\b", re.I)
WAITLIST_MARK = re.compile(r"\bwait\s?-?list\b", re.I)


def _year_in_text(ev: dict, entry, today: date) -> bool:
    """추정 연도의 날짜가 오늘부터 180일 안이고, 그 연도가 같은 원문(제목·본문)에 적혀 있으면 True."""
    text = f"{entry.title or ''} {entry.text or ''}"
    keys = [_d(ev.get(k)) for k in ("registration_opens", "start_date", "deadline", "end_date")]
    keys = [k for k in keys if k]
    return bool(keys) and all(0 <= (k - today).days <= YEAR_CONFIRM_DAYS and re.search(rf"\b{k.year}\b", text) for k in keys)


def finalize(extracted, entries: list, today: date, checked_at: str | None = None, sources: dict | None = None) -> tuple[list[dict], dict]:
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
            if d.get("year_basis") == "assumed" and _year_in_text(d, entry, today):
                d["year_basis"] = "published"  # 13차: 같은 글에 그 연도가 적혀 있고 가까운 날짜면 연도 확정(Thornton '11/24 접수 시작' 이 ⏰ 알림을 못 받음)
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
            if (len(r.events) == 1 and not entry.extra.get("page_hash") and d.get("registration_status") in ("open", "unknown", None)
                    and CLOSED_MARK.search(entry.text or "")):
                # 단일 행사 상세 페이지에 마감 표시가 있으면 확정(시험 F·G: 'Currently full RSVP Closed' 를 AI 가 unknown 으로 둠). 대기자 명단은 보냄
                d["registration_status"] = "waitlist" if WAITLIST_MARK.search(entry.text or "") else "closed"
            if d.get("registration_status") in ("closed", "soldout") and "page_fp" not in entry.extra:  # 이미 보낸 행사의 재검사(상세 변경)는 '마감' 변경 알림으로 보낸다
                drop("closed")  # 신청할 수 없는 새 행사는 보내지 않는다(대기자 명단 waitlist 는 보냄)
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
            sports_normalize(d, entry, (sources or {}).get(entry.source_id))
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


# ---------------------------------------------------------------- 경기 달력 정규화(이슈 #27·#43, 토큰 0)
# Codex 달력 페이지와의 자료 계약: docs/SPORTS-CONTRACT.md
SPORTS_FIELDS = ("school_level", "level_detail", "sport", "event_type", "home_team", "away_team", "participants", "start_time", "venue", "cost_class")
LEVELS = ("elementary", "middle", "high", "university")
VS_RE = re.compile(r"^(.*?)\s+(?:vs\.?|v\.|versus)\s+(.+)$", re.I)
AT_RE = re.compile(r"^(.*?)\s+(?:at|@)\s+(.+)$", re.I)
MEET_RE = re.compile(r"\b(meet|invitational|tri-?meet|triangular|quad(?:-meet)?|dual|championships?|regionals?|classic|cup)\b", re.I)
SPORT_RE = re.compile(r"\b(gymnastics|hockey|soccer|football|baseball|basketball|volleyball|softball|tennis|wrestling|lacrosse|rugby|swimming|"
                      r"water polo|field hockey|cross country|track and field|track & field|futsal|golf|skiing|diving)\b", re.I)
ICS_TIME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})\d{2}(Z?)$")
FREE_WORDS = re.compile(r"(^|[\s,(])(free|무료|no admission|no charge)(?=[\s.,)]|$)", re.I)
PAID_WORDS = re.compile(r"\$\s*[1-9]|유료|tickets? (?:required|start)", re.I)


def cost_class(cost: str | None) -> str:
    """관중 입장료 분류. 무료라고 적힌 경우만 free(참가비·등록비는 cost_text 에 넣지 않는다)."""
    s = str(cost or "").strip()
    if not s:
        return "unknown"
    if FREE_WORDS.search(s) and not PAID_WORDS.search(s):
        return "free"
    return "paid" if PAID_WORDS.search(s) else "unknown"


def _ics_local_time(published: str | None) -> str | None:
    m = ICS_TIME.match(str(published or ""))
    if not m:
        return None
    y, mo, d, hh, mm, z = m.groups()
    t = datetime(int(y), int(mo), int(d), int(hh), int(mm), tzinfo=timezone.utc if z else None)
    if z:
        from zoneinfo import ZoneInfo
        t = t.astimezone(ZoneInfo("America/Denver"))
    return None if (t.hour, t.minute) == (0, 0) else f"{t.hour:02d}:{t.minute:02d}"   # 자정 = 종일 일정(시각 미상)


def sports_normalize(d: dict, entry=None, source: dict | None = None) -> dict:
    """경기·대회 행사에 달력 필드를 채운다. 경기가 아니면 그대로 둔다.
    - school_level: 정보원 설정(sources.json school_level)이 모델 판단보다 우선
    - event_type: A vs B / A at B → match, 다자 대회 명칭(meet·invitational·quad·championship) → meet
    - start_time: iCal 시작 시각을 덴버 현지 시각으로(모델 추출보다 정확)
    - cost_class: 관중 입장료(무료라고 적힌 경우만 free)"""
    source = source or {}
    title = str(d.get("title") or "")
    sport = (d.get("sport") or source.get("sport") or "").strip().lower() or None
    m = SPORT_RE.search(title)
    if not sport and m:
        sport = m.group(1).lower().replace(" & ", " and ")
    if not sport:
        return d
    d["sport"] = sport
    lv = source.get("school_level") or d.get("school_level")
    d["school_level"] = lv if lv in LEVELS else None
    if source.get("level_detail") and not d.get("level_detail"):
        d["level_detail"] = source["level_detail"]
    et = d.get("event_type")
    if not et:
        if d.get("home_team") and d.get("away_team"):
            et = "match"
        elif MEET_RE.search(title):
            et = "meet"
        elif VS_RE.match(title) or AT_RE.match(title):
            et = "match"
    d["event_type"] = et
    if et == "match" and not (d.get("home_team") and d.get("away_team")):
        mv, ma = VS_RE.match(title), AT_RE.match(title)
        if mv:
            d["home_team"], d["away_team"] = mv.group(1).strip(), mv.group(2).strip()
        elif ma and not MEET_RE.search(ma.group(2)):
            d["home_team"], d["away_team"] = ma.group(2).strip(), ma.group(1).strip()
    if et == "meet":
        d["home_team"] = d["away_team"] = None   # 다자 대회를 1:1 경기로 꾸미지 않는다
    d["participants"] = list(dict.fromkeys(p.strip() for p in (d.get("participants") or []) if p and p.strip()))[:24]
    t = _ics_local_time(entry.published) if entry is not None and "uid" in entry.extra else None   # iCal 항목만
    if t:
        d["start_time"] = t
    elif d.get("start_time") and not re.match(r"^\d{2}:\d{2}$", str(d["start_time"])):
        d["start_time"] = None
    d["venue"] = d.get("location")
    d["cost_class"] = cost_class(d.get("cost_text"))
    return d


def calendar_title_ko(ev: dict) -> None:
    """한국어 제목에 '경기'를 붙여 달력 판별(sports.js: 체육 정보원 + match|game|경기)이 다자 대회(meet)를 놓치지 않게 한다.
    추천 판정이 title_ko 를 다시 쓰므로 Worker 로 보내기 직전에 적용한다."""
    if ev.get("sport") and ev.get("event_type") in ("match", "meet", "tournament"):
        t = str(ev.get("title_ko") or ev.get("title") or "")
        if t and not re.search(r"경기|대회|match|game", t, re.I):
            ev["title_ko"] = (t + (" 경기" if ev["event_type"] == "match" else " 대회 경기"))[:60]


def for_worker(ev: dict) -> dict:
    """Worker 로 보내는 필드만 남긴다."""
    calendar_title_ko(ev)
    keep = ("event_key", "identity", "bot", "title", "title_ko", "org", "start_date", "end_date", "time_text", "event_tz", "location", "city",
            "age_text", "english", "cost_text", "signup", "registration_opens", "registration_status", "deadline", "eligibility", "status_note",
            "url", "checked_at", "urgency", "tags", "annual", "year_label", "source_id", "date_uncertain",
            "rec_level", "category", "rec_score", "summary_ko", "highlights", "rec_reason", "special_reason", "found_by", "found_at", "verified", "ext_level", "run_id",
            "special_basis", "evidence_quote", "usual_alternative", "rec_blockers", "push_blocked",
            "hold_reason", "topic", "foreign_access", "access_note", "family_water", "water_note", "fee_note",
            *SPORTS_FIELDS)
    return {k: ev.get(k) for k in keep}
