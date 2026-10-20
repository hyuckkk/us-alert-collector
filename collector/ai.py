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
import os
import re
from dataclasses import dataclass, field
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel

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
    prices: dict
    web_search_usd: float = 0.01
    by_model: dict = field(default_factory=dict)

    def add(self, model: str, usage, searches: int = 0) -> None:
        m = self.by_model.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "usd": 0.0, "calls": 0, "searches": 0})
        inp = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        out = usage.output_tokens or 0
        pin, pout = self.prices.get(model, (0, 0))
        m["input_tokens"] += inp
        m["output_tokens"] += out
        m["calls"] += 1
        m["searches"] += searches
        m["usd"] += inp / 1e6 * pin + out / 1e6 * pout + searches * self.web_search_usd

    @property
    def usd(self) -> float:
        return sum(m["usd"] for m in self.by_model.values())

    def totals(self) -> dict:
        return {k: sum(m[k] for m in self.by_model.values()) for k in ("input_tokens", "output_tokens", "calls", "searches")}


# ---------------------------------------------------------------- 추출(작은 모델)
EnglishAttr = Literal["초보 가능", "보호자 동반", "시범 중심", "반복 만남"]
Signup = Literal["fcfs", "lottery", "open", "none", "unknown"]
Bot = Literal["life", "family", "kids"]


class ExtractedEvent(BaseModel):
    title: str
    title_ko: str
    org: Optional[str]
    bot: Bot
    start_date: Optional[str]
    end_date: Optional[str]
    time_text: Optional[str]
    event_tz: Optional[str]
    location: Optional[str]
    city: Optional[str]
    age_text: Optional[str]
    english: list[EnglishAttr]
    cost_text: Optional[str]
    signup: Signup
    registration_opens: Optional[str]
    deadline: Optional[str]
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


EXTRACT_SYSTEM = """You turn raw web items into structured event records for a Korean family living in Westminster, Colorado (Denver metro, ZIP [redacted]) from [stay period redacted].
Household: [redacted - supplied at runtime via HOUSEHOLD_PROFILE secret]
Three audiences: life (adult hobbies, skills, sports, weekday classes, shows, clubs), family (outings, school games/shows, festivals, national parks, special experiences, cruise/travel promotions), kids (after-school, camps, sports clubs, school programs, arts/science classes).

Rules:
- Use only facts present in the item. If a value is not stated, use null (or "unknown" for signup, [] for lists). Never guess dates, prices or ages.
- url must be exactly one of the URLs given for that item. Never invent or edit URLs.
- Dates as YYYY-MM-DD. If the year is missing, infer it only when unambiguous from the item; otherwise null.
- event_tz: IANA zone of the event location (America/Denver for Colorado). time_text: local time as written, e.g. "10:00-12:00".
- title_ko: short natural Korean title. Keep proper nouns in English in parentheses if useful.
- english: pick only attributes the text supports — 초보 가능 (beginners welcome), 보호자 동반 (parent accompanies), 시범 중심 (demonstration-based), 반복 만남 (recurring sessions with the same group).
- signup: fcfs = first come first served / limited spots; lottery = drawing; open = register any time; none = drop-in; unknown.
- tags: 1-4 short lowercase English slugs (e.g. "swim", "camp", "hockey", "museum").
- annual: true if the text says it happens every year or is the Nth annual.
- paid_work: true if participating means paid employment (hiring instructors, paid assistants).
- status_note: Korean note only when the item says something about residency pricing, residency requirements, citizenship, ID or background checks.
- needs_review: true when eligibility, age fit, visa/status fit or whether it is an event at all is unclear. Put the doubt in review_reason (Korean).
- relevant=false (events=[]) for news without an attendable event or program, job postings, ads, or places outside the U.S. unless it is a travel promotion departing Denver.
- One item may contain several distinct events (e.g. a calendar page); list each.
Return results for every item index you were given."""


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


