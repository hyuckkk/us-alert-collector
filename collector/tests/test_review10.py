"""10차 전체 검색 시험에서 확인된 결함의 재현 테스트(무료, 실제 공식 원문·실패 사례 기반). 수정 전 실패, 수정 후 통과."""
import json
from datetime import date
from types import SimpleNamespace

import ai
import fetch
import identity
import rules
import run
from conftest import FakeAPI, FakeClient, U, ev
from fetch import Entry, FetchResult

# Thornton WinterFest 공식 페이지 본문(2026-10-21 수집, eval/ground_truth_pages.json). 실제 페이지는 메뉴에 같은 제목이 먼저 나온다.
WINTERFEST_BODY = ("Thornton WinterFest Location, Date & Time Carpenter Park Fields 11000 Colorado Blvd, Thornton, CO 80233 "
                   "Fri, Dec 11 2026, 6:30 - 9pm Sat, Dec 12 2026, 10am - 9pm Sun, Dec 13 2026, 5 - 9pm Event Details Santa's Visitation Schedule")
THORNTON_MENU = ("Skip to main content City of Thornton Menu Community & Culture Festivals & Events Eggcessible Eggstravaganza "
                 "Fourth of July Celebration Lions Club Relays Paws for a Dip Thornton Barrel & Beer Festival Thorntonfest "
                 "Thornton Harvest Fest Thornton WinterFest Trunk or Treat Vendor Information Parks & Recreation Activity Registration "
                 "Information Recreation Centers Rentals Government Departments City Council Residents Business ") * 12  # 실제 실패: 첫 제목 위치에서 3,000자 안에 날짜가 없었음


def test_1_detail_text_starts_at_event_body_not_menu(monkeypatch):
    monkeypatch.setattr(fetch, "page_text", lambda client, url: (200, THORNTON_MENU + WINTERFEST_BODY))
    e = Entry("thornton-festivals", "https://www.thorntonco.gov/community-culture/festivals-events/thornton-winterfest", "Thornton WinterFest")
    assert fetch.fetch_detail(e, client=None)
    assert "Dec 11 2026" in e.text and "11000 Colorado Blvd" in e.text


def test_2_short_rss_items_get_detail_page(monkeypatch, tmp_path):
    """Westminster 일정 RSS 는 본문이 짧아 '무료' 같은 비용 정보가 상세 페이지에만 있다."""
    rss_entry = Entry("westminster-calendar", "https://www.westminsterco.gov/Calendar.aspx?EID=1593",
                      "Trunk or Treat Halloween Spooktacular in Downtown Westminster", "Event date: October 24, 2026", "Wed, 23 Sep 2026 12:38:37 -0700")
    monkeypatch.setattr(run, "fetch_source", lambda s, secrets=None, **k: FetchResult(True, 200, entries=[rss_entry]) if s["id"] == "westminster-calendar"
                        else FetchResult(True, 200, entries=[]))
    monkeypatch.setattr(run, "is_due", lambda s, today: s["id"] == "westminster-calendar")
    opened = []
    monkeypatch.setattr(run, "fetch_detail", lambda e, c, chars=3000: opened.append(e.url) or True)
    real = run.load
    monkeypatch.setattr(run, "load", lambda n: {**real(n), "discover": {**real(n)["discover"], "enabled": False}} if n == "models.json"
                        else ({"daily": {}, "weekday": {}} if n == "queries.json" else real(n)))
    run.main(["--no-ai"], client=FakeClient([]), api=FakeAPI())
    assert rss_entry.url in opened


def test_3_registration_announcements_are_kept():
    """Thornton '겨울·봄 프로그램 접수 시작일' 안내를 AI 가 '행사 아님'으로 버렸다 → 지시문에 명시하고, 규칙 단계에서도 남긴다."""
    s = ai.extract_system()
    assert "registration" in s.lower() and "announcement" in s.lower() and "relevant=true" in s
    assert "registration" in ai.JUDGE_RULES.lower() and "announcement" in ai.JUDGE_RULES.lower()
    reg = ev(title="Winter Swim Lessons Registration", start_date=None, end_date=None, registration_opens="2026-11-30",
             registration_status="upcoming", url="https://city.gov/reg")
    out, dropped = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[reg])],
                                  [Entry("thornton-registration", "https://city.gov/reg", "Activity Registration Information")], date(2026, 10, 21))
    assert len(out) == 1 and out[0]["registration_opens"] == "2026-11-30"


