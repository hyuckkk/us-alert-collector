"""무토큰 수집: 정보원을 프로그램이 조회해 항목(Entry)으로 바꾼다. AI 를 쓰지 않는다.

지원 형식: rss/atom, ical, json(경로 매핑), html(새 링크 또는 페이지 변경), gnews(Google News RSS 검색).
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; us-alert-collector/1.0; +https://github.com/hyuckkk/us-alert-collector)"
TIMEOUT = 25.0
MAX_BYTES = 3_000_000
# html 정보원에 link_pattern 이 없을 때 쓰는 기본 거르기: 행사·강좌·소식으로 보이는 주소만 남겨 메뉴 링크를 뺀다
EVENTISH = re.compile(r"/(events?|calendar|programs?|class(es)?|camps?|activit|exhibit|news|release|register|tickets?|schedule|workshop|festival)", re.I)
TRACKING = re.compile(r"^(utm_[a-z]+|fbclid|gclid|mc_[a-z]+|_hs[a-z]+|ref)$", re.I)


@dataclass
class Entry:
    source_id: str
    url: str
    title: str
    text: str = ""
    published: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class FetchResult:
    ok: bool
    status: int | None = None
    content_type: str = ""
    size: int = 0
    entries: list[Entry] = field(default_factory=list)
    error: str | None = None
    snippet: str = ""
    filtered: int = 0


def normalize_url(url: str, base: str | None = None) -> str:
    """추적용 파라미터와 #조각을 떼어 같은 글이 다른 주소로 두 번 잡히지 않게 한다."""
    if base:
        url = urllib.parse.urljoin(base, url)
    p = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urllib.parse.urlencode(q), ""))


def clean_text(s: str | None, limit: int = 2000) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&nbsp;|&#160;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _host(url: str) -> str:
    h = urllib.parse.urlsplit(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


# ---------------------------------------------------------------- parsers
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text: str, source_id: str, base: str) -> list[Entry]:
    root = ET.fromstring(xml_text)
    out = []
    for node in root.iter():
        name = _local(node.tag)
        if name not in ("item", "entry"):
            continue
        get = {}
        link = None
        for c in node:
            cn = _local(c.tag)
            if cn == "link":
                link = c.get("href") or (c.text or "").strip() or link
            elif cn not in get:
                get[cn] = (c.text or "").strip()
        title = clean_text(get.get("title"), 300)
        if not link or not title:
            continue
        out.append(Entry(source_id, normalize_url(link, base), title,
                         clean_text(get.get("description") or get.get("summary") or get.get("content")),
                         get.get("pubDate") or get.get("published") or get.get("updated")))
    return out


def _ics_unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_ical(text: str, source_id: str, base: str) -> list[Entry]:
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()  # 줄 이어붙이기(RFC 5545 folding)
    out, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
        elif ln == "END:VEVENT" and cur is not None:
            title = clean_text(cur.get("SUMMARY", ""), 300)
            if title:
                url = cur.get("URL") or f"{base}#{cur.get('UID', title)}"
                text = clean_text(" ".join(filter(None, [cur.get("LOCATION", ""), cur.get("DESCRIPTION", "")])))
                out.append(Entry(source_id, normalize_url(url, base) if cur.get("URL") else url, title, text, cur.get("DTSTART"),
                                 {"location": cur.get("LOCATION", ""), "uid": cur.get("UID")}))
            cur = None
        elif cur is not None and ":" in ln:
            k, v = ln.split(":", 1)
            cur.setdefault(k.split(";", 1)[0].upper(), _ics_unescape(v))
    return out


def _dig(obj, path: str):
    for part in path.split("."):
        if obj is None:
            return None
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def parse_json(data, source_id: str, base: str, mapping: dict | None) -> list[Entry]:
    if not mapping:
        return []
    items = _dig(data, mapping["items"]) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"json mapping: '{mapping['items']}' is not a list")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = clean_text(str(_dig(it, mapping["title"]) or ""), 300)
        url = _dig(it, mapping["url"])
        if not title or not url:
            continue
        text = _dig(it, mapping.get("text", "")) if mapping.get("text") else ""
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False)
        out.append(Entry(source_id, normalize_url(str(url), base), title, clean_text(str(text or "")),
                         str(_dig(it, mapping["date"])) if mapping.get("date") else None))
    return out


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self._href = None
        self._buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._buf)))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._href is not None:
            self._buf.append(data)


