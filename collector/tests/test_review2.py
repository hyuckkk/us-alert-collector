"""2차 검증 지적 재현 테스트(수집기 쪽). 수정 전 코드에서 실패하고 수정 후 통과해야 한다."""
import json
from datetime import date

import httpx
import pytest

import ai
import rules
import run
from conftest import FakeAPI, FakeClient, ev
from fetch import Entry, FetchResult
from worker_api import WorkerAPI


# ---------------------------------------------------------------- A. 대기열 300건 가져오기
def _mock_worker(lease: bool, n: int = 350):
    """Worker /admin/queue take 흉내. lease=False 는 수정 전 서버(항상 앞의 60건), True 는 임대 방식."""
    items = [{"key": f"k{i:03d}", "hash": "h", "source_id": "s", "url": f"https://x.org/{i}", "title": "t", "body": {}, "hint": None,
              "priority": 0, "status": "queued", "attempts": 0, "result": None, "first_seen": "2026-10-20"} for i in range(n)]
    leased = set()

    def handler(req):
        body = json.loads(req.content)
        if body.get("op") == "take":
            pool = [x for x in items if not (lease and x["key"] in leased)][: body["limit"]]
            if lease:
                leased.update(x["key"] for x in pool)
            return httpx.Response(200, json={"ok": True, "items": pool})
        return httpx.Response(200, json={"ok": True})
    return WorkerAPI("https://w.example", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_A_take_returns_300_distinct_items_from_leasing_worker():
    got = _mock_worker(lease=True).queue_take(300)
    assert len(got) == 300 and len({g["key"] for g in got}) == 300


def test_A_client_does_not_silently_stop_at_60_when_server_repeats():
    # 수정 전 서버처럼 같은 60건을 되풀이하면 클라이언트는 조용히 60건으로 끝내지 말고 오류로 알려야 한다
    with pytest.raises(RuntimeError, match="lease"):
        _mock_worker(lease=False).queue_take(300)


def test_A_dry_run_queue_also_leases_so_two_takes_do_not_overlap():
    api = WorkerAPI(None, None, dry_run=True)
    api.queue_add([{"key": f"k{i}", "hash": "h", "source_id": "s", "url": f"https://x/{i}", "title": "t", "priority": 0, "body": {}} for i in range(350)])
    a, b = api.queue_take(300), api.queue_take(300)
    assert len(a) == 300 and len(b) == 50 and not ({x["key"] for x in a} & {x["key"] for x in b})


# ---------------------------------------------------------------- 공통 실행 환경
@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    state = {"items": [], "detail_fail": set(), "detail_calls": [], "sources_fail": False}

    def fake_fetch(src, secrets=None, **k):
        if state["sources_fail"]:
            return FetchResult(False, None, error="ConnectTimeout")
        if src["type"] == "gnews":
            return FetchResult(True, 200, entries=[])
        if src["id"] == "butterfly-pavilion":
            return FetchResult(True, 200, entries=[Entry(e.source_id, e.url, e.title) for e in state["items"]])
        return FetchResult(False, 403, error="HTTP 403")
    monkeypatch.setattr(run, "fetch_source", fake_fetch)

    def fake_detail(entry, client, chars=3000):
        state["detail_calls"].append(entry.url)
        if entry.url in state["detail_fail"]:
            entry.extra["detail_error"] = "HTTP 503"
            return False
        entry.text = f"DETAIL {entry.title}. July 10, 2027. $20."
        return True
    monkeypatch.setattr(run, "fetch_detail", fake_detail)
    monkeypatch.setattr(run, "is_due", lambda s, today: s["id"] in ("butterfly-pavilion", "anythink-communico"))
    real = run.load

    def load(name):
        d = real(name)
        if name == "models.json":
            d["discover"]["enabled"] = False
        if name == "queries.json":
            d = {"daily": {"kids": ["q"]}, "weekday": {}}
        return d
    monkeypatch.setattr(run, "load", load)
    return state


def echo_reply(kw):
    content = kw["messages"][0]["content"]
    n = content.count('"index"')
    return ai.ExtractBatch(results=[ai.EntryResult(index=i, relevant=False, reason="", events=[]) for i in range(n)])


# ---------------------------------------------------------------- B. 상세 본문 없는 항목
def test_B_items_beyond_detail_cap_get_detail_before_ai_in_later_light_run(world, monkeypatch):
    monkeypatch.setattr(run, "MAX_DETAIL_FETCH", 2)
    world["items"] = [Entry("butterfly-pavilion", f"https://butterflies.org/events/e{i}", f"Exhibit tour number {i}") for i in range(4)]
    api = FakeAPI()
    run.main(["--no-ai"], client=FakeClient([]), api=api)            # full: 상세 2건만, AI 없음
    assert len(world["detail_calls"]) == 2
    c = FakeClient([echo_reply])
    run.main(["--mode", "light"], client=c, api=api)                  # light: 나머지 2건 상세 조회 후 AI
    prompt = c.messages.calls[0]["messages"][0]["content"]
    assert prompt.count("DETAIL Exhibit tour number") == 4, "본문 없이 AI 로 간 항목이 있음"
    assert len(world["detail_calls"]) == 4


def test_B_detail_failure_is_not_sent_to_ai_and_stays_queued(world):
    world["items"] = [Entry("butterfly-pavilion", "https://butterflies.org/events/ok", "Exhibit tour ok"),
                      Entry("butterfly-pavilion", "https://butterflies.org/events/bad", "Exhibit tour bad")]
    world["detail_fail"] = {"https://butterflies.org/events/bad"}
    api = FakeAPI()
    c = FakeClient([echo_reply])
    run.main([], client=c, api=api)
    prompt = c.messages.calls[0]["messages"][0]["content"]
    assert "Exhibit tour bad" not in prompt, "상세 본문 없는 항목을 AI 가 추측하게 함"
    bad = next(r for r in api._mem.values() if r["url"].endswith("/bad"))
    assert bad["status"] == "queued" and bad["attempts"] == 1


# ---------------------------------------------------------------- D. 정밀판정 결과 누락
@pytest.mark.parametrize("verdicts", ["none", "partial", "duplicate"])
def test_D_missing_judge_verdict_holds_event_unsent(world, verdicts):
    world["items"] = [Entry("butterfly-pavilion", f"https://butterflies.org/events/r{i}", f"Exhibit tour r{i}") for i in range(2)]

    def extract(kw):
        return ai.ExtractBatch(results=[ai.EntryResult(index=i, relevant=True, reason="", events=[ev(title=f"Review me {i}", needs_review=True, review_reason="연령 불명",
                                                                                               url=f"https://butterflies.org/events/r{i}")]) for i in range(2)])
    v = lambda i: ai.Verdict(index=i, keep=True, reason="ok", bot="kids", age_text="6+", status_note=None, english=[])  # noqa: E731
    judged = {"none": [], "partial": [v(0)], "duplicate": [v(0), v(0)]}[verdicts]
    api = FakeAPI()
    run.main([], client=FakeClient([extract], beta_replies=[lambda kw: ai.JudgeBatch(verdicts=judged)]), api=api)
    sent_titles = {e["title"] for e in api.enqueued}
    assert "Review me 1" not in sent_titles, "판정 없이 발송 대기열로 감"
    held = next(r for r in api._mem.values() if r["url"].endswith("/r1"))
    assert held["status"] == "queued" and held["attempts"] == 1


# ---------------------------------------------------------------- E. 지문 갱신 순서
def test_E_fingerprint_not_updated_when_change_could_not_be_queued(world, monkeypatch):
    api = FakeAPI()
    api.due_watch = [{"url": "https://butterflies.org/events/x", "event_keys": ["eX"], "source_id": "butterfly-pavilion", "fingerprint": "old"}]
    monkeypatch.setattr(run, "page_text", lambda hc, url: (200, "Kids Clay. July 17, 2027. $25."))

    def broken_add(items):
        raise RuntimeError("D1 down")
    api.queue_add = broken_add
    run.main(["--mode", "light", "--no-ai"], client=FakeClient([]), api=api)
    updates = [u for n, a in api.log if n == "watch_update" for u in a[0]]
    assert not any(u.get("fingerprint") and u["url"].endswith("/x") for u in updates), "대기열 보존 실패인데 새 지문을 기록함"


# ---------------------------------------------------------------- F. 한 페이지의 여러 행사
def test_F_page_with_two_events_change_maps_back_to_both_keys(world, monkeypatch):
    url = "https://butterflies.org/events/pottery"
    index = [{"event_key": "eA", "title": "Pottery Session 1", "org": "Butterfly Pavilion", "url": url, "start_date": "2027-07-10"},
             {"event_key": "eB", "title": "Pottery Session 2", "org": "Butterfly Pavilion", "url": url, "start_date": "2027-07-17"}]
    api = FakeAPI(index=index)
    api.due_watch = [{"url": url, "event_keys": ["eA", "eB"], "source_id": "butterfly-pavilion", "fingerprint": "old"}]
    monkeypatch.setattr(run, "page_text", lambda hc, u: (200, "Pottery Session 1 July 10, 2027 $20. Pottery Session 2 July 17, 2027 $30."))

    def extract(kw):
        return ai.ExtractBatch(results=[ai.EntryResult(index=0, relevant=True, reason="", events=[
            ev(title="Pottery Session 1", start_date="2027-07-10", url=url), ev(title="Pottery Session 2", start_date="2027-07-17", cost_text="$30", url=url)])])
    run.main(["--mode", "light"], client=FakeClient([extract]), api=api)
    assert sorted(e["event_key"] for e in api.enqueued) == ["eA", "eB"]


# ---------------------------------------------------------------- G. 정상 수집 판정
def test_G_all_sources_failing_is_not_a_completed_collection(world):
    world["sources_fail"] = True
    api = FakeAPI()
    run.main(["--no-ai"], client=FakeClient([]), api=api)
    log = dict(api.log)["run_log"][0]
    assert log["completed"] is False
    assert log["metrics"]["sources_ok"] == 0


# ---------------------------------------------------------------- H. 연도 없는 날짜
def test_H_assumed_year_on_old_post_is_dropped_and_recent_is_marked_uncertain():
    today = date(2026, 10, 20)
    old = Entry("gnews:x", "https://n.example/old", "Fall festival Oct 30", published="Mon, 02 Oct 2023 10:00:00 GMT")
    new = Entry("butterfly-pavilion", "https://b.org/new", "Fall festival Oct 30", published="Mon, 12 Oct 2026 10:00:00 GMT")
    res = [ai.EntryResult(index=0, relevant=True, reason="", events=[ev(title="Fall Festival", start_date="2026-10-30", year_basis="assumed", url=old.url)]),
           ai.EntryResult(index=1, relevant=True, reason="", events=[ev(title="Fall Festival", start_date="2026-10-30", year_basis="assumed", url=new.url,
                                                                       deadline="2026-10-25")])]
    out, dropped = rules.finalize(res, [old, new], today)
    assert dropped.get("old-post") == 1
    assert len(out) == 1 and out[0]["date_uncertain"] is True and out[0]["urgency"] == "weekly"
    assert "연도" in (out[0]["status_note"] or "")


def test_H_stated_year_is_kept_as_is():
    today = date(2026, 10, 20)
    e = Entry("butterfly-pavilion", "https://b.org/x", "Camp", published="Mon, 02 Oct 2023 10:00:00 GMT")
    out, _ = rules.finalize([ai.EntryResult(index=0, relevant=True, reason="", events=[ev(start_date="2027-07-10", year_basis="stated", url=e.url)])], [e], today)
    assert len(out) == 1 and not out[0].get("date_uncertain")