def test_4_undated_items_are_not_sent():
    """날짜를 못 뽑은 Thornton 지난 행사 3건이 알림 대상이 됐다 → 날짜·접수일이 전혀 없으면 보내지 않고 보류로 센다."""
    undated = ev(title="Trunk or Treat", start_date=None, end_date=None, registration_opens=None, deadline=None, annual=False,
                 url="https://www.thorntonco.gov/community-culture/festivals-events/trunk-or-treat")
    out, dropped = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[undated])],
                                  [Entry("thornton-festivals", undated.url, "Trunk or Treat")], date(2026, 10, 21))
    assert out == [] and dropped.get("no-date") == 1


def test_5_same_title_different_dates_and_pages_are_not_merged():
    """DAM 무료입장일 11/7 과 12/8 이 같은 행사로 합쳐져 12/8 이 사라졌다."""
    a = {"title": "SCFD Free Day", "org": "Denver Art Museum", "start_date": "2026-11-07", "url": "https://www.denverartmuseum.org/en/calendar/free-day-nov-7-2026"}
    b = {"title": "SCFD Free Day", "org": "Denver Art Museum", "start_date": "2026-12-08", "url": "https://www.denverartmuseum.org/en/calendar/free-day-dec-8-2026"}
    assert not identity.same_event(a, b)
    # 같은 페이지의 날짜 변경(연기)은 여전히 같은 행사
    assert identity.same_event(a, {**a, "start_date": "2026-11-21"})


def test_6_discovery_prefers_upcoming_and_drops_past():
    """AI 발굴 결과 11건이 대부분 2026년 봄 일정이었다 → 오늘 날짜를 주고, 지난 날짜의 결과는 버린다."""
    text = json.dumps({"sources": [], "events": [
        {"url": "https://x.org/spring", "title": "Spring 2026 league registration", "date": "2026-03-01", "why": ""},
        {"url": "https://x.org/winter", "title": "Winter camp registration", "date": "2026-11-20", "why": ""},
        {"url": "https://x.org/permit", "title": "2028 national park timed-entry permit lottery", "date": "2028-01-15", "why": ""}]})
    resp = SimpleNamespace(content=[SimpleNamespace(type="web_search_tool_result", content=[SimpleNamespace(url="https://x.org/spring"), SimpleNamespace(url="https://x.org/winter"),
                                                                                            SimpleNamespace(url="https://x.org/permit")]),
                                    SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=U())
    c = FakeClient(beta_replies=[resp])
    cfg = {"discover": {"model": "claude-opus-5-5", "effort": "low", "max_searches": 1, "max_tokens": 100}}
    d = ai.discover(c, cfg, "kids camps", [], ai.Usage({}), today="2026-10-21")
    # 지난 일정만 버리고, 가까운 일정과 미리 발표된 먼 미래 정보(2028 허가 추첨)는 모두 남긴다(12차: 기간 제한 없음)
    assert [e["url"] for e in d["events"]] == ["https://x.org/winter", "https://x.org/permit"]
    sent = json.dumps(c.beta.messages.calls[0]["messages"]) + c.beta.messages.calls[0]["system"]
    assert "2026-10-21" in sent and "90 days" not in sent
    for word in ("2027–2028", "early registration", "national park permits", "camp enrollment", "already over"):
        assert word in sent


# ---------------------------------------------------------------- 12차 시험 이후 추가 수정의 회귀 테스트
# Thornton 활동 등록 안내 실제 본문(2026-10-21 수집). 페이지 변경 감시 방식이 앞 3,000자(메뉴)만 넘겨 AI 가 '행사 없음'으로 판단했다.
REG_BODY = ("Activity Registration Information Request an Account Online Activity Registration Activities Guide Release of Liability Waiver "
            "Registration Dates Open all General Registration at 7 a.m. Winter/Spring Registration | Begins November 24 Swim Lessons Residents 6:30 p.m. "
            "Nonresidents 7 a.m. Online and walk-ins only. No Phone-in registrations accepted for swim registration. Winter Swim Registration Begins "
            "Residents November 30, 2026 Nonresidents December 1, 2026 Gymnastics Residents 7 p.m. Nonresidents 7 a.m. Winter Gymnastics Registration "
            "Begins Residents December 2, 2026 Nonresidents December 3, 2026")


def test_7_page_change_source_passes_registration_body_not_menu():
    html = "<html><body><nav>" + ("<a href='/x'>Parks &amp; Recreation Programs Community Culture Government Residents Business</a> " * 90) + \
           "</nav><main><p>" + REG_BODY + "</p></main></body></html>"
    [e] = fetch.parse_html(html, {"id": "thornton-registration", "type": "html", "mode": "page-change",
                                  "url": "https://www.thorntonco.gov/parks-recreation/activity-registration-information"})
    assert "November 30, 2026" in e.text and "December 3, 2026" in e.text and "Begins November 24" in e.text
    assert len(e.text) <= fetch.PAGE_WINDOW


def test_8_extract_rules_session_date_reservations_and_closed_groups():
    s = ai.extract_system()
    assert "ONE session" in s                                      # Westminster 부스 10/27 페이지가 시리즈 첫 회차 10/21 로 추출됨
    for w in ("reservation", "permit", "lottery", "enrollment", "Far-future"):
        assert w in s
    assert "closed group" in s and "military base personnel" in s   # USAFA 상주 인원 가족 영화, CU 학생·교직원 전용
    assert "RSVP closed" in s                                       # 시험 F: USAFA 홈스쿨(RSVP 마감)이 알림 대상이 됨
    assert "cost_text in Korean" in s and "age_text in Korean" in s and "Korean, e.g. \"오전 10시" in s   # 알림은 한국어(장소·기관명은 지도 검색용 영어 유지)


def test_9_closed_or_soldout_registration_is_not_sent():
    full = ev(title="Homeschool Program", start_date="2026-10-30", registration_status="soldout", url="https://u.edu/h")
    wait = ev(title="Camp", start_date="2026-11-30", registration_status="waitlist", url="https://u.edu/c")
    out, dropped = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[full, wait])],
                                  [Entry("usafa-events", "https://u.edu/h", "Homeschool"), ], date(2026, 10, 21))
    assert dropped.get("closed") == 1 and [o["title"] for o in out] == ["Camp"]