def parse_html(html: str, source: dict) -> list[Entry]:
    p = _LinkParser()
    p.feed(html)
    base = source["url"]
    page_text = clean_text(" ".join(p.text), 6000)
    if source.get("mode") == "page-change":
        # 페이지 본문 해시가 바뀌면 한 건으로 넘긴다. 같은 페이지 주소 + 본문 해시가 '이미 본' 판정 단위.
        # AI 에는 페이지 앞부분(메뉴)이 아니라 날짜·접수·비용 문장이 가장 많은 구간을 넘긴다(12차 시험: Thornton 접수 안내 누락).
        full = clean_text(" ".join(p.text), 20000)
        digest = hashlib.sha1(fingerprint(full)[1].encode()).hexdigest()[:12]
        return [Entry(source["id"], normalize_url(base), source.get("name", source["id"]), best_window(full, PAGE_WINDOW), None, {"page_hash": digest})]
    host = _host(base)
    pat = re.compile(source["link_pattern"]) if source.get("link_pattern") else EVENTISH
    seen, out = set(), []
    for href, label in p.links:
        label = clean_text(label, 300)
        if not href or href.startswith(("mailto:", "tel:", "javascript:")) or len(label) < 8:
            continue
        url = normalize_url(href, base)
        if _host(url) != host or url in seen or url.rstrip("/") == normalize_url(base).rstrip("/"):
            continue
        if not pat.search(urllib.parse.urlsplit(url).path + "?" + urllib.parse.urlsplit(url).query):
            continue
        seen.add(url)
        out.append(Entry(source["id"], url, label))
    return out


# ---------------------------------------------------------------- 상세 페이지
# 행사 정보와 관련된 문장만 골라 지문을 만든다(메뉴·광고·장식 변경으로 AI 를 다시 부르지 않기 위함)
RELEVANT = re.compile(
    r"(\$\s?\d|\bfree\b|\bfee|\bprice|\bcost|regist|sign[- ]?up|deadline|enroll|waitlist|wait list|sold out|spots?\b|capacity|seats?\b|"
    r"cancel|postpone|reschedul|\bages?\b|grades?\b|\byears? old|\blocation|\bvenue|\baddress|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}|\b\d{1,2}/\d{1,2}(/\d{2,4})?\b|\b\d{1,2}(:\d{2})?\s?(am|pm)\b|\b20\d\d-\d\d-\d\d)",
    re.I)


def fingerprint(text: str) -> tuple[str, str]:
    """반환: (해시, 관련 문장 묶음). 관련 문장이 하나도 없으면 본문 앞부분으로 대신한다."""
    parts = re.split(r"(?<=[.!?])\s+|\s{2,}|\s[•|·]\s", text or "")
    keep = [re.sub(r"\s+", " ", p).strip().lower() for p in parts if RELEVANT.search(p)]
    basis = " | ".join(dict.fromkeys(k for k in keep if 3 < len(k) < 400)) or (text or "")[:1500].lower()
    return hashlib.sha1(basis.encode()).hexdigest()[:16], basis[:3000]


def page_text(client: httpx.Client, url: str) -> tuple[int | None, str]:
    r = client.get(url)
    if r.status_code >= 400 or not r.content:
        return r.status_code, ""
    p = _LinkParser()
    p.feed(r.content[:MAX_BYTES].decode(r.encoding or "utf-8", errors="replace"))
    return r.status_code, clean_text(" ".join(p.text), 20000)


PAGE_WINDOW = 4000


def best_window(text: str, size: int = PAGE_WINDOW) -> str:
    """긴 페이지에서 날짜·장소·비용·신청 문장이 가장 많이 모인 size 글자 구간(메뉴·바닥글 대신 본문)."""
    if len(text) <= size:
        return text
    starts = sorted({*range(0, len(text) - size + 1, 250), len(text) - size})  # 끝 구간도 후보에 넣는다
    best = max(starts, key=lambda s: (len(RELEVANT.findall(text[s:s + size])), -s))
    return text[best:best + size]


