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
                                 {"location": cur.get("LOCATION", "")}))
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
    items = _dig(data, mapping["items"]) or []
    out = []
    for it in items if isinstance(items, list) else []:
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
        digest = hashlib.sha1(page_text.encode()).hexdigest()[:12]
        return [Entry(source["id"], normalize_url(base), source.get("name", source["id"]), page_text[:3000], None, {"page_hash": digest})]
    host = _host(base)
    pat = re.compile(source["link_pattern"]) if source.get("link_pattern") else None
    seen, out = set(), []
    for href, label in p.links:
        label = clean_text(label, 300)
        if not href or href.startswith(("mailto:", "tel:", "javascript:")) or len(label) < 8:
            continue
        url = normalize_url(href, base)
        if _host(url) != host or url in seen or url.rstrip("/") == normalize_url(base).rstrip("/"):
            continue
        if pat and not pat.search(url):
            continue
        seen.add(url)
        out.append(Entry(source["id"], url, label))
    return out


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
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"user-agent": USER_AGENT, "accept-language": "en-US,en;q=0.8"})
    try:
        r = client.get(url)
        body = r.content[:MAX_BYTES]
        ctype = r.headers.get("content-type", "").split(";")[0]
        res = FetchResult(r.status_code < 400, r.status_code, ctype, len(r.content))
        text = body.decode(r.encoding or "utf-8", errors="replace")
        if probe:
            res.snippet = clean_text(text, 600)
        if not res.ok:
            res.error = f"HTTP {r.status_code}"
            return res
        sid, base = source["id"], str(r.url)
        t = source["type"]
        if t in ("rss", "gnews"):
            res.entries = parse_feed(text, sid, base)
        elif t == "ical":
            res.entries = parse_ical(text, sid, base)
        elif t == "json":
            res.entries = parse_json(json.loads(text), sid, base, source.get("json"))
        elif t == "html":
            res.entries = parse_html(text, source)
        else:
            res.ok, res.error = False, f"unknown type {t}"
        return res
    except (httpx.HTTPError, ET.ParseError, json.JSONDecodeError, ValueError) as e:
        return FetchResult(False, error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        if own:
            client.close()