# ---------------------------------------------------------------- 13차 전체 시험 C 이후 수정
def test_10_assumed_year_confirmed_when_year_in_same_text():
    """Thornton 'Winter/Spring Registration | Begins November 24'(연도 없음)는 같은 글에 2026 이 있어도 '연도 추정'이 되어 ⏰ 알림을 못 받았다."""
    reg = ev(title="Winter/Spring General Registration", start_date=None, end_date=None, registration_opens="2026-11-24",
             registration_status="upcoming", year_basis="assumed", url="https://city.gov/reg")
    entry = Entry("thornton-registration", "https://city.gov/reg", "Activity Registration Information", REG_BODY)
    [out], _ = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[reg])], [entry], date(2026, 10, 21))
    assert not out.get("date_uncertain")
    # 연도가 원문에 없거나 먼 날짜면 여전히 '추정'
    far = ev(title="Spring Camp", start_date="2027-06-01", year_basis="assumed", url="https://city.gov/reg")
    [o2], _ = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[far])], [entry], date(2026, 10, 21))
    assert o2.get("date_uncertain")
    bare = Entry("x", "https://city.gov/reg", "Swim", "Registration begins November 24")
    [o3], _ = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[reg])], [bare], date(2026, 10, 21))
    assert o3.get("date_uncertain")


def test_11_until_month_rule_in_extract_prompt():
    s = ai.extract_system()
    assert "last day of that month" in s and "keeps its real start_date" in s


def test_12_truncated_extract_batch_is_split_and_retried():
    """시험 D: 12건 묶음 응답이 출력 한도에서 잘려(JSON EOF) 24건이 추출되지 못했다 → 반으로 나눠 바로 다시 보낸다."""
    entries = [Entry("s", f"https://x.org/{i}", f"Event {i}", "Saturday, November 7, 2026 at the library") for i in range(4)]

    def reply(kw):
        n = kw["messages"][0]["content"].count("https://x.org/")
        if n > 2:
            raise ValueError("1 validation error for ExtractBatch\n  Invalid JSON: EOF while parsing a string at line 1 column 23765")
        return ai.ExtractBatch(results=[ai.EntryResult(index=j, relevant=True, reason="", events=[]) for j in range(n)])

    c = FakeClient([reply] * 3)
    cfg = {"extract": {"model": "claude-haiku-5-5", "effort": "low", "batch": 4, "max_tokens": 16000, "text_chars": 4500}}
    u = ai.Usage({"claude-haiku-5-5": {"input": 0.1, "output": 0.5}})
    out = ai.extract(c, cfg, entries, "2026-10-21", u)
    assert sorted(out.results) == [0, 1, 2, 3] and not out.failed
    assert u.by_model["claude-haiku-5-5"]["output_tokens"] >= 16000   # 잘린 응답의 사용량도 추정 기록


