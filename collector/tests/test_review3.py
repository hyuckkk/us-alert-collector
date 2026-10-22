"""3차 검증 지적 재현 테스트(수집기 쪽): 대기열 임대 소유권. 수정 전 코드에서 실패하고 수정 후 통과해야 한다."""
import json
from datetime import datetime, timedelta, timezone

import httpx

import run
from conftest import FakeAPI, FakeClient
from test_run import CLAY, reply_for, world  # noqa: F401  (fixture 재사용)
from worker_api import WorkerAPI

PAST = datetime.now(timezone.utc) - timedelta(hours=1)
LATER = datetime.now(timezone.utc) + timedelta(hours=1)


def _item(k):
    return {"key": k, "hash": "h1", "source_id": "s", "url": f"https://x/{k}", "title": k, "priority": 0, "body": {}}


def test_late_report_from_expired_lease_is_rejected_and_does_not_corrupt_new_owner():
    api = WorkerAPI(None, None, dry_run=True)
    api.queue_add([_item("q1")])
    a = api.queue_take(10)[0]
    a_lease = {"lease_id": a["lease_id"], "hash": a["hash"]}
    api._mem["q1"]["lease_until"] = PAST                       # A 의 임대 만료
    b = api.queue_take(10)[0]                                  # B 가 다시 가져감
    assert b["key"] == "q1" and b["lease_id"] != a_lease["lease_id"]
    rejected = api.queue_result([{"key": "q1", "status": "extracted", "result": [{"from": "A"}], **a_lease}])
    assert rejected == {"q1"}
    row = api._mem["q1"]
    assert row["status"] == "queued" and row["result"] is None and row["lease_id"] == b["lease_id"]
    assert api.queue_result([{"key": "q1", "status": "extracted", "result": [{"from": "B"}], "lease_id": b["lease_id"], "hash": b["hash"]}]) == set()
    assert api._mem["q1"]["result"] == [{"from": "B"}]


def test_report_without_lease_or_after_expiry_is_rejected():
    api = WorkerAPI(None, None, dry_run=True)
    api.queue_add([_item("q2")])
    api.queue_take(10)
    assert api.queue_result([{"key": "q2", "status": "retry", "lease_id": "", "hash": "h1"}]) == {"q2"}
    api.queue_take(0)
    api._mem["q2"]["lease_until"] = PAST
    assert api.queue_result([{"key": "q2", "status": "retry"}]) == {"q2"}
    assert api._mem["q2"]["attempts"] == 0


def test_worker_client_sends_lease_and_hash_and_returns_rejected():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if body["op"] == "take":
            return httpx.Response(200, json={"ok": True, "items": [{"key": "k1", "hash": "hh", "lease_id": "L1", "status": "queued"}] if len(seen) == 1 else []})
        if body["op"] == "result":
            return httpx.Response(200, json={"ok": True, "updated": 0, "rejected": ["k1"]})
        if body["op"] == "extend":
            return httpx.Response(200, json={"ok": True, "extended": [], "lost": ["k1"]})
        return httpx.Response(200, json={"ok": True})
    api = WorkerAPI("https://w.example", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    api.queue_take(5)
    assert api.queue_extend(["k1"]) == {"k1"}
    api._leases["k1"] = {"lease_id": "L1", "hash": "hh"}
    assert api.queue_result([{"key": "k1", "status": "done"}]) == {"k1"}
    sent = [b for b in seen if b["op"] == "result"][0]["items"][0]
    assert sent["lease_id"] == "L1" and sent["hash"] == "hh"


def _steal(api):
    """다른 실행(B)이 임대 만료 후 모든 항목을 가져간 상태로 만든다."""
    for r in api._mem.values():
        r["lease_id"], r["lease_until"] = "B", LATER


def test_lease_lost_before_ai_skips_ai_call_and_enqueue(world):  # noqa: F811
    api = FakeAPI()
    real_take = api.queue_take

    def take_then_lose(limit, lease_minutes=30):
        rows = real_take(limit, lease_minutes)
        _steal(api)
        return rows
    api.queue_take = take_then_lose
    c = FakeClient([reply_for([CLAY])])
    run.main([], client=c, api=api)
    assert c.messages.calls == [], "다른 실행이 가진 항목으로 AI 를 부름"
    assert api.enqueued == []
    assert all(r["lease_id"] == "B" and r["status"] == "queued" for r in api._mem.values())


def test_lease_lost_during_ai_drops_results_and_keeps_new_owner_state(world):  # noqa: F811
    api = FakeAPI()
    real_index = api.event_index

    def index_then_lose():
        _steal(api)  # AI 처리 도중 임대가 끝나 B 가 가져감
        return real_index()
    api.event_index = index_then_lose
    rep = run.main([], client=FakeClient([reply_for([CLAY])]), api=api)
    assert api.enqueued == [], "임대를 잃은 항목의 행사를 등록함"
    assert all(r["status"] == "queued" and r["result"] is None and r["lease_id"] == "B" for r in api._mem.values())
    assert rep["metrics"]["lease_rejected"] == 1
