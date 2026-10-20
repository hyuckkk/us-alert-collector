"""1차 검증 1-1 재현: 옛 event_key(제목+시작일+도시, 영문·숫자만)는 아래 사례에서 틀린다. 새 식별 체계로 모두 통과해야 한다."""
import identity as I
from fetch import Entry


def ev(title, start, url, org="Butterfly Pavilion", **k):
    return {"title": title, "start_date": start, "url": url, "org": org, "city": "Westminster", "source_id": k.pop("source_id", "butterfly-pavilion"), **k}


def keyed(events, index=(), official=True, entry=None):
    for e in events:
        e["_anchor"] = I.anchor(e, entry, official=official)
    I.assign_keys(events, list(index))
    return events


def as_index(events):
    return [{k: e.get(k) for k in ("event_key", "title", "org", "url", "start_date", "identity")} for e in events]


def old_key(title, start, city):  # 1차 코드의 방식(비교용)
    import hashlib
    norm = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return "e" + hashlib.sha1(f"{norm}|{start or ''}|{(city or '').lower()}".encode()).hexdigest()[:15]


import re  # noqa: E402

URL = "https://butterflies.org/event/family-flashlight-tours/"


def test_date_change_keeps_key():
    a = keyed([ev("Family Flashlight Tours", "2027-07-10", URL)])[0]
    b = keyed([ev("Family Flashlight Tours", "2027-07-17", URL)], index=as_index([a]))[0]
    assert a["event_key"] == b["event_key"]
    assert old_key("Family Flashlight Tours", "2027-07-10", "Westminster") != old_key("Family Flashlight Tours", "2027-07-17", "Westminster")  # 옛 방식은 새 행사로 봄


def test_venue_change_keeps_key():
    a = keyed([ev("Kids Clay Workshop", "2027-07-10", "https://butterflies.org/event/clay/", location="Room A")])[0]
    b = keyed([ev("Kids Clay Workshop", "2027-07-10", "https://butterflies.org/event/clay/", location="Main Hall", city="Denver")], index=as_index([a]))[0]
    assert a["event_key"] == b["event_key"]


def test_partial_title_edit_keeps_key_even_without_url_anchor():
    # 검색 결과처럼 주소 기준이 없는 행사: 제목 일부가 바뀌어도 같은 기관·같은 날짜면 같은 행사
    a = keyed([ev("Youth Basketball League Registration", "2027-01-05", "https://news.example.com/a", org="Hyland Hills", source_id="gnews:x")], official=False)[0]
    b = keyed([ev("Youth Basketball League Registration Now Open", "2027-01-05", "https://news.example.com/b", org="Hyland Hills", source_id="gnews:y")],
              index=as_index([a]), official=False)[0]
    assert a["event_key"] == b["event_key"]


def test_korean_only_titles_do_not_collide():
    a, b = keyed([ev("어린이 미술 교실", "2027-07-10", "https://k.example.org/list", org="한인회"),
                  ev("성인 요가 교실", "2027-07-10", "https://k.example.org/list", org="한인회")], official=False)
    assert a["event_key"] != b["event_key"]
    assert old_key("어린이 미술 교실", "2027-07-10", "Westminster") == old_key("성인 요가 교실", "2027-07-10", "Westminster")  # 옛 방식 충돌 재현


def test_multiple_sessions_on_same_page_stay_separate_and_stable():
    first = keyed([ev("Family Flashlight Tours", "2027-07-10", URL), ev("Family Flashlight Tours", "2027-07-17", URL), ev("Family Flashlight Tours", "2027-07-24", URL)])
    assert len({e["event_key"] for e in first}) == 3
    # 다음 날 다시 수집해도 같은 키
    again = keyed([ev("Family Flashlight Tours", "2027-07-10", URL), ev("Family Flashlight Tours", "2027-07-17", URL), ev("Family Flashlight Tours", "2027-07-24", URL)],
                  index=as_index(first))
    assert [e["event_key"] for e in again] == [e["event_key"] for e in first]


def test_title_anchored_far_apart_dates_are_different_occurrences():
    spring = keyed([ev("Kids Swim Lessons", "2027-03-01", "https://n.example.com/1", org="Westminster Rec", source_id="gnews:x")], official=False)[0]
    fall = keyed([ev("Kids Swim Lessons", "2027-09-10", "https://n.example.com/2", org="Westminster Rec", source_id="gnews:x")], index=as_index([spring]), official=False)[0]
    assert spring["event_key"] != fall["event_key"]


def test_2027_and_2028_editions_are_different():
    a = keyed([ev("Crawl-o-ween", "2027-10-25", "https://butterflies.org/event/crawl-o-ween/")])[0]
    b = keyed([ev("Crawl-o-ween", "2028-10-23", "https://butterflies.org/event/crawl-o-ween/")], index=as_index([a]))[0]
    assert a["event_key"] != b["event_key"]


def test_cross_site_duplicate_maps_to_official_event():
    official = keyed([ev("Fire Safety Booth at West View Recreation Center", "2026-10-28", "https://www.westminsterco.gov/Calendar.aspx?EID=4411", org="City of Westminster",
                         source_id="westminster-calendar")])[0]
    news = keyed([ev("Fire safety booth at West View Recreation Center this week", "2026-10-28", "https://news.example.com/fire", org="City of Westminster",
                     source_id="gnews:x")], index=as_index([official]), official=False)[0]
    assert news["event_key"] == official["event_key"]


def test_cross_site_duplicate_in_same_run_and_dedupe_prefers_official():
    evs = keyed([ev("Winter Volleyball League", "2027-01-06", "https://news.example.com/v", org="Hyland Hills", source_id="gnews:x"),
                 ev("Winter Volleyball League", "2027-01-06", "https://www.hylandhills.gov/event/winter-volleyball/", org="Hyland Hills", source_id="hyland-hills")])
    assert evs[0]["event_key"] == evs[1]["event_key"]
    kept, n = I.dedupe(evs)
    assert n == 1 and kept[0]["source_id"] == "hyland-hills"


def test_same_name_different_org_not_merged():
    a = keyed([ev("Family Movie Night", "2027-07-10", "https://a.example.org/m", org="Westminster Library", source_id="gnews:a")], official=False)[0]
    b = keyed([ev("Family Movie Night", "2027-07-10", "https://b.example.org/m", org="Thornton Rec", source_id="gnews:b")], index=as_index([a]), official=False)[0]
    # 같은 날짜라 date_match 가 되지만 주최가 다르면… 제목 완전 일치 + 같은 날이면 같은 행사로 볼 위험 → 주소 기준이 다르고 기관이 달라야 분리
    assert a["event_key"] != b["event_key"]


def test_ical_uid_is_used_first():
    e = Entry("du-athletics-ics", "https://denverpioneers.com/cal.ics#x", "Hockey vs CU", extra={"uid": "abc123"})
    a = keyed([ev("Hockey vs CU", "2027-01-10", e.url, org="DU")], entry=e)[0]
    b = keyed([ev("Hockey vs. Colorado", "2027-01-11", e.url, org="DU")], entry=e, index=as_index([a]))[0]
    assert a["event_key"] == b["event_key"]
