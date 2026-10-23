"""실행 흐름 재현 테스트: 대기열 보존(3), AI 누락 재시도(1-2), 단계 격리(1-6), 상세 재검사(1-3), AI 중단 대응(4), 예산(8)."""
import pytest

import ai
import run
from conftest import credit_error, FakeAPI, FakeClient, ev
from fetch import Entry, FetchResult

CLAY = "https://butterflies.org/events/clay"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    listing = {"items": [Entry("butterfly-pavilion", CLAY, "Kids Clay Workshop")]}

    def fake_fetch(src, secrets=None, **k):
        if src["type"] == "gnews":
            return FetchResult(True, 200, entries=[])
        if src["id"] == "butterfly-pavilion":
            return FetchResult(True, 200, entries=[Entry(e.source_id, e.url, e.title) for e in listing["items"]])
        return FetchResult(False, 403, error="HTTP 403")
    monkeypatch.setattr(run, "fetch_source", fake_fetch)

    def fake_detail(entry, client, chars=3000):
        reg = " Registration opens Oct 25. Limited spots." if "camp" in entry.title.lower() else ""
        entry.text = f"{entry.title}. Saturday July 10, 2027 10am-noon. Ages 6-10. $20.{reg}"
        return True
    monkeypatch.setattr(run, "fetch_detail", fake_detail)
    monkeypatch.setattr(run, "is_due", lambda s, today: s["id"] in ("butterfly-pavilion", "anythink-events"))
    real = run.load

    def load(name):
        d = real(name)
        if name == "models.json":
            d["discover"]["enabled"] = False
        if name == "queries.json":
            d = {"daily": {"kids": ["q"]}, "weekday": {}}
        return d
    monkeypatch.setattr(run, "load", load)
    return listing


def reply_for(urls):
    """입력 항목마다 행사 1건씩 돌려주는 가짜 추출."""
    def f(kw):
        n = kw["messages"][0]["content"].count('"index"')
        return ai.ExtractBatch(results=[ai.EntryResult(index=i, relevant=True, reason="", events=[ev(title=f"Event {i}", url=urls[i] if i < len(urls) else CLAY)])
                                        for i in range(n)])
    return f


def test_full_run_queues_extracts_enqueues_and_marks_done(world, tmp_path):
    api = FakeAPI()
    rep = run.main(["--out", str(tmp_path / "r.json")], client=FakeClient([reply_for([CLAY])]), api=api)
    assert rep["stages"]["sources"]["ok"] and rep["stages"]["ai"]["ok"] and rep["stages"]["enqueue"]["ok"]
    sent = api.enqueued[0]
    assert sent["url"] == CLAY and sent["bot"] == "kids" and sent["event_key"].startswith("e")
    assert all(r["status"] == "done" for r in api._mem.values())
    m = rep["metrics"]
    assert m["entries_new"] == 1 and m["events_out"] == 1 and m["usd"] > 0
    log = dict(api.log)["run_log"][0]
    assert log["completed"] is True and log["ai"] == {"ok": True}
    assert {s["id"]: s for s in log["sources"]}["butterfly-pavilion"]["events"] == 1
    assert "chat_id" not in (tmp_path / "r.json").read_text()


def test_second_run_does_not_reprocess_same_item(world):
    api = FakeAPI()
    run.main([], client=FakeClient([reply_for([CLAY])]), api=api)
    c = FakeClient([])
    rep = run.main([], client=c, api=api)
    assert c.messages.calls == [] and rep["metrics"]["entries_new"] == 0


def test_3_overflow_is_kept_in_queue_and_processed_next_run_with_registration_items_first(world):
    world["items"] = [Entry("butterfly-pavilion", f"https://butterflies.org/events/e{i}", f"Exhibit tour number {i}") for i in range(5)]
    world["items"].append(Entry("butterfly-pavilion", "https://butterflies.org/events/camp", "Summer camp registration opens"))
    api = FakeAPI()
    c = FakeClient([reply_for([f"https://butterflies.org/events/x{i}" for i in range(12)])] * 3)
    run.main(["--max", "2"], client=c, api=api)
    first = c.messages.calls[0]["messages"][0]["content"]
    assert "Summer camp registration opens" in first, "접수 관련 항목이 먼저 처리돼야 함"
    statuses = [r["status"] for r in api._mem.values()]
    assert statuses.count("done") == 2 and statuses.count("queued") == 4, "상한을 넘은 항목은 버리지 않고 대기"
    run.main(["--max", "10"], client=c, api=api)
    assert all(r["status"] == "done" for r in api._mem.values())


def test_1_2_missing_items_are_retried_next_run_not_lost(world):
    world["items"] = [Entry("butterfly-pavilion", f"https://butterflies.org/events/e{i}", f"Exhibit tour number {i}") for i in range(3)]
    api = FakeAPI()

    def partial(kw):  # 3건 중 1건만 돌려줌
        return ai.ExtractBatch(results=[ai.EntryResult(index=0, relevant=False, reason="", events=[])])
    run.main([], client=FakeClient([partial]), api=api)
    st = sorted(r["status"] for r in api._mem.values())
    assert st == ["done", "queued", "queued"]
    assert all(r["attempts"] == 1 for r in api._mem.values() if r["status"] == "queued")
    c = FakeClient([reply_for([CLAY, CLAY])])
    run.main([], client=c, api=api)
    assert c.messages.calls[0]["messages"][0]["content"].count('"index"') == 2
    assert all(r["status"] == "done" for r in api._mem.values())