def extract(client, cfg: dict, entries: list, today: str, usage: Usage) -> list[EntryResult]:
    c = cfg["extract"]
    out: list[EntryResult] = []
    for start in range(0, len(entries), c["batch"]):
        chunk = entries[start:start + c["batch"]]
        resp = client.messages.parse(
            model=c["model"],
            max_tokens=c["max_tokens"],
            system=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": build_extract_prompt(chunk, today, c["text_chars"])}],
            output_config={"effort": c["effort"]},
            output_format=ExtractBatch,
        )
        usage.add(c["model"], resp.usage)
        if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
            raise RuntimeError(f"extract stopped: {resp.stop_reason}")
        for r in resp.parsed_output.results:
            if 0 <= r.index < len(chunk):
                out.append(r.model_copy(update={"index": start + r.index}))
    return out


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


JUDGE_SYSTEM = EXTRACT_SYSTEM.split("Rules:")[0] + """Children: [redacted].
You review events a smaller model flagged as uncertain. For each, decide keep (worth sending to this family or local friends), the right audience bot, and correct age_text, status_note (Korean) and english attributes using only the source text. Drop paid jobs, events requiring U.S. citizenship, and items that are not attendable events or programs. Reason in Korean, one sentence."""


def judge(client, cfg: dict, items: list[tuple[dict, str]], usage: Usage) -> list[Verdict]:
    """items: (event dict, source text). 큰 모델 + 서버측 대체 모델(거절 시)."""
    c = cfg["judge"]
    out: list[Verdict] = []
    for start in range(0, len(items), c["batch"]):
        chunk = items[start:start + c["batch"]]
        body = "\n".join(json.dumps({"index": i, "event": ev, "source_text": _clip(txt, 2000)}, ensure_ascii=False) for i, (ev, txt) in enumerate(chunk))
        resp = client.beta.messages.parse(
            model=c["model"],
            max_tokens=c["max_tokens"],
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": body}],
            output_config={"effort": c["effort"]},
            output_format=JudgeBatch,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        usage.add(c["model"], resp.usage)
        if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
            raise RuntimeError(f"judge stopped: {resp.stop_reason}")
        for v in resp.parsed_output.verdicts:
            if 0 <= v.index < len(chunk):
                out.append(v.model_copy(update={"index": start + v.index}))
    return out


# ---------------------------------------------------------------- 발굴(큰 모델 + 웹 검색)
DISCOVER_SYSTEM = """You look for NEW official sources of family, kids and adult activities near Westminster, Colorado (Denver metro) for a Korean family arriving June 2027.
Search the web for the theme you are given. Report:
- sources: official organization pages that list recurring events, classes or registrations (city recreation, libraries, museums, universities, parks, clubs). Prefer pages with calendars or RSS.
- events: specific upcoming events or registration openings worth checking.
Use only URLs that appeared in your search results. Skip pages already listed under "known".
Answer with JSON only, no prose: {"sources":[{"url":"","name":"","reason":"","bots":["kids"]}],"events":[{"url":"","title":"","why":""}]}"""


def _json_from_text(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def discover(client, cfg: dict, theme: str, known: list[str], usage: Usage) -> dict:
    """반환: {"sources": [...], "events": [...], "result_urls": set}. 검색 결과에 실제로 나온 URL 만 남긴다."""
    c = cfg["discover"]
    messages = [{"role": "user", "content": f"Theme: {theme}\nknown: {json.dumps(known[:60])}"}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": c["max_searches"],
              "user_location": {"type": "approximate", "city": "Westminster", "region": "Colorado", "country": "US", "timezone": "America/Denver"}}]
    seen_urls: set[str] = set()
    text = ""
    for _ in range(3):  # pause_turn 이면 이어서 요청
        resp = client.beta.messages.create(
            model=c["model"], max_tokens=c["max_tokens"], system=DISCOVER_SYSTEM, messages=messages, tools=tools,
            output_config={"effort": c["effort"]}, betas=[FALLBACK_BETA], fallbacks="default",
        )
        searches = 0
        for b in resp.content:
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
    sources = [s for s in raw_s if isinstance(s, dict) and ok(s.get("url"))]
    events = [e for e in raw_e if isinstance(e, dict) and ok(e.get("url"))]
    return {"sources": sources, "events": events, "dropped_urls": len(raw_s) + len(raw_e) - len(sources) - len(events), "result_urls": len(seen_urls)}
