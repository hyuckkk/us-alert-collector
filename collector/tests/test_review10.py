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
        {"url": "https://x.org/winter", "title": "Winter camp registration", "date": "2026-11-20", "why": ""}]})
    resp = SimpleNamespace(content=[SimpleNamespace(type="web_search_tool_result", content=[SimpleNamespace(url="https://x.org/spring"), SimpleNamespace(url="https://x.org/winter")]),
                                    SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=U())
    c = FakeClient(beta_replies=[resp])
    cfg = {"discover": {"model": "claude-opus-5-5", "effort": "low", "max_searches": 1, "max_tokens": 100}}
    d = ai.discover(c, cfg, "kids camps", [], ai.Usage({}), today="2026-10-21")
    assert [e["url"] for e in d["events"]] == ["https://x.org/winter"]
    sent = json.dumps(c.beta.messages.calls[0]["messages"]) + c.beta.messages.calls[0]["system"]
    assert "2026-10-21" in sent and "upcoming" in sent.lower()
