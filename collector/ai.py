"""AI 호출을 이 파일 한 곳에 모은다(설계안: 모델·경로 전환이 설정 변경으로 끝나게).

- check_key: 키 유효성(모델 목록 조회, 토큰 0)
- extract:   새 항목 묶음 → 행사 정보 추출 (작은 모델, config/models.json 의 extract)
- judge:     자격·신분·연령이 애매한 건만 다시 판단 (큰 모델, judge)
- discover:  웹 검색으로 새 정보원·행사 페이지 발굴 (큰 모델 + web_search, discover)

키는 GitHub Actions 비밀값 ANTHROPIC_API_KEY (colorado-alerts 워크스페이스 키)에서만 읽는다.
원문에 없는 값은 null 로 두고, 주소(URL)는 입력에 있던 것만 쓰게 한다(링크 환각 방지는 rules.py 에서 한 번 더 검사).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, TypeAdapter
from anthropic.lib._parse._transform import transform_schema

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def check_key() -> dict:
    """모델 목록 조회로 키가 살아 있는지 확인한다. 토큰을 쓰지 않는다."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "status": "missing", "detail": "GitHub secret ANTHROPIC_API_KEY is empty"}
    try:
        page = get_client().models.list(limit=1)
        first = page.data[0].id if page.data else "-"
        return {"ok": True, "status": "ok", "detail": f"models.list ok ({first})"}
    except anthropic.AuthenticationError as e:
        return {"ok": False, "status": "invalid", "detail": f"401 {e.message}"[:300]}
    except anthropic.PermissionDeniedError as e:
        return {"ok": False, "status": "permission", "detail": f"403 {e.message}"[:300]}
    except anthropic.RateLimitError:
        return {"ok": False, "status": "transient", "detail": "429 rate limited"}
    except anthropic.APIStatusError as e:
        return {"ok": False, "status": "transient", "detail": f"{e.status_code} {e.message}"[:300]}
    except anthropic.APIConnectionError:
        return {"ok": False, "status": "transient", "detail": "network error"}


# ---------------------------------------------------------------- 사용량
@dataclass
class Usage:
    """모델별 사용량. 가격은 config/models.json 의 prices(백만 토큰당 달러: input, output, cache_write, cache_read).
    웹 검색 횟수는 응답의 usage.server_tool_use.web_search_requests 를 우선 쓴다."""
    prices: dict
    web_search_usd: float = 0.01
    by_model: dict = field(default_factory=dict)
    cap: float | None = None   # 실행당 비용 상한(달러). 호출 전 '지금까지 + 그 호출의 최악 비용'이 넘으면 호출하지 않는다

    def allows(self, reserve: float) -> bool:
        return self.cap is None or self.usd + reserve <= self.cap

    def _m(self, model: str) -> dict:
        return self.by_model.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0,
                                                "usd": 0.0, "calls": 0, "searches": 0, "errors": 0, "batch_calls": 0})

    def add(self, model: str, usage, searches: int = 0, rate: float = 1.0) -> None:
        """rate: 가격 배율. 배치(Message Batches) 응답은 모든 토큰이 반값이라 0.5."""
        m = self._m(model)
        p = self.prices.get(model, {})
        if isinstance(p, (list, tuple)):  # 예전 형식 [입력, 출력]
            p = {"input": p[0], "output": p[1], "cache_write": p[0] * 1.25, "cache_read": p[0] * 0.1}
        inp = usage.input_tokens or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        out = usage.output_tokens or 0
        stu = getattr(usage, "server_tool_use", None)
        searches = (getattr(stu, "web_search_requests", None) if stu is not None else None) or searches
        m["input_tokens"] += inp
        m["cache_write_tokens"] += cw
        m["cache_read_tokens"] += cr
        m["output_tokens"] += out
        m["calls"] += 1
        m["batch_calls"] += int(rate != 1.0)
        m["searches"] += searches
        m["usd"] += (inp * p.get("input", 0) + cw * p.get("cache_write", 0) + cr * p.get("cache_read", 0) + out * p.get("output", 0)) / 1e6 * rate \
            + searches * self.web_search_usd
        live = os.environ.get("USAGE_LIVE_FILE")
        if live:  # 실행이 중간에 끊겨도 그때까지의 사용액이 남도록 호출마다 기록(개발 비용 집계용)
            with open(live, "w") as f:
                json.dump({"usd": round(self.usd, 4), "calls": sum(x["calls"] for x in self.by_model.values()), "by_model": self.by_model}, f)

    def error(self, model: str) -> None:
        self._m(model)["errors"] += 1

    @property
    def usd(self) -> float:
        return sum(m["usd"] for m in self.by_model.values())

    def totals(self) -> dict:
        keys = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "calls", "searches", "errors")
        return {k: sum(m[k] for m in self.by_model.values()) for k in keys}


# ---------------------------------------------------------------- 추출(작은 모델)
EnglishAttr = Literal["초보 가능", "보호자 동반", "시범 중심", "반복 만남"]
Signup = Literal["fcfs", "lottery", "open", "none", "unknown"]
RegStatus = Literal["upcoming", "open", "closed", "soldout", "waitlist", "cancelled", "postponed", "unknown"]
YearBasis = Literal["stated", "published", "assumed", "none"]
Bot = Literal["life", "family", "kids"]


class ExtractedEvent(BaseModel):
    title: str
    title_ko: str
    org: Optional[str]
    bot: Bot
    start_date: Optional[str]
    end_date: Optional[str]
    year_basis: YearBasis
    time_text: Optional[str]
    event_tz: Optional[str]
    location: Optional[str]
    city: Optional[str]
    age_text: Optional[str]
    english: list[EnglishAttr]
    cost_text: Optional[str]
    signup: Signup
    registration_opens: Optional[str]
    registration_status: RegStatus
    deadline: Optional[str]
    eligibility: Optional[str]
    status_note: Optional[str]
    tags: list[str]
    annual: bool
    paid_work: bool
    needs_review: bool
    review_reason: Optional[str]
    url: str


class EntryResult(BaseModel):
    index: int
    relevant: bool
    reason: str
    events: list[ExtractedEvent]


class ExtractBatch(BaseModel):
    results: list[EntryResult]


# 가족 구성·체류 신분 같은 개인 정보는 공개 저장소에 두지 않는다. GitHub 비밀값 HOUSEHOLD_PROFILE 이 있으면 그 문장을 쓴다.
DEFAULT_PROFILE = ("Audience: Korean residents and long-term visitors in the Denver metro around Westminster, Colorado, and their local friends. "
                   "Some hold non-immigrant visas that do not allow paid work; hobby or recreational classes are fine.")


def household_profile() -> str:
    return os.environ.get("HOUSEHOLD_PROFILE") or DEFAULT_PROFILE


