import json

import pytest

import ai
import run
from conftest import FakeAPI, FakeClient, ev
from fetch import Entry, FetchResult


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    def fake_fetch(src, secrets=None, **k):
        if src["type"] == "gnews":
            return FetchResult(True, 200, entries=[])
        if src["id"] == "butterfly-pavilion":
            return FetchResult(True, 200, entries=[Entry(src["id"], "https://butterflies.org/events/clay", "Kids Clay Workshop")])
        return FetchResult(False, 403, error="HTTP 403")
    monkeypatch.setattr(run, "fetch_source", fake_fetch)
    monkeypatch.setattr(run, "is_due", lambda s, today: True)
    real = run.load
    def load(name):
        d = real(name)
        if name == "models.json":
            d["discover"]["enabled"] = False
        return d
    monkeypatch.setattr(run, "load", load)


def extract_reply(kw):
    return ai.ExtractBatch(results=[ai.EntryResult(index=0, relevant=True, reason="", events=[ev(url="https://butterflies.org/events/clay")])])


def test_full_run_enqueues_marks_seen_and_logs(world, tmp_path):
    api = FakeAPI()
    rep = run.main(["--out", str(tmp_path / "r.json")], client=FakeClient([extract_reply]), api=api)
    names = [n for n, _ in api.log]
    assert names[:1] == ["seen_check"] and "enqueue" in names and "seen_mark" in names and names[-1] == "run_log"
    sent = dict(api.log)["enqueue"][0]
    assert sent[0]["url"] == "https://butterflies.org/events/clay" and sent[0]["bot"] == "kids"
    m = rep["metrics"]
    assert m["entries_new"] == 1 and m["events_out"] >= 1 and m["sources_ok"] == 1 and m["usd"] > 0
    assert any("성공률" in a for a in rep["alerts"])  # 1/19 성공 → 경고
    saved = json.loads((tmp_path / "r.json").read_text())
    assert "chat_id" not in json.dumps(saved)


def test_already_seen_items_do_not_reach_ai(world):
    import rules
    key = rules.entry_key(Entry("butterfly-pavilion", "https://butterflies.org/events/clay", "Kids Clay Workshop"))
    c = FakeClient([])
    rep = run.main([], client=c, api=FakeAPI(seen=[key]))
    assert c.messages.calls == [] and rep["metrics"]["entries_new"] == 0


def test_budget_exceeded_skips_ai_and_does_not_mark_seen(world):
    api = FakeAPI(month_usd=999)
    c = FakeClient([])
    rep = run.main([], client=c, api=api)
    assert c.messages.calls == []
    assert any("예산" in a for a in rep["alerts"])
    assert "seen_mark" not in [n for n, _ in api.log]
