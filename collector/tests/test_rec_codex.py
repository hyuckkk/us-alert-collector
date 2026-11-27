"""맥북 Codex 추천 판정 경로: 수집 실행은 추천 대기(await_rec)로 넘기고, 판정기가 판정·등록·완료한다. 오래 남으면 Claude 가 대신."""
import json

import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import ai
import run
from conftest import FakeAPI, FakeClient
from test_run import CLAY, reply_for, world  # noqa: F401  (pytest fixture 재사용)


def rec_json(n, level="push"):
    return json.dumps({"items": [{"index": i, "level": level, "bot": "kids", "category": "class", "score": 70, "title_ko": f"행사 {i}",
                                  "summary_ko": "요약", "highlights": [], "reason": "이유", "special_reason": "한정 체험" if level == "push" else "",
                                  "special_basis": "rare_one_off" if level == "push" else "none", "evidence_quote": "Kids Clay Workshop" if level == "push" else "", "usual_alternative": "",
                                  "blockers": []} for i in range(n)]})


def codex_on(monkeypatch, fallback=180):
    real = run.load

    def load(name):
        d = real(name)
        if name == "models.json":
            d["recommend_codex"] = {**d["recommend_codex"], "enabled": True, "fallback_minutes": fallback}
        return d
    monkeypatch.setattr(run, "load", load)


def handed(world, monkeypatch, tmp_path):
    codex_on(monkeypatch)
    called = []
    monkeypatch.setattr(ai, "recommend", lambda *a, **k: called.append(1))
    api = FakeAPI()
    rep = run.main(["--out", str(tmp_path / "r.json")], client=FakeClient([reply_for([CLAY])]), api=api)
    assert not called, "수집 실행에서는 추천 판정을 하지 않음"
    assert not api.enqueued, "판정 전에는 Worker 에 등록하지 않음(발송 안 됨)"
    rows = [r for r in api._mem.values() if r["status"] == "await_rec"]
    assert len(rows) == 1 and rows[0]["result"][0]["url"] == CLAY and rows[0]["result"][0].get("rec_level") is None
    assert rep["metrics"]["await_rec"] == 1
    assert rep["stages"]["rec_fallback"]["ok"] and rep["stages"]["rec_fallback"].get("rows", 0) == 0, "방금 넘긴 것은 대신 판정하지 않음"
    return api


def test_codex_mode_hands_events_to_await_rec_without_claude_recommend(world, monkeypatch, tmp_path):
    handed(world, monkeypatch, tmp_path)


def test_runner_processes_await_rec_enqueues_and_marks_done(world, monkeypatch, tmp_path):
    api = handed(world, monkeypatch, tmp_path)
    rows = api.queue_take_rec(60)
    seen = {}

    def fake_run(cmd, input, **k):
        seen["cmd"], seen["prompt"] = cmd, input
        open(cmd[cmd.index("-o") + 1], "w").write(rec_json(1))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    cfg = {"recommend_codex": {"model": "gpt-6.2-sol", "effort": "low", "batch": 20, "parallel": 1}}
    out = ai.process_await_rec(api, rows, lambda items: ai.recommend_codex(cfg, items, "2026-11-25", run=fake_run), found_by="mac_codex")
    assert out["events"] == 1 and out["levels"] == {"push": 1}
    assert api.enqueued[-1]["rec_level"] == "push" and api.enqueued[-1]["found_by"] == "mac_codex"
    assert [r["status"] for r in api._mem.values()] == ["done"]
    assert "-m" in seen["cmd"] and "gpt-6.2-sol" in seen["cmd"] and 'model_reasoning_effort="low"' in seen["cmd"]
    assert "=== SYSTEM ===" in seen["prompt"] and "Kids Clay" in seen["prompt"]


def test_codex_failure_keeps_rows_waiting(world, monkeypatch, tmp_path):
    api = handed(world, monkeypatch, tmp_path)
    rows = api.queue_take_rec(60)
    cfg = {"recommend_codex": {"model": "m", "effort": "low", "batch": 20, "parallel": 1}}
    bad = lambda cmd, input, **k: SimpleNamespace(returncode=1, stdout="", stderr="usage limit")  # noqa: E731
    out = ai.process_await_rec(api, rows, lambda items: ai.recommend_codex(cfg, items, "2026-11-25", run=bad))
    assert out["events"] == 0 and out["missing_rows"] == 1
    assert [r["status"] for r in api._mem.values()] == ["await_rec"], "실패하면 대기 상태 그대로(나중에 다시)"
    assert not [e for e in api.enqueued if e.get("url") == CLAY]


