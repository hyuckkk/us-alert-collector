"""외부 검색 결과 연결: 검사 → 공식 페이지 재확인 → 추출·추천 → 같은 행사는 같은 키(다시 알리지 않음) → Worker 등록."""
import ai
import external_ingest as xi
import fetch
from conftest import FakeAPI, FakeClient, ev


def test_validate_rejects_non_https_and_missing_title_and_caps_count():
    raw = {"events": [{"title": "Jazz Night", "url": "https://example.org/jazz", "description": "x" * 5000, "secret": "drop"},
                      {"title": "Bad", "url": "http://example.org/a"}, {"url": "https://example.org/b"}, "oops"] + [{"title": f"t{i}", "url": f"https://e.org/{i}"} for i in range(40)]}
    ok, bad = xi.validate(raw)
    assert ok[0]["url"] == "https://example.org/jazz" and len(ok[0]["description"]) == 1500 and "secret" not in ok[0]
    assert len(ok) == 1 + 26 and any("https" in b for b in bad) and any("제목" in b for b in bad) and any("초과분" in b for b in bad)


def test_ingest_runs_full_pipeline_and_same_event_is_not_new(monkeypatch):
    monkeypatch.setattr(xi, "fetch_detail", lambda e, c, chars=4000: setattr(e, "text", "Botanic Gardens Blossoms of Light. Nov 27, 2026 5-9pm. $25.") or True)

    class NC:
        def __enter__(self): return None
        def __exit__(self, *a): return False
    monkeypatch.setattr(xi, "new_client", lambda: NC())
    reply = ai.ExtractBatch(results=[ai.EntryResult(index=0, relevant=True, reason="", events=[ev(
        title="Blossoms of Light", title_ko="블로섬 오브 라이트", bot="family", start_date="2026-11-27", url="https://www.botanicgardens.org/blossoms-light",
        org="Denver Botanic Gardens", location="Denver Botanic Gardens", city="Denver")])])
    api = FakeAPI()
    raw = {"events": [{"title": "Blossoms of Light", "url": "https://www.botanicgardens.org/blossoms-light", "audience": "family", "description": "holiday lights"}]}
    rep = xi.ingest(raw, api, FakeClient([reply]), {"extract": {"model": "claude-haiku-5-5", "effort": "low", "batch": 12, "max_tokens": 1000, "text_chars": 4500},
                                                       "prices": {}, "limits": {"credit_reference_usd": 100}}, "2026-10-22")
    assert rep["enqueued"] == 1 and api.enqueued[0]["source_id"] == "ext-search" and api.enqueued[0]["rec_level"] == "push"
    key = api.enqueued[0]["event_key"]
    # 같은 행사가 다시 들어오면 Worker 의 행사 목록과 대조해 같은 키(새 행사가 아님)
    api.index = [{**api.enqueued[0]}]
    rep2 = xi.ingest(raw, api, FakeClient([reply]), {"extract": {"model": "claude-haiku-5-5", "effort": "low", "batch": 12, "max_tokens": 1000, "text_chars": 4500},
                                                        "prices": {}, "limits": {"credit_reference_usd": 100}}, "2026-10-22")
    assert rep2["event_keys"] == [key]


def test_gap_check_classifies_and_adds_source_and_queries(monkeypatch):
    """외부가 찾은 행사를 우리 수집이 놓쳤으면 원인을 기록하고, 감시할 공식 페이지(실제로 조회되면)와 검색어를 자동으로 더한다."""
    from fetch import FetchResult, Entry as E2
    evs = [{"event_key": "eA", "url": "https://www.botanicgardens.org/blossoms-light", "title": "Blossoms of Light", "title_ko": "블로섬 오브 라이트", "bot": "family", "_entry_index": 0, "start_date": "2026-11-20"},
           {"event_key": "eB", "url": "https://newfest.org/winter", "title": "Winter Fest", "bot": "family", "_entry_index": 1},
           {"event_key": "eC", "url": "https://www.westminsterco.gov/Calendar.aspx?EID=1", "title": "Concert", "bot": "life", "_entry_index": 2},
           {"event_key": "eD", "url": "https://www.thorntonco.gov/x", "title": "Queued", "bot": "life", "_entry_index": 3}]
    items = [{"description": "lights"}, {"description": "fest"}, {}, {}]
    by_key = {"eC": {"event_key": "eC", "source_id": "westminster-calendar"}}
    api = FakeAPI()
    sent = {}
    monkeypatch.setattr(api, "queue_find", lambda urls: [{"url": "https://www.thorntonco.gov/x", "status": "queued"}])
    monkeypatch.setattr(api, "gaps", lambda g, s, q: sent.update(g=g, s=s, q=q) or {"ok": True})
    monkeypatch.setattr(api, "sources_extra", lambda: [])
    fixes = {0: ai.GapFix(index=0, cause_ko="특별 행사 페이지를 감시하지 않음", monitor_url="https://www.botanicgardens.org/events/special-events", monitor_type="html",
                          monitor_mode="list", monitor_name="Botanic Gardens special events", queries=["holiday lights Denver tickets"], query_bot="family"),
             1: ai.GapFix(index=1, cause_ko="정보원 없음", monitor_url="https://newfest.org/events", monitor_type="html", monitor_mode="list", monitor_name=None,
                          queries=["winter festival Colorado"], query_bot="any")}
    monkeypatch.setattr(ai, "gap_analysis", lambda client, cfg, todo, hosts, usage: fixes)
    monkeypatch.setattr(xi, "fetch_source", lambda src, client=None, **k: FetchResult(True, 200, entries=[E2("x", "https://a/b", "t")]) if "botanic" in src["url"] else FetchResult(False, 403, error="HTTP 403"))

    class NC:
        def __enter__(self): return None
        def __exit__(self, *a): return False
    monkeypatch.setattr(xi, "new_client", lambda: NC())
    out = xi.check_gaps(evs, items, by_key, api, None, {}, ai.Usage({}))
    found = {r["event_key"]: r["found"] for r in sent["g"]}
    assert found == {"eA": "registered-missed", "eB": "unregistered", "eC": "found", "eD": "queued"}
    assert [x["url"] for x in sent["s"]] == ["https://www.botanicgardens.org/events/special-events"]   # 조회되는 공식 페이지만 정보원으로
    assert {x["query"] for x in sent["q"]} == {"holiday lights Denver tickets", "winter festival Colorado"}
    assert out["by_found"] == {"found": 1, "queued": 1, "registered-missed": 1, "unregistered": 1}