def test_1_6_discover_failure_does_not_block_extraction(world, monkeypatch):
    real = run.load
    monkeypatch.setattr(run, "load", lambda n: {**real(n), "discover": {**real(n)["discover"], "enabled": True}} if n == "models.json" else real(n))

    def boom(*a, **k):
        raise RuntimeError("search exploded")
    monkeypatch.setattr(ai, "discover", boom)
    api = FakeAPI()
    rep = run.main([], client=FakeClient([reply_for([CLAY])]), api=api)
    assert rep["stages"]["discover"]["ok"] and rep["stages"]["discover"]["errors"] == 4
    assert len(api.enqueued) == 1, "발굴 실패와 무관하게 수집한 행사는 처리"


def test_1_6_worker_enqueue_failure_keeps_ai_result_and_does_not_call_ai_again(world):
    api = FakeAPI()
    calls = {"n": 0}
    real_enqueue = api.enqueue

    def flaky(events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker down")
        return real_enqueue(events)
    api.enqueue = flaky
    c = FakeClient([reply_for([CLAY])])
    rep = run.main([], client=c, api=api)
    assert not rep["stages"]["enqueue"]["ok"]
    assert [r["status"] for r in api._mem.values()] == ["extracted"]
    rep2 = run.main([], client=c, api=api)
    assert len(c.messages.calls) == 1, "AI 를 다시 부르지 않음"
    assert rep2["stages"]["enqueue"]["ok"] and len(api.enqueued) == 1
    assert [r["status"] for r in api._mem.values()] == ["done"]


def test_1_3_detail_change_reextracts_and_keeps_event_key(world, monkeypatch):
    api = FakeAPI()
    run.main([], client=FakeClient([reply_for([CLAY])]), api=api)
    key = api.enqueued[0]["event_key"]
    api.due_watch = [{"url": CLAY, "event_key": key, "source_id": "butterfly-pavilion", "fingerprint": "old"}]
    monkeypatch.setattr(run, "page_text", lambda hc, url: (200, "Kids Clay Workshop. Saturday July 17, 2027. $25. Sold out."))

    def changed(kw):
        assert "[상세 변경]" in kw["messages"][0]["content"]
        return ai.ExtractBatch(results=[ai.EntryResult(index=0, relevant=True, reason="", events=[ev(title="Kids Clay Workshop (new date)", start_date="2027-07-17",
                                                                                                  cost_text="$25", url=CLAY)])])
    rep = run.main(["--mode", "light"], client=FakeClient([changed]), api=api)
    assert rep["metrics"]["watch_changed"] == 1
    assert api.enqueued[-1]["event_key"] == key and api.enqueued[-1]["cost_text"] == "$25"
    # 같은 지문이면 AI 를 부르지 않는다
    fp = [u for n, a in api.log if n == "watch_update" for u in a[0]][-1]["fingerprint"]
    api.due_watch = [{"url": CLAY, "event_key": key, "source_id": "butterfly-pavilion", "fingerprint": fp}]
    c = FakeClient([])
    run.main(["--mode", "light"], client=c, api=api)
    assert c.messages.calls == []


def test_8_month_spend_no_longer_stops_ai(world):
    """이슈 #20: 내부 월 상한(예: $150)으로 AI 를 멈추지 않는다. 사용액이 커도 정상 처리."""
    api = FakeAPI(month_usd=999)
    c = FakeClient([reply_for([CLAY])])
    rep = run.main([], client=c, api=api)
    assert c.messages.calls, "사용액만으로는 AI 를 멈추지 않음"
    assert not any("예산" in a for a in rep["alerts"])
    assert rep["workload"]["path"] == "github_collector" and rep["workload"]["cost_usd"]["extract"] > 0


def test_8_credit_exhausted_isolates_ai_keeps_items_and_alerts(world):
    """크레딧이 실제로 바닥나면 AI 단계만 멈추고, 항목은 대기열에 남기고, 소유자에게 알리고, 검증 전 공식 정보 경로를 쓴다."""
    api = FakeAPI()
    c = FakeClient([credit_error])
    rep = run.main([], client=c, api=api)
    assert len(c.messages.calls) == 1, "첫 거절 뒤 더 부르지 않음"
    assert ai.CREDIT["out"] and rep["workload"]["credit_exhausted"] is True
    assert any("크레딧 소진" in a and "카드" in a for a in rep["alerts"])
    assert [r["status"] for r in api._mem.values()] == ["queued"], "삭제하지 않고 보존"
    assert rep["stages"]["dispatch"]["ok"], "발송 단계는 계속"
    sent = [a for n, a in api.log if n == "unverified"]
    assert sent and sent[0][0][0]["url"] == CLAY


def test_4_long_ai_outage_sends_unverified_to_owner(world):
    api = FakeAPI(ai_health={"failing_since": "2026-01-01T00:00:00Z"})
    run.main(["--no-ai"], client=FakeClient([]), api=api)
    sent = [a for n, a in api.log if n == "unverified"]
    assert sent and sent[0][1] > 36


def test_light_mode_skips_source_collection(world):
    api = FakeAPI()
    rep = run.main(["--mode", "light"], client=FakeClient([]), api=api)
    assert "sources" not in rep["stages"] and rep["stages"]["watch"]["ok"]
