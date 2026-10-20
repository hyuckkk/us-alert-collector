from types import SimpleNamespace

import pytest

import ai
from conftest import FakeClient, U, ev
from fetch import Entry

CFG = {"extract": {"model": "claude-haiku-5-5", "effort": "low", "batch": 2, "max_tokens": 16000, "text_chars": 50},
       "judge": {"model": "claude-opus-5-5", "effort": "medium", "batch": 8, "max_tokens": 16000},
       "discover": {"model": "claude-opus-5-5", "effort": "medium", "max_searches": 3, "max_tokens": 16000}}
PRICES = {"claude-haiku-5-5": [0.10, 0.50], "claude-opus-5-5": [4.0, 20.0]}


def test_extract_batches_and_offsets_indexes():
    entries = [Entry("s", f"https://x.org/{i}", f"t{i}", "x" * 500) for i in range(3)]
    reply = lambda kw: ai.ExtractBatch(results=[ai.EntryResult(index=0, relevant=True, reason="", events=[ev()])])  # noqa: E731
    c = FakeClient([reply, reply])
    usage = ai.Usage(PRICES)
    out = ai.extract(c, CFG, entries, "2026-10-20", usage)
    assert [r.index for r in out] == [0, 2]
    kw = c.messages.calls[0]
    assert kw["model"] == "claude-haiku-5-5" and kw["output_format"] is ai.ExtractBatch and kw["output_config"] == {"effort": "low"}
    assert "x" * 51 not in kw["messages"][0]["content"]  # 본문은 text_chars 로 자름
    assert usage.by_model["claude-haiku-5-5"]["calls"] == 2
    assert usage.usd == pytest.approx(2 * (1000 / 1e6 * 0.10 + 200 / 1e6 * 0.50))


def test_extract_refusal_raises():
    c = FakeClient([SimpleNamespace(parsed_output=None, stop_reason="refusal", usage=U())])
    with pytest.raises(RuntimeError):
        ai.extract(c, CFG, [Entry("s", "https://x.org", "t")], "2026-10-20", ai.Usage(PRICES))


def test_judge_uses_big_model_with_server_fallback():
    v = ai.Verdict(index=0, keep=False, reason="유급", bot="kids", age_text=None, status_note=None, english=[])
    c = FakeClient(beta_replies=[lambda kw: ai.JudgeBatch(verdicts=[v])])
    out = ai.judge(c, CFG, [({"title": "x"}, "src")], ai.Usage(PRICES))
    kw = c.beta.messages.calls[0]
    assert out[0].keep is False
    assert kw["model"] == "claude-opus-5-5" and kw["betas"] == [ai.FALLBACK_BETA] and kw["fallbacks"] == "default"


def test_discover_keeps_only_urls_seen_in_search_results_and_resumes_pause_turn():
    block = lambda t, **k: SimpleNamespace(type=t, **k)  # noqa: E731
    r1 = SimpleNamespace(stop_reason="pause_turn", usage=U(), content=[block("web_search_tool_result", content=[SimpleNamespace(url="https://real.org/events")])])
    r2 = SimpleNamespace(stop_reason="end_turn", usage=U(), content=[block("text", text='Here: {"sources":[{"url":"https://real.org/events","name":"Real","reason":"r","bots":["kids"]},'
                                                                              '{"url":"https://fake.org","name":"F","reason":"r","bots":[]}],"events":[]}')])
    c = FakeClient(beta_replies=[r1, r2])
    usage = ai.Usage(PRICES, 0.01)
    d = ai.discover(c, CFG, "kids camps", [], usage)
    assert [s["url"] for s in d["sources"]] == ["https://real.org/events"] and d["dropped_urls"] == 1
    assert len(c.beta.messages.calls) == 2 and c.beta.messages.calls[0]["tools"][0]["type"] == "web_search_20260209"
    assert usage.by_model["claude-opus-5-5"]["searches"] == 1


def test_check_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ai.check_key()["status"] == "missing"