def test_fallback_uses_claude_for_old_await_rec(world, monkeypatch, tmp_path):
    api = handed(world, monkeypatch, tmp_path)
    for r in api._mem.values():
        r["updated_at"] = datetime.now(timezone.utc) - timedelta(hours=5)
    got = []

    def fake_rec(client, cfg, items, today, usage, batch_wait_min=None):
        got.append(len(items))
        return ai.RecommendOutcome(recs={i: ai.Recommendation(**json.loads(rec_json(1, "interest"))["items"][0]) for i in range(len(items))})
    monkeypatch.setattr(ai, "recommend", fake_rec)
    rep = run.main(["--out", str(tmp_path / "r2.json"), "--mode", "light"], client=FakeClient([]), api=api)
    assert got == [1]
    assert rep["stages"]["rec_fallback"]["events"] == 1
    assert api.enqueued[-1]["rec_level"] == "interest"
    assert [r["status"] for r in api._mem.values()] == ["done"]


@pytest.mark.real_recommend
def test_mac_runner_extracts_queued_with_codex_client(world, monkeypatch, tmp_path):
    """크레딧 소진 기간: 맥북 판정기가 추출 전 항목을 같은 수집 코드 + Codex 대역으로 처리(추출 → 추천 → 등록)."""
    import rec_runner
    from conftest import ev
    api = FakeAPI()
    # 수집 실행이 크레딧 소진으로 추출 못 하고 대기열에 둔 상태를 만든다(--no-ai)
    run.main(["--out", str(tmp_path / "r.json"), "--no-ai"], client=FakeClient([]), api=api)
    assert [r["status"] for r in api._mem.values()] == ["queued"]
    calls = []

    def fake_run(cmd, input, **k):
        out = cmd[cmd.index("-o") + 1]
        if "You turn raw web items" in input:
            calls.append("extract")
            body = {"results": [json.loads(ai.EntryResult(index=0, relevant=True, reason="", events=[ev(title="Kids Clay Workshop", url=CLAY)]).model_dump_json())]}
        else:
            calls.append("recommend")
            body = json.loads(rec_json(1))
        open(out, "w").write(json.dumps(body))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    cfg = json.loads((rec_runner.ROOT / "config/models.json").read_text())
    client = ai.CodexClient(cfg["recommend_codex"], run=fake_run)
    rep = rec_runner.extract_queued(cfg, api, client, 60)
    client.close()
    assert calls == ["extract", "recommend"]
    assert rep["metrics"]["ai_items"] == 1 and rep["metrics"]["events_out"] == 1
    assert api.enqueued[-1]["url"] == CLAY and api.enqueued[-1]["rec_level"] == "push"
    assert [r["status"] for r in api._mem.values()] == ["done"]


def test_codex_failure_in_extract_retries_item_not_crash(world, monkeypatch, tmp_path):
    import rec_runner
    api = FakeAPI()
    run.main(["--out", str(tmp_path / "r.json"), "--no-ai"], client=FakeClient([]), api=api)
    bad = lambda cmd, input, **k: SimpleNamespace(returncode=1, stdout="", stderr="rate limit")  # noqa: E731
    cfg = json.loads((rec_runner.ROOT / "config/models.json").read_text())
    rep = rec_runner.extract_queued(cfg, api, ai.CodexClient(cfg["recommend_codex"], run=bad), 60)
    assert rep["stages"]["ai"]["ok"] and rep["metrics"]["ai_failed"] == 1
    assert [r["status"] for r in api._mem.values()] == ["queued"]


def test_credit_exhausted_releases_without_using_retry(world, monkeypatch, tmp_path):
    from conftest import credit_error
    api = FakeAPI()
    c = FakeClient([credit_error])
    run.main(["--out", str(tmp_path / "r.json")], client=c, api=api)
    r = list(api._mem.values())[0]
    assert r["status"] == "queued" and r["attempts"] == 0, "크레딧 소진은 시도 횟수를 쓰지 않음"


class _PeekAPI:
    def __init__(self, rows):
        self.rows, self.sent = rows, []

    def ai_health(self):
        return {"failing_since": (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()}

    def queue_peek(self, limit):
        return self.rows

    def unverified(self, items, outage_hours, pending):
        self.sent.append(items)
        return {"sent": 1}


def _fallback(rows, mac=True):
    api = _PeekAPI(rows)
    me = SimpleNamespace(api=api, models={"recommend_codex": {"enabled": mac, "mac_extract": mac, "fallback_minutes": 180}})
    return run.Run.fallback(me), api


def test_no_unverified_notice_while_mac_codex_keeps_up():
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    rows = [{"source_id": "jeffco", "url": "https://x.org/e/1", "title": "[상세 변경] a", "first_seen": fresh}]
    out, api = _fallback(rows)
    assert out == {"needed": False, "reason": "mac_codex_keeping_up"} and not api.sent
    out, api = _fallback(rows, mac=False)
    assert out["needed"] and api.sent, "맥북 판정기를 안 쓰면 예전처럼 보냄"


def test_unverified_notice_resumes_when_queue_gets_old():
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    rows = [{"source_id": "s", "url": "https://x.org/1", "title": "old", "first_seen": old},
            {"source_id": "s", "url": "https://x.org/2", "title": "new", "first_seen": fresh}]
    out, api = _fallback(rows)
    assert out["needed"] and [i["title"] for i in api.sent[0]] == ["old"]