EXTRACT_RULES = """You turn raw web items into structured event records.
Three audiences: life (adult hobbies, skills, sports, weekday classes, shows, clubs), family (outings, school games/shows, festivals, national parks, special experiences, cruise/travel promotions), kids (after-school, camps, sports clubs, school programs, arts/science classes).

Rules:
- Use only facts present in the item. If a value is not stated, use null (or "unknown" for signup, [] for lists). Never guess dates, prices or ages.
- url must be exactly one of the URLs given for that item. Never invent or edit URLs.
- Dates as YYYY-MM-DD. year_basis says where the year came from: stated (the item states the year), published (taken from the item's page date, posting date or a year heading), assumed (no year anywhere, you assumed the next occurrence after today), none (no date). Never present an assumed year as certain — set year_basis=assumed. If there is no date at all, null.
- Which date: if the item is about ONE session (its title or the first date right after the title names one date or one location), use that session's date even when the page also lists other sessions of a series. For a continuous run (e.g. "October 2-25", "on view until December"), use the stated first and last dates even if the start is already past; "until/through <month> [year]" without a day means end_date = the last day of that month, and a run that started before today keeps its real start_date (never replace it with a single performance or today). Only when the item itself covers several separate dates (e.g. weekly tours) use the earliest upcoming date as start_date and the last listed date as end_date.
- event_tz: IANA zone of the event location (America/Denver for Colorado). time_text: local time in Korean, e.g. "오전 10시–낮 12시", "매주 토 오후 2시". Times only — no notes like "(차량 관람)" that the title or summary already say.
- Korean for people: cost_text in Korean (e.g. "무료", "$25 (주민 $20)", "입장료 별도"), age_text in Korean (e.g. "6–10세", "성인", "초등 3–5학년", "보호자 동반 시 전 연령"). Keep amounts, dates and numbers exactly as stated. Keep location, org and proper nouns in their original English so they can be searched on a map.
- title_ko: short natural Korean title. Keep proper nouns in English in parentheses if useful.
- english: pick only attributes the text supports — 초보 가능 (beginners welcome), 보호자 동반 (parent accompanies), 시범 중심 (demonstration-based), 반복 만남 (recurring sessions with the same group).
- signup: fcfs = first come first served / limited spots; lottery = drawing; open = register any time; none = drop-in; unknown.
- registration_opens: the date registration/sign-up starts, only if stated. registration_status: upcoming (not open yet), open, closed, soldout, waitlist, cancelled (event cancelled), postponed, unknown. "RSVP closed", "registration closed", "registration is full", "at capacity" or "no spots left" for this item → closed; "sold out" → soldout. "Register now", a registration/RSVP button or "N spots remaining" for this item → open (and signup=fcfs when spots are limited).
- For registration announcements, put the opening time in time_text (e.g. "주민 오전 8:30 / 비주민 오전 9:30").
- age_text: copy every audience the page lists for this item (e.g. "60세 이상 · 성인"); do not replace a stated age with a broader word.
- eligibility: Korean note on who may join (residents only, members only, age/grade limits, skill level) when stated; else null.
- tags: 1-4 short lowercase English slugs (e.g. "swim", "camp", "hockey", "museum"). Always include these when they apply, because they keep far-away items for people who filter by distance: "permit", "lottery", "reservation", "timed-entry", "national-park", "state-park", "camping", "festival", "rare" (once-a-year or unusual experience).
- city: the city or town where the event happens (e.g. "Westminster", "Estes Park"), from the address or venue; null if unknown or online.
- annual: true if the text says it happens every year or is the Nth annual.
- paid_work: true if participating means paid employment (hiring instructors, paid assistants).
- status_note: Korean note only when the item says something about residency pricing, residency requirements, citizenship, ID or background checks.
- needs_review: true when eligibility, age fit, visa/status fit or whether it is an event at all is unclear. Put the doubt in review_reason (Korean).
- Registration, enrollment, reservation, permit, lottery, tryout and ticket-sale announcements ARE relevant (relevant=true) even when the page is not a single event: e.g. "Winter swim lesson registration begins Nov 30 for residents, Dec 1 for non-residents", "timed-entry reservations open Jan 15", "summer camp enrollment opens Feb 1". Output one record per program, season or permit with registration_opens set (the earliest date; put resident/non-resident or member differences in eligibility), deadline if stated, registration_status=upcoming or open, start_date null unless the program dates are stated, title naming the program and season. Far-future items (next year) count too. General season lines count as their own record too (e.g. "Winter/Spring Registration | Begins November 24" → one record for the season's general registration); if the year is not stated, use the next occurrence after today with year_basis=assumed.
- relevant=false when only a closed group may attend: enrolled students only, employees/faculty/staff only, military base personnel and their families only, private members only, invitation only. Events open to the public that merely need a ticket, RSVP, base visitor pass or registration ARE relevant.
- relevant=false (events=[]) for news without an attendable event or program, job postings, ads, or places outside the U.S. unless it is a travel promotion departing Denver.
- One item may contain several distinct events (e.g. a calendar page); list each.
Return exactly one result for EVERY item index you were given, including irrelevant ones."""


def extract_system() -> str:
    return EXTRACT_RULES + "\n\n" + household_profile()


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def build_extract_prompt(entries: list, today: str, text_chars: int) -> str:
    parts = [f"Today is {today} (America/Denver).", "Items:"]
    for i, e in enumerate(entries):
        parts.append(json.dumps({
            "index": i, "source": e.source_id, "url": e.url, "title": e.title,
            "published": e.published, "text": _clip(e.text or "", text_chars),
        }, ensure_ascii=False))
    return "\n".join(parts)


STOP_ERRORS = (anthropic.RateLimitError, anthropic.AuthenticationError, anthropic.PermissionDeniedError)
# API 선불 크레딧이 떨어지면 호출이 거부된다(카드·자동 충전이 없으면 추가 과금 없음). 그 실행에서는 AI 를 더 부르지 않고,
# 비AI 수집·기존 알림 발송은 계속한다. 카드 등록·충전·자동 충전은 소유자 승인 없이 하지 않는다.
CREDIT = {"out": False, "error": None}


def credit_exhausted(e: Exception) -> bool:
    t = str(e).lower()
    hit = isinstance(e, anthropic.APIStatusError) and ("credit balance" in t or "insufficient credit" in t or "billing" in t or "purchase credits" in t)
    if hit:
        CREDIT.update(out=True, error=str(e)[:200])
    return hit


@dataclass
class ExtractOutcome:
    results: dict = field(default_factory=dict)   # 입력 index → EntryResult
    failed: dict = field(default_factory=dict)    # 입력 index → 실패 사유(다음 실행에서 재시도)
    stopped: str | None = None                    # 사용량 제한 등으로 남은 묶음을 시도하지 않은 이유