def event_body_start(text: str, title: str) -> int:
    """제목은 사이트 메뉴·이동 경로에도 나오므로 첫 위치가 아니라, 제목이 나오는 곳(최대 12곳) 중
    뒤따르는 1,500자에 날짜·장소·비용·신청 문장이 가장 많은 곳을 본문 시작으로 본다(10차 시험: 메뉴부터 잘라 날짜 누락)."""
    low, key = text.lower(), (title or "").lower()[:40]
    hits, i = [], low.find(key) if key else -1
    while i >= 0 and len(hits) < 12:
        hits.append(i)
        i = low.find(key, i + 1)
    if not hits:
        # 제목이 본문에 없으면(사이트가 다른 이름을 씀) 행사 문장이 가장 많은 구간
        w = best_window(text, 1500)
        return max(text.find(w), 0)
    return max(hits, key=lambda h: (len(RELEVANT.findall(text[h:h + 1500])), -h))


def fetch_detail(entry: Entry, client: httpx.Client, chars: int = 4500) -> bool:
    """html 정보원의 새 링크는 링크 글자만 있으므로, 상세 페이지 본문을 가져와 AI 가 날짜·가격·연령을 볼 수 있게 한다(토큰 0)."""
    try:
        status, text = page_text(client, entry.url)
        if not text:
            entry.extra["detail_error"] = f"HTTP {status}"
            return False
        entry.extra["fingerprint"] = fingerprint(text)[0]
        start = event_body_start(text, entry.title)
        entry.text = text[start:start + chars]
        return True
    except httpx.HTTPError as e:
        entry.extra["detail_error"] = type(e).__name__
        return False


def new_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"user-agent": USER_AGENT, "accept-language": "en-US,en;q=0.8"})


# ---------------------------------------------------------------- fetch
def gnews_url(query: str) -> str:
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})


def fetch_source(source: dict, secrets=None, probe: bool = False, client: httpx.Client | None = None) -> FetchResult:
    secrets = secrets or {}
    url = gnews_url(source["query"]) if source["type"] == "gnews" else source["url"]
    need = source.get("requires_secret")
    if need:
        if not secrets.get(need):
            return FetchResult(False, error=f"skipped: {need} not set")
        url += ("&" if "?" in url else "?") + "api_key=" + urllib.parse.quote(secrets[need])
    own = client is None
    client = client or new_client()
    try:
        try:
            r = client.get(url)
        except httpx.TimeoutException:
            r = client.get(url)  # 시간 초과는 한 번만 다시 시도(같은 공식 정보원이 가끔 느림)
        body = r.content[:MAX_BYTES]
        ctype = r.headers.get("content-type", "").split(";")[0]
        res = FetchResult(r.status_code < 400, r.status_code, ctype, len(r.content))
        text = body.decode(r.encoding or "utf-8", errors="replace")
        if probe:
            res.snippet = clean_text(text, 600)
        if not res.ok:
            res.error = f"HTTP {r.status_code}"
            return res
        if not body.strip():
            res.ok, res.error = False, "empty body"
            return res
        sid, base = source["id"], str(r.url)
        t = source["type"]
        if t in ("rss", "gnews"):
            res.entries = parse_feed(text, sid, base)
        elif t == "ical":
            res.entries = parse_ical(text, sid, base)
        elif t == "json":
            data = json.loads(text)
            res.entries = parse_json(data, sid, base, source.get("json"))
            if source.get("json") and not res.entries and isinstance(data, dict) and _dig(data, source["json"]["items"]):
                res.ok, res.error = False, "json mapping matched 0 items"
        elif t == "html":
            res.entries = parse_html(text, source)
        else:
            res.ok, res.error = False, f"unknown type {t}"
        # 제목으로 미리 거르기(토큰 0): 예) 대학 경기 일정에서 원정 경기·관심 밖 종목 제외
        inc = re.compile(source["include"], re.I) if source.get("include") else None
        exc = re.compile(source["exclude"], re.I) if source.get("exclude") else None
        if inc or exc:
            before = len(res.entries)
            res.entries = [e for e in res.entries if (not inc or inc.search(e.title)) and not (exc and exc.search(e.title))]
            res.filtered = before - len(res.entries)
        return res
    except Exception as e:  # noqa: BLE001  정보원 하나의 이상한 응답이 실행 전체를 멈추지 않게
        return FetchResult(False, error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        if own:
            client.close()
