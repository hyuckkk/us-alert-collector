"""2027-28 미국 계절 행사 검색 누락 방지 및 과다 알림 억제 단위 테스트."""
from datetime import date
import re

import run
from fetch import parse_html


def q():
    return run.load("queries.json")


def seasonal_names(day):
    return {season["id"] for season in run.active_seasons(q(), day)}


def seasonal_searches(day):
    return [item for item in run.queries_for(q(), day) if "2027" in item[1] or "2028" in item[1]]


def test_no_extra_2026_halloween_searches_before_us_arrival():
    assert not seasonal_names(date(2026, 10, 31))
    assert not seasonal_searches(date(2026, 10, 31))
    assert "Westminster Colorado family events" in [text for _, text in run.queries_for(q(), date(2026, 10, 31))]


def test_halloween_weekly_and_daily_near_date():
    # 월/목 2회: 2027-10-04(월), 10-07(목)
    assert "halloween_fall" in seasonal_names(date(2027, 10, 4))
    thursday = seasonal_searches(date(2027, 10, 7))
    assert any("Westy Fest 2027" in term for _, term in thursday)
    assert any("Crawl-O-Ween 2027" in term for _, term in thursday)
    assert any(bot == "kids" for bot, _ in thursday)
    # 금요일: 평시에는 없음, 10/15부터는 매일
    assert not seasonal_searches(date(2027, 10, 8))
    assert any("Halloween" in term for _, term in seasonal_searches(date(2027, 10, 15)))
    assert any("Westy Fest 2027" in term for _, term in seasonal_searches(date(2027, 10, 31)))
    assert "halloween_fall" not in seasonal_names(date(2027, 11, 1))
    assert "halloween_fall" not in seasonal_names(date(2028, 10, 7))


def test_winter_and_independence_cover_stay_without_old_year_false_dates():
    assert "independence_day" in seasonal_names(date(2027, 6, 25))
    assert "independence_day" in seasonal_names(date(2028, 7, 4))
    assert "independence_day" not in seasonal_names(date(2028, 7, 6))
    christmas = seasonal_searches(date(2027, 12, 15))
    assert any("Christmas" in s and "2027" in s for _, s in christmas)
    assert "winter_holidays" not in seasonal_names(date(2028, 12, 15))
    assert not any("Halloween" in x for _, x in seasonal_searches(date(2028, 10, 15)))


def test_overlap_and_existing_queries_not_lost():
    today = date(2027, 10, 16)  # 토요일, 겨울휴일 정기 검색 + 핼러윈 매일 검색
    terms = run.queries_for(q(), today)
    assert len(terms) == len(set(terms))
    assert any("Christmas" in t for _, t in terms)
    assert any("Westy Fest" in t for _, t in terms)
    assert any("Denver flight deal family" == t for _, t in terms)


def test_westminster_official_special_event_source_catches_numeric_city_paths():
    s = next(x for x in run.load("sources.json")["sources"] if x["id"] == "westminster-special-events")
    assert s["active_from"] == "2027-06-01"
    assert s["official"] and "family" in s["bots"]
    html = """
    <a href="/1144/Westy-Fest-2027">Westy Fest 2027 event details</a>
    <a href="/Calendar.aspx?EID=1701">City Park fall carnival</a>
    <a href="/1234/Regular-City-Council">Regular City Council Meeting</a>
    """
    entries = parse_html(html, s)
    assert len(entries) == 2
    assert all(e.url.startswith("https://www.westminsterco.gov/") for e in entries)
    assert any("Westy-Fest-2027" in e.url for e in entries)


def test_seasonal_config_is_bounded_and_does_not_invent_event_date():
    for season in q()["seasonal"]:
        assert season["start_year"] >= 2027
        assert season["end_year"] <= 2028
        assert re.fullmatch(r"\d{2}-\d{2}", season["start"])
        assert re.fullmatch(r"\d{2}-\d{2}", season["end"])
        assert len(season["weekdays"]) >= 2
        assert any("{year}" in term for terms in season["queries"].values() for term in terms)
        assert not any("2026" in term for terms in season["queries"].values() for term in terms)


def test_seasonal_official_source_not_fetched_before_june_2027(monkeypatch):
    from types import SimpleNamespace
    from conftest import FakeAPI
    from fetch import FetchResult

    src = next(x for x in run.load("sources.json")["sources"] if x["id"] == "westminster-special-events")
    real_load = run.load
    monkeypatch.setattr(run, "load", lambda name: {"sources": [src]} if name == "sources.json" else real_load(name))
    monkeypatch.setattr(run, "is_due", lambda source, day: True)
    fetched = []

    def fake_fetch(source, secrets=None):
        fetched.append(source["id"])
        return FetchResult(True, 200, entries=[])

    monkeypatch.setattr(run, "fetch_source", fake_fetch)
    args = SimpleNamespace(mode="full", dry_run=True, only=None, all_sources=True)
    runner = run.Run(args, api=FakeAPI())
    runner.today = date(2026, 10, 31)
    assert runner.sources()["due"] == 0
    assert fetched == []
    runner.today = date(2027, 6, 1)
    assert runner.sources()["due"] == 1
    assert fetched == ["westminster-special-events"]