def extract(client, cfg: dict, entries: list, today: str, usage: Usage) -> ExtractOutcome:
    """묶음별로 독립 처리한다. 한 묶음이 실패해도 앞 묶음 결과는 남고, AI 가 빠뜨린 항목은 실패로 표시해 재시도한다(1차 검증 1-2).
    묶음은 parallel 개씩 동시에 보낸다(13차: 정보원 62곳·상세 본문으로 순차 처리가 58분을 넘김). 동시에 보내는 묶음 수만큼
    최악 비용을 미리 예약해 실행당 비용 상한을 넘지 않는다. 사용량 기록과 결과 정리는 모두 이 함수(주 스레드)에서 한다."""
    c = cfg["extract"]
    out = ExtractOutcome()
    reserve, width = c.get("reserve_usd", 0.02), max(int(c.get("parallel", 1)), 1)
    # 작업 단위 (시작 위치, 크기). 응답이 출력 한도에서 잘리면(13차 시험 D: 12건 묶음 2개) 반으로 나눠 다시 넣는다
    work = [(s, min(c["batch"], len(entries) - s)) for s in range(0, len(entries), c["batch"])]

    def call(item: tuple[int, int]):
        start, size = item
        chunk = entries[start:start + size]
        try:
            return start, chunk, client.messages.parse(
                model=c["model"], max_tokens=c["max_tokens"], system=extract_system(),
                messages=[{"role": "user", "content": build_extract_prompt(chunk, today, c["text_chars"])}],
                output_config={"effort": c["effort"]}, output_format=ExtractBatch,
            ), None
        except Exception as e:  # noqa: BLE001  분류는 아래 주 스레드에서
            return start, chunk, None, e

    pos = 0
    with ThreadPoolExecutor(width) as ex:
        while pos < len(work):
            if not out.stopped:
                n = width
                while n > 0 and not usage.allows(reserve * n):
                    n -= 1
                if n == 0:
                    out.stopped = "run-cost-cap"
            if out.stopped:
                for start, size in work[pos:]:
                    out.failed.update({i: f"not-attempted: {out.stopped}" for i in range(start, start + size)})
                break
            wave = work[pos:pos + n]
            pos += len(wave)
            for start, chunk, resp, err in ex.map(call, wave):
                idx = range(start, start + len(chunk))
                truncated = (err is not None and "EOF while parsing" in str(err)) or (resp is not None and resp.stop_reason == "max_tokens")
                if truncated and len(chunk) > 1:
                    # SDK 가 잘린 JSON 에서 예외를 내면 응답 사용량을 받을 수 없다 → 입력(글자/4)·출력(한도) 추정치로 기록(개발비 과소 집계 방지)
                    usage.add(c["model"], resp.usage if resp is not None else SimpleNamespace(
                        input_tokens=4000 + sum(min(len(e.text or ""), c["text_chars"]) for e in chunk) // 4, output_tokens=c["max_tokens"]))
                    half = len(chunk) // 2
                    work += [(start, half), (start + half, len(chunk) - half)]
                    continue
                if err is not None:
                    usage.error(c["model"])
                    if isinstance(err, STOP_ERRORS) or credit_exhausted(err):
                        out.stopped = "credit-exhausted" if CREDIT["out"] else type(err).__name__
                        out.failed.update({i: out.stopped for i in idx})
                    elif isinstance(err, (anthropic.APIError, ValueError)):
                        out.failed.update({i: f"{type(err).__name__}: {str(err)[:120]}" for i in idx})
                    else:
                        raise err
                    continue
                usage.add(c["model"], resp.usage)
                if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
                    out.failed.update({i: f"stop_reason={resp.stop_reason}" for i in idx})
                    continue
                got = set()
                for r in resp.parsed_output.results:
                    if 0 <= r.index < len(chunk) and r.index not in got:
                        got.add(r.index)
                        out.results[start + r.index] = r.model_copy(update={"index": start + r.index})
                for j in range(len(chunk)):
                    if j not in got:
                        out.failed[start + j] = "missing-in-output"
    return out


# ---------------------------------------------------------------- 배치 호출(Message Batches): 같은 요청을 반값에
# 판정·추천은 몇 분 늦어도 되는 일이라 묶어서 배치로 보낸다(소유자 결정 2026-11-25: API 크레딧 월 $100 안에서 끊기지 않게).
# 배치는 보통 몇 분, 길면 1시간 넘게 걸린다. wait_min 분까지만 기다리고, 그때까지 안 끝난 요청은 취소한 뒤 기존 방식(즉시 호출)으로
# 처리한다 → 최악이어도 지금과 같은 비용·결과. 배치는 서버측 대체 모델(fallbacks)을 못 쓰므로 거절(refusal)된 요청도 즉시 호출로 다시 보낸다.
BATCH_POLL_SEC = 15


def _schema_format(out_type) -> dict:
    return {"type": "json_schema", "schema": transform_schema(TypeAdapter(out_type).json_schema())}


def _parsed_from_message(msg, out_type):
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        return out_type.model_validate_json(text)
    except Exception:  # noqa: BLE001  형식이 깨지면 판정 없음 → 호출한 쪽이 '누락'으로 다음 실행에 다시
        return None


def structured_calls(client, c: dict, system: str, bodies: list[str], out_type, usage: Usage, wait_min: float | None = None,
                     sleep=time.sleep, clock=time.monotonic) -> list[tuple]:
    """bodies 마다 (응답, 오류) 를 같은 순서로 돌려준다. 응답은 stop_reason·usage·parsed_output 을 가진다.
    wait_min 이 있으면 먼저 배치로 보내고(반값), 없거나 배치를 못 쓰면 하나씩 즉시 호출(서버측 대체 모델 포함)한다."""
    res: list = [None] * len(bodies)
    pending = list(range(len(bodies)))
    batches = getattr(getattr(client, "messages", None), "batches", None)
    if wait_min and bodies and batches is not None and not CREDIT["out"]:
        fmt = _schema_format(out_type)
        reqs = [{"custom_id": f"r{i}", "params": {
            "model": c["model"], "max_tokens": c["max_tokens"], "system": system,
            "messages": [{"role": "user", "content": body}], "output_config": {"effort": c["effort"], "format": fmt}}}
            for i, body in enumerate(bodies)]
        b = None
        try:
            b = batches.create(requests=reqs)
        except anthropic.APIError as e:
            usage.error(c["model"])
            if credit_exhausted(e) or isinstance(e, STOP_ERRORS):
                return [(None, e)] * len(bodies)
            # 배치 접수만 실패: 즉시 호출로 진행
        if b is not None:
            deadline = clock() + wait_min * 60
            while b.processing_status != "ended" and clock() < deadline:
                sleep(BATCH_POLL_SEC)
                b = batches.retrieve(b.id)
            if b.processing_status != "ended":
                batches.cancel(b.id)   # 이미 끝난 요청의 결과는 남고(반값 청구), 나머지는 즉시 호출로
                stop = clock() + 180
                while b.processing_status != "ended" and clock() < stop:
                    sleep(5)
                    b = batches.retrieve(b.id)
            if b.processing_status == "ended":
                for r in batches.results(b.id):
                    i = int(r.custom_id[1:])
                    if r.result.type != "succeeded":
                        continue           # 오류·취소·만료 → 즉시 호출로
                    msg = r.result.message
                    usage.add(c["model"], msg.usage, rate=0.5)
                    if msg.stop_reason == "refusal":
                        continue           # 대체 모델로 다시
                    res[i] = (SimpleNamespace(stop_reason=msg.stop_reason, usage=msg.usage,
                                              parsed_output=_parsed_from_message(msg, out_type) if msg.stop_reason != "max_tokens" else None), None)
                pending = [i for i in pending if res[i] is None]
    for i in pending:
        if CREDIT["out"]:
            res[i] = (None, RuntimeError("credit-exhausted"))
            continue
        try:
            res[i] = (client.beta.messages.parse(
                model=c["model"], max_tokens=c["max_tokens"], system=system,
                messages=[{"role": "user", "content": bodies[i]}], output_config={"effort": c["effort"]},
                output_format=out_type, betas=[FALLBACK_BETA], fallbacks="default",
            ), None)
        except (anthropic.APIError, ValueError) as e:
            usage.error(c["model"])
            credit_exhausted(e)
            res[i] = (None, e)
    return res


# ---------------------------------------------------------------- 판단(큰 모델)
class Verdict(BaseModel):
    index: int
    keep: bool
    reason: str
    bot: Bot
    age_text: Optional[str]
    status_note: Optional[str]
    english: list[EnglishAttr]


class JudgeBatch(BaseModel):
    verdicts: list[Verdict]


JUDGE_RULES = """U.S. camps usually state the grade a child enters in the fall; compare with the audience's school grades when given.
You review events a smaller model flagged as uncertain. For each, decide keep (worth sending to this family or local friends), the right audience bot, and correct age_text (Korean, e.g. "6–10세"), status_note (Korean) and english attributes using only the source text. Drop paid jobs, events requiring U.S. citizenship, and items that are not attendable events or programs. Keep program registration announcements (registration opening dates for classes, lessons, camps or seasons) — they are actionable even without an event date. Reason in Korean, one sentence."""


@dataclass
class JudgeOutcome:
    verdicts: dict = field(default_factory=dict)   # 입력 index → Verdict
    missing: dict = field(default_factory=dict)    # 입력 index → 사유(판정이 없거나 실패: 보내지 않고 다음 실행에서 다시)
    duplicates: int = 0


def _plan_chunks(c: dict, n: int, usage: Usage, default_reserve: float) -> tuple[list[int], dict]:
    """보낼 묶음의 시작 위치와, 비용 상한·크레딧 때문에 못 보내는 항목(index → 사유)."""
    starts, skipped = [], {}
    reserve = c.get("reserve_usd", default_reserve)
    for start in range(0, n, c["batch"]):
        idx = range(start, min(start + c["batch"], n))
        if CREDIT["out"] or not usage.allows(reserve * (len(starts) + 1)):
            skipped.update({i: "credit-exhausted" if CREDIT["out"] else "run-cost-cap" for i in idx})
        else:
            starts.append(start)
    return starts, skipped


def _missing_reason(err) -> str:
    return "credit-exhausted" if str(err) == "credit-exhausted" else type(err).__name__


def judge(client, cfg: dict, items: list[tuple[dict, str]], usage: Usage, batch_wait_min: float | None = None) -> JudgeOutcome:
    """items: (event dict, source text). 큰 모델 + 서버측 대체 모델(거절 시). batch_wait_min 이 있으면 배치(반값)로 먼저 보낸다.
    추출과 같이 입력·출력 인덱스를 대조한다(2차 재검증 D): 빠지거나 실패한 항목은 missing, 중복 인덱스는 첫 판정만."""
    c = cfg["judge"]
    out = JudgeOutcome()
    starts, out.missing = _plan_chunks(c, len(items), usage, 0.4)
    bodies = ["\n".join(json.dumps({"index": i, "event": ev, "source_text": _clip(txt, 2000)}, ensure_ascii=False)
                         for i, (ev, txt) in enumerate(items[start:start + c["batch"]])) for start in starts]
    replies = structured_calls(client, c, JUDGE_RULES + "\n\n" + household_profile(), bodies, JudgeBatch, usage, batch_wait_min)
    for start, (resp, err) in zip(starts, replies):
        chunk = items[start:start + c["batch"]]
        idx = range(start, start + len(chunk))
        if err is not None:
            out.missing.update({i: _missing_reason(err) for i in idx})
            continue
        if not isinstance(resp, SimpleNamespace):   # 즉시 호출 응답(배치 응답은 structured_calls 에서 반값으로 기록)
            usage.add(c["model"], resp.usage)
        if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
            out.missing.update({i: f"stop_reason={resp.stop_reason}" for i in idx})
            continue
        for v in resp.parsed_output.verdicts:
            if not (0 <= v.index < len(chunk)):
                continue
            if start + v.index in out.verdicts:
                out.duplicates += 1
                continue
            out.verdicts[start + v.index] = v.model_copy(update={"index": start + v.index})
        for j in range(len(chunk)):
            if start + j not in out.verdicts:
                out.missing[start + j] = "missing-verdict"
    return out


# ---------------------------------------------------------------- 추천 판정(큰 모델): 수집한 행사 중 사람에게 알릴 가치가 있는 것을 고른다
# 추출(무슨 행사인가)과 추천(알릴 만한가)은 다른 판단이다. 소유자 피드백(2026-10-22): 일상 봇 60건에 시의회·위원회 회의, 약물 반납,
# 영유아 동화 시간, 시니어 프로그램이 성인 취미·체험 알림으로 섞였다 → 모든 발송 후보에 이 판정을 적용하고, 판정이 없으면 보내지 않는다.
Category = Literal["performance", "festival", "sports", "outdoors", "class", "museum", "camp", "university", "kids-activity", "family-outing",
                   "food-market", "toddler", "senior", "library-regular", "civic", "public-service", "support-group", "other"]
Highlight = Literal["선착순", "무료", "예약 필요", "추첨", "접수 예정", "마감 임박", "특별 체험", "허가 필요", "연례 행사"]


SpecialBasis = Literal["restricted_access", "public_subsidy_price", "free_limited", "scarce_quota_or_lottery", "rare_one_off", "school_community_nonregular", "none"]
Blocker = Literal["routine_commercial_program", "routine_performance", "pro_sports", "school_sports_regular", "english_listening_core", "civic_admin",
                  "age_or_eligibility_mismatch", "ordinary_open_facility", "promo_wording_only", "recurring_series"]
# 이 중 하나라도 있으면 적극 알림 금지(이슈 #21·#22·#24). 수집·저장은 그대로, 발송만 거른다
PUSH_BLOCKERS = set(Blocker.__args__)


class Recommendation(BaseModel):
    index: int
    level: Literal["push", "interest", "none"]
    bot: Bot
    category: Category
    score: int
    title_ko: str
    summary_ko: str
    highlights: list[Highlight]
    reason: str
    special_reason: str
    special_basis: SpecialBasis
    evidence_quote: str
    usual_alternative: str
    blockers: list[Blocker]


class RecommendBatch(BaseModel):
    items: list[Recommendation]


RECOMMEND_RULES = """You are the editor of three Telegram alert channels (life, family, kids) for Korean residents in the Denver metro (base: Westminster, CO) and their local friends.
- life: adults' outings and experiences; family: things a whole family does together (budget for 5 people: 2 adults + 3 children); kids: school-age children's activities and camps.
This editorial policy is the purpose of the service and applies to all three channels and every subscriber. The system collects everything; you decide what deserves a PUSH alert.

THE KEY QUESTION: "Is this just an ordinary product or schedule that anyone can buy or attend at its normal price on another day?" If yes, it is NOT push, however it is advertised.
PUSH is for opportunities that would be a pity to miss: experiences normally impossible, publicly funded or sponsored programs much cheaper than usual or free, scarce quotas/lotteries/permits, genuinely rare one-off events, and hands-on experiences.
OWNER STANDARD (same as the owner's Korean alerts): the owner loves events. Annual or seasonal EVENTS are wanted - a city/town/county/park district/library/school/university festival, parade, fireworks, tree lighting, holiday market, county fair, rodeo, Halloween or holiday community event, cultural day - and also annual seasonal events by museums, zoos, gardens or heritage railways (holiday light festivals, a holiday train that runs a few weeks a year). Free or paid does not matter for these, and an unstated price is NOT a reason to withhold push. What is NOT wanted is the routine product of a commercial business or studio, sold the same way week after week (classes, drop-ins, workshops, ordinary shows).
Prefer direct participation (riding, making, trying, entering a closed facility, joining a community event) over passive listening. The audience does not follow long spoken English well.

Fill these fields for every item:
- special_basis: the single strongest verified basis for push, or none:
    restricted_access - a place normally closed to the public opens (military base, airfield, aviation/space/research facility, behind-the-scenes, special rides);
    public_subsidy_price - a city/county/state/school/public agency/university funds it so it is free or far below the usual market price for the same thing;
    free_limited - free tickets/admission days with limited supply (e.g. museum or zoo free days, first-come free tickets);
    scarce_quota_or_lottery - permits, lotteries, timed-entry or registrations with a small quota or a short window;
    rare_one_off - a genuinely rare event (once a year or less, short season, pop-up) whose core is an experience you cannot normally get; this INCLUDES every annual/seasonal event in the OWNER STANDARD (a public body's yearly festival, parade, lighting, fair or holiday event; a yearly light festival or holiday train), paid or free - quote the words that show it is that event (e.g. "Annual", "WinterFest", "Parade of Lights", "holiday lighting");
    school_community_nonregular - a non-regular school/university community event the public can join or watch (e.g. district/regional tournament, school field day, student-run festival, community engagement day);
    none - no such basis.
- evidence_quote: a short phrase copied EXACTLY (verbatim, original language) from the source text that proves special_basis (e.g. "free admission", "open to the public for one day only", "limited to 30 participants", "funded by SCFD"). Empty if none. Never paraphrase; never invent.
- usual_alternative: one short Korean phrase on how one can normally get the same thing (e.g. "같은 수업이 매주 $20에 상시 운영", "정규 시즌 홈경기 상시 판매", "평소에도 무료 개방"). Empty if there is no ordinary alternative.
- blockers: every trait that applies (these forbid push):
    routine_commercial_program - a class, session or service sold at its normal price by a gym, recreation/fitness center, studio, shop or venue: yoga, meditation, sound bath, singing bowl, healing, breathwork, wellness, massage, fitness/dance class, cooking, crafts, pottery, painting, adult hobby class, paid one-day workshop or drop-in, regular camp sessions - even if a public recreation district runs it, even if it is a single date, needs registration or has limited spots, as long as that is how it is normally sold;
    routine_performance - regular-season or full-price concerts, theater, orchestras (including civic/city orchestras' regular concerts), ordinary Red Rocks shows, ordinary concert-hall programs;
    pro_sports - professional or semi-pro league games (NBA, NHL, NFL, MLB, MLS, volleyball leagues etc.);
    school_sports_regular - regular-season games or meets of elementary, middle, high school or college/university teams, including club sports (these go to a separate calendar page, not alerts); a district/state tournament or a school field day the public can join is NOT this blocker;
    english_listening_core - the value is mainly listening to or reading English: lectures, talks, seminars, colloquia, panels, author/director Q&A, documentary screening with discussion, docent-led talks, book clubs, conversation groups, theory-heavy science talks;
    civic_admin - council/board/commission meetings, hearings, administrative services, drug take-back, recycling drop-off;
    age_or_eligibility_mismatch - toddler/baby-only, senior-only, members/staff/students-only, residents-only that the audience cannot join, or ages that do not fit the household;
    ordinary_open_facility - a facility, museum, park or gallery that is simply open as usual;
    promo_wording_only - the only "special" claim is marketing wording ("special", "one time", "limited", "exclusive", "reservation required", "small group") with no verified scarcity, price advantage or access; never use it for an annual/seasonal event in the OWNER STANDARD;
    recurring_series - one date of a weekly or monthly recurring program (e.g. a tour every Friday).
- level:
    push - ONLY when special_basis is not none, evidence_quote is verbatim in the source, and blockers is empty. Free alone is not enough (a library's always-free weekly meetup is not push); paid is fine if the experience itself is rare (e.g. a holiday train ride that runs only a few weeks a year).
    interest - worthwhile for people who chose this category but not special (routine classes, recurring programs, ordinary performances).
    none - not worth alerting (items such as civic_admin, pro_sports, school_sports_regular, english_listening_core, mismatched age).
    When in doubt, do not push.
- special_reason: for push, ONE short Korean sentence stating the verified reason (e.g. "평소 비공개인 공군기지 비행장을 하루만 개방", "시 지원으로 무료, 선착순 30명"). Empty otherwise. It must follow from evidence_quote.
- Consider the total cost for a family of five, age/residency eligibility for a stay from late June 2027 to August 2028, travel from Westminster, and pre-registration. Far places are push only when the experience is truly rare. If eligibility is unclear, say so in reason and do not push. If only the cost is unclear, an annual/seasonal event in the OWNER STANDARD is still push (say "비용 미확인" in reason); other items with unclear total cost are not push.
- Any venue is judged on the event itself: a famous place is not a reason, and a genuinely free, publicly sponsored or restricted-access event at a famous place can still be push.
- bot: the channel whose audience fits best. category: best fit. score 0-100 within its level.
- title_ko: concise natural Korean title (max ~30 characters).
- summary_ko: one or two short Korean sentences on what you actually do or get there. No dates, prices or addresses.
- highlights: only those the item supports: 선착순, 무료, 예약 필요, 추첨, 접수 예정, 마감 임박, 특별 체험, 허가 필요, 연례 행사. Use 무료 only if the source states it is free.
- NO REPETITION (owner complaint 2026-11-25): the alert shows title_ko, highlight badges, special_reason, summary_ko, then date/time, place, cost and registration lines. Each piece must add something new.
    special_reason: only the rarity/access fact (e.g. "12월 몇 주간만 운영", "평소 닫힌 구역을 하루만 개방"). Do not restate what the event is, the venue name, or a highlight already chosen (연례 행사, 예약 필요, 무료 ...).
    summary_ko: what you actually do there, in words not already in title_ko or special_reason. Do not repeat the venue or the event type from the title.
    Example of what NOT to write: title "레드락스 크리스마스 인 컬러 드라이브", special_reason "연말 몇 주간만 레드락스에서 열리는 홀리데이 조명 드라이브 행사", summary "레드락스 공원 안을 차로 지나며 … 조명 쇼". Better: special_reason "연말 몇 주간만 운영", summary "차 안에서 캐럴에 맞춰 바뀌는 대형 조명을 봅니다."
- reason: one short Korean sentence explaining the level (for interest/none, name the main blocker or the missing basis)."""


@dataclass
class RecommendOutcome:
    recs: dict = field(default_factory=dict)      # 입력 index → Recommendation
    missing: dict = field(default_factory=dict)   # 입력 index → 사유(판정이 없으면 보내지 않고 다음 실행에서 다시)


def recommend(client, cfg: dict, items: list[tuple[dict, str]], today: str, usage: Usage, batch_wait_min: float | None = None) -> RecommendOutcome:
    """items: (event dict, source text). 모든 발송 후보에 적용한다(애매한 것만이 아니라). batch_wait_min 이 있으면 배치(반값)로 먼저 보낸다."""
    c = cfg["recommend"]
    out = RecommendOutcome()
    starts, out.missing = _plan_chunks(c, len(items), usage, 0.5)
    bodies = [f"Today: {today}\n" + "\n".join(json.dumps({"index": i, "event": ev, "source_text": _clip(txt, 1500)}, ensure_ascii=False)
                                              for i, (ev, txt) in enumerate(items[start:start + c["batch"]])) for start in starts]
    replies = structured_calls(client, c, RECOMMEND_RULES + "\n\n" + household_profile(), bodies, RecommendBatch, usage, batch_wait_min)
    for start, (resp, err) in zip(starts, replies):
        chunk = items[start:start + c["batch"]]
        idx = range(start, start + len(chunk))
        if err is not None:
            out.missing.update({i: _missing_reason(err) for i in idx})
            continue
        if not isinstance(resp, SimpleNamespace):   # 즉시 호출 응답(배치 응답은 structured_calls 에서 반값으로 기록)
            usage.add(c["model"], resp.usage)
        if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
            out.missing.update({i: f"stop_reason={resp.stop_reason}" for i in idx})
            continue
        for r in resp.parsed_output.items:
            if 0 <= r.index < len(chunk) and start + r.index not in out.recs:
                out.recs[start + r.index] = r.model_copy(update={"index": start + r.index, "score": max(0, min(100, r.score))})
        for j in range(len(chunk)):
            if start + j not in out.recs:
                out.missing[start + j] = "missing-recommendation"
    return out



# ---------------------------------------------------------------- 추천 판정을 맥북 Codex(ChatGPT 구독)로
# 소유자 결정(2026-11-25): API 크레딧 대신 남는 ChatGPT 구독 사용량으로 추천 판정. 같은 규칙·입력으로 Opus 대비 적극 알림 여부 28/30 일치
# (gpt-6.2-sol, effort low). Luna 는 연말 행사를 놓쳐 제외. 공개 저장소라 GitHub 에서는 돌리지 않고 맥북(collector/rec_runner.py)에서만.
CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
CODEX_PREAMBLE = ("You are acting as a classification function, not a coding agent. Do not run any commands or read any files. "
                  "Follow the SYSTEM rules below and return one item per input index as JSON matching the output schema.\n\n")


def codex_prompt(system: str, body: str) -> str:
    return CODEX_PREAMBLE + "=== SYSTEM ===\n" + system + "\n\n=== INPUT ===\n" + body


def _codex_call(c: dict, prompt: str, schema_path: str, run=None) -> str:
    import subprocess
    import tempfile
    run = run or subprocess.run
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.json")
        cmd = [c.get("bin") or CODEX_BIN, "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-C", d,
               "-m", c["model"], "-c", f'model_reasoning_effort="{c["effort"]}"', "--output-schema", schema_path, "-o", out, "-"]
        p = run(cmd, input=prompt, capture_output=True, text=True, timeout=c.get("timeout_sec", 900))
        if p.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(f"codex exit {p.returncode}: {(p.stderr or p.stdout or '')[-300:]}")
        return open(out).read()


def recommend_codex(cfg: dict, items: list[tuple[dict, str]], today: str, run=None) -> RecommendOutcome:
    """recommend() 와 같은 규칙·입력·결과 형식. cfg["recommend_codex"]: model, effort, batch, parallel, bin, timeout_sec."""
    import tempfile
    c = cfg["recommend_codex"]
    out = RecommendOutcome()
    system = RECOMMEND_RULES + "\n\n" + household_profile()
    starts = list(range(0, len(items), c["batch"]))
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(_schema_format(RecommendBatch)["schema"], f)
        schema_path = f.name

    def one(start):
        chunk = items[start:start + c["batch"]]
        body = f"Today: {today}\n" + "\n".join(json.dumps({"index": i, "event": ev, "source_text": _clip(txt, 1500)}, ensure_ascii=False)
                                              for i, (ev, txt) in enumerate(chunk))
        try:
            return start, RecommendBatch.model_validate_json(_codex_call(c, codex_prompt(system, body), schema_path, run)), None
        except Exception as e:  # noqa: BLE001  실패한 묶음은 누락으로(다음에 다시)
            return start, None, e

    try:
        with ThreadPoolExecutor(max(int(c.get("parallel", 2)), 1)) as ex:
            for start, parsed, err in ex.map(one, starts):
                chunk = items[start:start + c["batch"]]
                if parsed is None:
                    out.missing.update({start + j: f"codex: {type(err).__name__}: {str(err)[:120]}" for j in range(len(chunk))})
                    continue
                for r in parsed.items:
                    if 0 <= r.index < len(chunk) and start + r.index not in out.recs:
                        out.recs[start + r.index] = r.model_copy(update={"index": start + r.index, "score": max(0, min(100, r.score))})
                for j in range(len(chunk)):
                    if start + j not in out.recs:
                        out.missing[start + j] = "missing-recommendation"
    finally:
        os.unlink(schema_path)
    return out


# ---------------------------------------------------------------- 추천 대기(await_rec) 항목 처리: 맥북 Codex 판정기와 GitHub 대체 경로가 같이 쓴다
def process_await_rec(api, rows: list[dict], recommend_fn, run_id: str | None = None, found_by: str = "github_collector") -> dict:
    """rows: take_rec 로 받은 대기열 항목(result = 추천 전 행사 목록, body.text = 원문).
    판정된 항목은 Worker 에 등록하고 done, 판정이 빠진 항목은 돌려놓는다(대기 상태 유지, 나중에 다시)."""
    from rules import for_worker
    items, owner = [], []
    for r in rows:
        txt = (r.get("body") or {}).get("text") or ""
        for ev in r.get("result") or []:
            items.append((ev, txt))
            owner.append(r["key"])
    ro = recommend_fn(items) if items else RecommendOutcome()
    bad = {owner[j] for j in ro.missing}
    evs, ev_owner = [], []
    for j, (ev, txt) in enumerate(items):
        if owner[j] in bad or j not in ro.recs:
            continue
        x = for_worker(apply_recommendation(dict(ev), ro.recs[j], txt))
        x["found_by"] = x.get("found_by") or found_by
        x["run_id"] = x.get("run_id") or run_id
        evs.append(x)
        ev_owner.append(owner[j])
    good = sorted({r["key"] for r in rows} - bad)
    enq = api.enqueue(evs) if evs else {}
    rejected = api.queue_result([{"key": k, "status": "done"} for k in good] + [{"key": k, "status": "release"} for k in sorted(bad)]) or set()
    levels = {}
    for x in evs:
        levels[x["rec_level"]] = levels.get(x["rec_level"], 0) + 1
    return {"rows": len(rows), "events": len(evs), "missing_rows": len(bad), "levels": levels, "outbox": enq.get("outbox", 0), "rejected": len(rejected)}

# ---------------------------------------------------------------- 놓친 검색 분석: 외부 검색이 찾은 행사를 우리 수집이 왜 못 찾았나, 다음부터 찾으려면
CAUSE_CODES = ("missing_source", "query_gap", "scrape_or_js_failure", "freshness_or_schedule", "extraction_error",
               "eligibility_or_recommendation_error", "identity_duplicate_error", "delivery_error", "outside_scope", "uncertain")


class GapFix(BaseModel):
    index: int
    cause_code: Literal["missing_source", "query_gap", "scrape_or_js_failure", "freshness_or_schedule", "extraction_error",
                        "eligibility_or_recommendation_error", "identity_duplicate_error", "delivery_error", "outside_scope", "uncertain"]
    cause_ko: str
    monitor_url: Optional[str]
    monitor_type: Literal["html", "rss", "ical", "json"]
    monitor_mode: Literal["list", "page-change"]
    monitor_name: Optional[str]
    queries: list[str]
    query_bot: Literal["family", "kids", "life", "any"]


class GapBatch(BaseModel):
    items: list[GapFix]


GAP_RULES = """An alert system collects Colorado (Denver metro) events from registered official sources, daily news-style keyword searches and an AI web search.
Another search found the events below that our system did NOT find (or only found but had not processed). For each, explain the most likely structural cause in one Korean sentence
(e.g. "Botanic Gardens 일반 행사 목록에는 특별 행사(Blossoms of Light)가 따로 있어 감시하지 않음", "이 기관 사이트가 정보원에 없음", "축제 이름으로만 공지되어 키워드 검색에 안 걸림"),
and propose a fix that would catch this event AND similar future ones:
- monitor_url: one official https page that lists such events or announces them (an events/calendar/special-events/registration page of the organization; prefer a list or calendar page over a single-event page; use the event page itself only for a recurring annual event with its own page). Must be on the organization's own site, not a ticket reseller or news site. null if none fits.
- monitor_type: html for normal pages (rss/ical/json only if the URL clearly is a feed). monitor_mode: list (a page with links to many events) or page-change (a single page whose text changes, e.g. registration dates).
- queries: 1–3 short English search phrases (as people would search news/listings) that would surface this and similar events, e.g. "holiday light show Denver tickets".
Registered source hosts are given; if the host is already registered, the fix is usually a more specific list page on that site.
cause_code (pick one; do not guess beyond the evidence):
- missing_source: the organization's official site is not monitored at all AND none of our keyword searches would plausibly surface it.
- query_gap: the event is publicised in ways our searches could catch, but our search phrases do not cover its name/type.
- scrape_or_js_failure: the host IS registered but the listing is likely rendered by JavaScript, a separate special-events page, a ticketing widget or blocked to bots.
- extraction_error: our_history says we fetched this exact URL before but produced no event (text was there but not extracted).
- freshness_or_schedule: our_history says we had it queued / it was published after our last run.
- eligibility_or_recommendation_error / identity_duplicate_error / delivery_error: only when our_history shows that stage dropped it.
- outside_scope: not attendable for this household (members/students only, far outside Colorado, already over).
- uncertain: the evidence is not enough to tell.
Do NOT pick missing_source merely because the host is absent from registered_hosts; first consider query_gap and outside_scope."""


def gap_analysis(client, cfg: dict, items: list[dict], registered_hosts: list[str], usage: Usage) -> dict:
    """items: {index, title, url, found, start_date, description}. 반환: index → GapFix. 큰 모델(추천 판정과 같은 설정)."""
    c = cfg["recommend"]
    if not items or not usage.allows(c.get("reserve_usd", 0.5)) or CREDIT["out"]:
        return {}
    body = json.dumps({"registered_hosts": registered_hosts[:200], "events": items}, ensure_ascii=False)
    try:
        resp = client.beta.messages.parse(model=c["model"], max_tokens=c["max_tokens"], system=GAP_RULES, messages=[{"role": "user", "content": body}],
                                          output_config={"effort": c["effort"]}, output_format=GapBatch, betas=[FALLBACK_BETA], fallbacks="default")
    except (anthropic.APIError, ValueError) as e:
        usage.error(c["model"])
        credit_exhausted(e)
        return {}
    usage.add(c["model"], resp.usage)
    if resp.parsed_output is None:
        return {}
    return {g.index: g for g in resp.parsed_output.items}


SOURCE_FIELDS = ("title", "org", "location", "cost_text", "time_text", "age_text", "eligibility", "status_note", "registration_status", "signup")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\u2018\u2019\u201c\u201d\"'`]", "", str(t or "").lower())).strip()