def test_13_json_api_reads_following_pages():
    """CU 행사 API 는 60일 행사가 100건을 넘는데 첫 페이지만 읽어 'Mornings at the Museum' 등이 빠졌다."""
    pages = {1: [f"a{i}" for i in range(3)], 2: ["b0", "b1"], 3: []}

    class C:
        calls = []

        def get(self, url):
            self.calls.append(url)
            n = int(dict(fetch.urllib.parse.parse_qsl(fetch.urllib.parse.urlsplit(url).query)).get("page", 1))
            body = json.dumps({"events": [{"event": {"title": t, "localist_url": f"https://c.edu/{t}", "first_date": "2026-11-01"}} for t in pages.get(n, [])]})
            return SimpleNamespace(content=body.encode(), status_code=200, headers={"content-type": "application/json"}, encoding="utf-8", url=url)

    src = {"id": "cu", "type": "json", "url": "https://c.edu/api/2/events?days=60&pp=100",
           "json": {"items": "events", "title": "event.title", "url": "event.localist_url", "date": "event.first_date", "pages": {"param": "page", "max": 5}}}
    c = C()
    r = fetch.fetch_source(src, client=c)
    assert r.ok and [e.title for e in r.entries] == ["a0", "a1", "a2", "b0", "b1"]
    assert len(c.calls) == 3 and "page=2" in c.calls[1] and "pp=100" in c.calls[1]


def test_14_entities_decoded_and_old_news_posts_skipped():
    assert fetch.clean_text("Air &#038; Space &#8211; Bingo&nbsp;Night") == "Air & Space – Bingo Night"
    rss = ("<rss><channel>"
           "<item><title>Timed entry reservations open Nov 1</title><link>https://nps.gov/a</link><pubDate>" + "Mon, 19 Oct 2026 10:00:00 GMT" + "</pubDate></item>"
           "<item><title>Timed entry 2019 pilot</title><link>https://nps.gov/b</link><pubDate>Mon, 07 Oct 2019 10:00:00 GMT</pubDate></item>"
           "</channel></rss>")

    class C:
        def get(self, url):
            return SimpleNamespace(content=rss.encode(), status_code=200, headers={"content-type": "application/rss+xml"}, encoding="utf-8", url=url)

    r = fetch.fetch_source({"id": "rmnp-news", "type": "rss", "url": "https://nps.gov/feed", "max_age_days": 60}, client=C())
    assert [e.url for e in r.entries] == ["https://nps.gov/a"]


def test_15_closed_marker_on_single_event_page_blocks_send():
    """시험 F·G: USAFA 홈스쿨 상세 페이지 'Currently full RSVP Closed' 인데 AI 가 registration_status=unknown → 발송됨."""
    text = ("Homeschool Program: The Outer Solar System Friday, October 30 @ 10:30 am - 11:45 am Free ... With our limited seating, if you RSVP "
            "and your plans change ... 120 Going Currently full RSVP Closed « USAFA Family Movie")
    hs = ev(title="Homeschool Program", start_date="2026-10-30", registration_status="unknown", url="https://u.edu/h")
    e = Entry("usafa-events", "https://u.edu/h", "Homeschool Program", text)
    out, dropped = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[hs])], [e], date(2026, 10, 21))
    assert out == [] and dropped.get("closed") == 1
    # 대기자 명단이 있으면 보낸다, 페이지 변경 감시(여러 프로그램이 한 페이지)는 건드리지 않는다
    w = Entry("x", "https://u.edu/h", "Camp", "Camp is currently full - join the waitlist")
    [o], _ = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[hs])], [w], date(2026, 10, 21))
    assert o["registration_status"] == "waitlist"
    pc = Entry("x", "https://u.edu/h", "Programs", "Swim: sold out. Gymnastics: open", extra={"page_hash": "abc"})
    [o2], _ = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[hs])], [pc], date(2026, 10, 21))
    assert o2["registration_status"] == "unknown"


def test_16_closed_status_change_of_sent_event_still_goes_to_worker():
    """이미 보낸 행사가 재검사에서 매진·마감으로 바뀌면 버리지 않고 '변경(접수 상태)'으로 보낸다."""
    hs = ev(title="Kids Clay Workshop", start_date="2027-07-17", registration_status="soldout", url="https://b.org/e/1")
    e = Entry("butterfly-pavilion", "https://b.org/e/1", "[상세 변경] registration_status", "Kids Clay Workshop. Sold out.", None, {"page_fp": "f1"})
    [o], dropped = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[hs])], [e], date(2026, 10, 21))
    assert o["registration_status"] == "soldout" and not dropped.get("closed")
