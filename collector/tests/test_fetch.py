import httpx

from fetch import EVENTISH, Entry, fetch_detail, fetch_source, gnews_url, normalize_url, parse_feed, parse_html, parse_ical, parse_json


def test_normalize_strips_tracking_and_fragment():
    assert normalize_url("HTTPS://Ex.org/a?utm_source=x&id=3#top") == "https://ex.org/a?id=3"
    assert normalize_url("/e/1", "https://ex.org/list/") == "https://ex.org/e/1"


def test_rss_and_atom():
    rss = "<rss><channel><item><title>Camp</title><link>https://x.org/c</link><description>&lt;p&gt;Fun&lt;/p&gt;</description><pubDate>Mon</pubDate></item></channel></rss>"
    e = parse_feed(rss, "s", "https://x.org")[0]
    assert (e.title, e.url, e.text, e.published) == ("Camp", "https://x.org/c", "Fun", "Mon")
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Talk</title><link href="https://y.org/t"/><summary>Hi</summary></entry></feed>'
    assert parse_feed(atom, "s", "https://y.org")[0].url == "https://y.org/t"


def test_ical_folding_and_escapes():
    ics = "BEGIN:VEVENT\r\nSUMMARY:Hockey vs\r\n  CU\r\nDTSTART;TZID=America/Denver:20261101T190000\r\nLOCATION:Arena\\, Denver\r\nEND:VEVENT"
    e = parse_ical(ics, "s", "https://d.com/cal.ics")[0]
    assert e.title == "Hockey vs CU"
    assert e.published == "20261101T190000"
    assert e.extra["location"] == "Arena, Denver"
    assert e.url.startswith("https://d.com/cal.ics#")


def test_json_mapping():
    data = {"data": [{"title": "Ranger Talk", "infourl": "https://nps.gov/x", "datestart": "2027-07-01", "description": "<b>Join</b>"}]}
    e = parse_json(data, "nps", "https://nps.gov", {"items": "data", "title": "title", "url": "infourl", "date": "datestart", "text": "description"})[0]
    assert (e.title, e.text, e.published) == ("Ranger Talk", "Join", "2027-07-01")


def test_html_keeps_event_links_only():
    html = ('<nav><a href="/visit">Plan Your Visit Today</a></nav><a href="/events/clay">Kids Clay Workshop</a>'
            '<a href="https://other.org/events/x">Other site event link</a><a href="/events/">Events</a><script>var a</script>')
    out = parse_html(html, {"id": "b", "url": "https://www.b.org/"})
    assert [e.url for e in out] == ["https://www.b.org/events/clay"]
    assert EVENTISH.search("/calendar/2027")


def test_page_change_mode_hashes_text():
    a = parse_html("<p>Reservations open May 1</p>", {"id": "r", "url": "https://n.gov/p", "mode": "page-change"})[0]
    b = parse_html("<p>Reservations open May 2</p>", {"id": "r", "url": "https://n.gov/p", "mode": "page-change"})[0]
    assert a.url == b.url and a.extra["page_hash"] != b.extra["page_hash"]


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_source_errors_are_reported_not_raised():
    r = fetch_source({"id": "x", "type": "rss", "url": "https://x.org/f"}, client=_client(lambda req: httpx.Response(403)))
    assert (r.ok, r.status, r.error) == (False, 403, "HTTP 403")
    r = fetch_source({"id": "x", "type": "rss", "url": "https://x.org/f"}, client=_client(lambda req: httpx.Response(200, content=b"")))
    assert (r.ok, r.error) == (False, "empty body")
    r = fetch_source({"id": "x", "type": "rss", "url": "https://x.org/f"}, client=_client(lambda req: httpx.Response(200, content=b"<rss><bad")))
    assert r.ok is False and r.error.startswith("ParseError")


def test_secret_required_source_is_skipped_without_key_and_key_not_in_result():
    r = fetch_source({"id": "n", "type": "json", "url": "https://api.x/e?a=1", "requires_secret": "NPS_API_KEY"}, secrets={})
    assert r.error == "skipped: NPS_API_KEY not set"
    seen = []
    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"data": []})
    r = fetch_source({"id": "n", "type": "json", "url": "https://api.x/e?a=1", "requires_secret": "NPS_API_KEY", "json": {"items": "data", "title": "t", "url": "u"}},
                     secrets={"NPS_API_KEY": "k1"}, client=_client(h))
    assert r.ok and "api_key=k1" in seen[0] and "k1" not in (r.error or "")


def test_gnews_url():
    assert gnews_url("kids camp").startswith("https://news.google.com/rss/search?q=kids+camp&hl=en-US")


def test_fetch_detail_skips_menu_before_title():
    html = b"<nav>Home Visit Shop</nav><h1>Kids Clay Workshop</h1><p>July 10, 2027 - Ages 6-10 - $20</p><script>x</script>"
    e = Entry("b", "https://b.org/events/clay", "Kids Clay Workshop")
    assert fetch_detail(e, _client(lambda req: httpx.Response(200, content=html))) is True
    assert e.text.startswith("Kids Clay Workshop") and "Ages 6-10" in e.text and "Shop" not in e.text
    e2 = Entry("b", "https://b.org/x", "X")
    assert fetch_detail(e2, _client(lambda req: httpx.Response(404))) is False and e2.extra["detail_error"] == "HTTP 404"


def test_unexpected_payload_shape_is_an_error_not_a_crash():
    # JSON 의 items 가 문자열 목록인 경우(경로 매핑과 맞지 않음) — 1차 시험 조회에서 실행이 멈춘 종류의 오류
    src = {"id": "j", "type": "json", "url": "https://x.org/j", "json": {"items": "events", "title": "event.title", "url": "event.url"}}
    r = fetch_source(src, client=_client(lambda req: httpx.Response(200, json={"events": ["a", "b"]})))
    assert r.ok is False and r.error == "json mapping matched 0 items"
    r = fetch_source(src, client=_client(lambda req: httpx.Response(200, json=["not", "a", "dict"])))
    assert r.ok is False and r.error


def test_probe_runs_with_current_config(monkeypatch, tmp_path):
    import probe
    from fetch import FetchResult
    monkeypatch.setattr(probe, "fetch_source", lambda s, **k: FetchResult(False, 403, error="HTTP 403"))
    monkeypatch.setattr(probe, "ROOT", probe.ROOT)
    out = tmp_path / "reports"
    monkeypatch.setattr(probe.Path, "mkdir", lambda self, **k: None) if False else None
    assert probe.main() == 0