def push_gate(r: Recommendation, text: str | None) -> str | None:
    """적극 알림 결정 검증(이슈 #24). 통과하면 None, 아니면 막은 사유(내부 기록용).
    모델의 판단만 믿지 않는다: 근거 종류·원문 인용·차단 사유를 코드로 다시 본다."""
    if r.level != "push":
        return None
    if not (r.special_reason or "").strip():
        return "no-special-reason"
    if r.special_basis == "none":
        return "no-special-basis"
    bad = [x for x in r.blockers if x in PUSH_BLOCKERS]
    if bad:
        return "blocked:" + ",".join(bad)
    q = _norm(r.evidence_quote)
    if len(q) < 4:
        return "no-evidence-quote"
    if text is not None and q not in _norm(text):
        return "evidence-not-in-source"   # 원문에 없는 근거(지어낸 근거·홍보 문구 해석)는 인정하지 않는다
    return None


def apply_recommendation(ev: dict, r: Recommendation, text: str | None = None) -> dict:
    """판정 적용. text 를 주면 근거 인용이 원문에 실제로 있는지 대조한다(없으면 적극 알림 불가)."""
    special = (r.special_reason or "").strip()[:160]
    if text is not None:  # 모델이 본 것과 같게: 원문 + 추출된 행사 정보(제목·비용·자격 등)
        text = text + " " + " ".join(str(ev.get(k) or "") for k in SOURCE_FIELDS)
    blocked = push_gate(r, text)
    level = "interest" if blocked else r.level
    ev.update({"rec_level": level, "bot": r.bot, "category": r.category, "rec_score": r.score, "summary_ko": r.summary_ko.strip()[:240],
               "highlights": list(dict.fromkeys(r.highlights)), "rec_reason": r.reason.strip()[:200], "special_reason": special if level == "push" else "",
               "special_basis": r.special_basis, "evidence_quote": r.evidence_quote.strip()[:200], "usual_alternative": r.usual_alternative.strip()[:120],
               "rec_blockers": list(dict.fromkeys(r.blockers)), "push_blocked": blocked or ""})
    if r.title_ko.strip():
        ev["title_ko"] = r.title_ko.strip()[:60]
    return ev


# ---------------------------------------------------------------- 발굴(큰 모델 + 웹 검색)
DISCOVER_SYSTEM = """You look for NEW official sources of family, kids and adult activities near Westminster, Colorado (Denver metro) for Korean residents and their local friends.
Search the web for the theme you are given. Only future things matter (today's date is given): prioritise events happening, or registrations opening or closing, in the next few months, but ALSO report information announced far in advance whatever its date — events in 2027–2028, early registration or reservations, national park permits, lotteries and timed-entry reservations, summer camp enrollment, season passes. Exclude only things that are already over (past events, past seasons, archived pages whose dates are all before today).
Report:
- sources: official organization pages that list recurring events, classes or registrations (city recreation, libraries, museums, universities, parks, clubs), including nearby cities' official calendars. Prefer pages with calendars or RSS that show upcoming dates.
- events: specific upcoming events or registration openings worth checking, each with date = its next upcoming date (YYYY-MM-DD) as stated on the page, or null if not stated.
Use only URLs that appeared in your search results. Skip pages already listed under "known".
Use only these bot names: kids, family, life (life = adults).
Answer with JSON only, no prose: {"sources":[{"url":"","name":"","reason":"","bots":["kids"]}],"events":[{"url":"","title":"","date":null,"why":""}]}"""


def _json_from_text(text: str) -> dict:
    """응답 글에서 {"sources": …} 객체를 찾는다. 앞뒤 설명·코드 블록·여러 조각이 섞여 있어도, 뒤에서부터 해석되는 첫 객체를 쓴다."""
    dec = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"\{", text)]
    for i in reversed(starts):
        try:
            obj, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("sources" in obj or "events" in obj):
            return obj
    return {}


BOT_ALIASES = {"life": "life", "adult": "life", "adults": "life", "family": "family", "families": "family",
               "kids": "kids", "kid": "kids", "children": "kids", "teens": "kids", "youth": "kids"}


def norm_bots(bots) -> list[str]:
    """발굴 결과의 분야 이름을 봇 이름으로 맞춘다(예: adults → life). 모르는 이름은 버린다."""
    return list(dict.fromkeys(BOT_ALIASES[b.lower()] for b in (bots or []) if isinstance(b, str) and b.lower() in BOT_ALIASES))


def discover(client, cfg: dict, theme: str, known: list[str], usage: Usage, max_searches: int | None = None, today: str | None = None) -> dict:
    """반환: {"sources": [...], "events": [...], "result_urls": set}. 검색 결과에 실제로 나온 URL 만 남긴다.
    today 를 주면 가까운 일정을 우선하되 미리 발표되는 먼 미래 정보(2027–2028 행사, 조기 신청, 국립공원 허가, 캠프 모집)도 찾게 하고,
    날짜가 오늘 이전인 결과만 버린다(10차 시험: 지난 시즌 일정 위주, 12차: 기간 제한 없음)."""
    c = cfg["discover"]
    today = today or datetime.now(timezone.utc).date().isoformat()
    messages = [{"role": "user", "content": f"Today: {today}\nTheme: {theme}\nknown: {json.dumps(known[:60])}"}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches or c["max_searches"],
              "user_location": {"type": "approximate", "city": "Westminster", "region": "Colorado", "country": "US", "timezone": "America/Denver"}}]
    seen_urls: set[str] = set()
    queries: list[str] = []
    text = ""
    for _ in range(3):  # pause_turn 이면 이어서 요청
        if not usage.allows(c.get("reserve_usd", 1.2)) or CREDIT["out"]:
            break
        try:
            resp = client.beta.messages.create(
                model=c["model"], max_tokens=c["max_tokens"], system=DISCOVER_SYSTEM, messages=messages, tools=tools,
                output_config={"effort": c["effort"]}, betas=[FALLBACK_BETA], fallbacks="default",
            )
        except anthropic.APIError as e:
            credit_exhausted(e)
            raise
        searches = 0
        for b in resp.content:
            if b.type == "server_tool_use" and isinstance(getattr(b, "input", None), dict) and b.input.get("query"):
                queries.append(str(b.input["query"])[:200])  # 작업량 기록: 실제 검색어
            if b.type == "web_search_tool_result":
                searches += 1
                if isinstance(b.content, list):
                    seen_urls.update(r.url for r in b.content if getattr(r, "url", None))
            elif b.type == "text":
                text += b.text
        usage.add(c["model"], resp.usage, searches=searches)
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    data = _json_from_text(text)

    def ok(u: str) -> bool:
        return isinstance(u, str) and u.rstrip("/") in {x.rstrip("/") for x in seen_urls}

    raw_s, raw_e = data.get("sources") or [], data.get("events") or []
    sources = [{**s, "bots": norm_bots(s.get("bots"))} for s in raw_s if isinstance(s, dict) and ok(s.get("url"))]
    events = [e for e in raw_e if isinstance(e, dict) and ok(e.get("url"))
              and not (isinstance(e.get("date"), str) and re.match(r"\d{4}-\d{2}-\d{2}$", e["date"]) and e["date"] < today)]
    return {"sources": sources, "events": events, "dropped_urls": len(raw_s) + len(raw_e) - len(sources) - len(events),
            "result_urls": len(seen_urls), "queries": queries, "raw_text": text[-1500:]}
