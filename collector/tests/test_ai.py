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
    entries = [Entry("s", f"https://x.org/{i}", f"t{i}", "x" * 500) for i in range(4)]
    reply = lambda kw: ai.ExtractBatch(results=[ai.EntryResult(index=0, relevant=True, reason="", events=[ev()]),  # noqa: E731
                                                ai.EntryResult(index=1, relevant=False, reason="", events=[])])
    c = FakeClient([reply, reply])
    usage = ai.Usage(PRICES)
    out = ai.extract(c, CFG, entries, "2026-10-20", usage)
    assert sorted(out.results) == [0, 1, 2, 3] and out.failed == {}
    kw = c.messages.calls[0]
    assert kw["model"] == "claude-haiku-5-5" and kw["output_format"] is ai.ExtractBatch and kw["output_config"] == {"effort": "low"}
    assert "x" * 51 not in kw["messages"][0]["content"]  # 본문은 text_chars 로 자름
    assert usage.by_model["claude-haiku-5-5"]["calls"] == 2
    assert usage.usd == pytest.approx(2 * (1000 / 1e6 * 0.10 + 200 / 1e6 * 0.50))


def test_1_2_items_missing_from_ai_output_are_marked_failed_not_done():
    """1차 검증 1-2 재현: AI 가 12건 중 일부만 돌려주면 나머지는 '처리 완료'가 아니라 재시도 대상이어야 한다."""
    entries = [Entry("s", f"https://x.org/{i}", f"t{i}") for i in range(2)]
    c = FakeClient([lambda kw: ai.ExtractBatch(results=[ai.EntryResult(index=1, relevant=True, reason="", events=[ev()])])])
    out = ai.extract(c, CFG, entries, "2026-10-20", ai.Usage(PRICES))
    assert list(out.results) == [1] and out.failed == {0: "missing-in-output"}


def test_1_2_failed_batch_keeps_earlier_batches_and_rate_limit_stops_rest():
    import httpx
    entries = [Entry("s", f"https://x.org/{i}", f"t{i}") for i in range(6)]
    ok = lambda kw: ai.ExtractBatch(results=[ai.EntryResult(index=j, relevant=False, reason="", events=[]) for j in range(2)])  # noqa: E731
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def boom(kw):
        raise ai.anthropic.RateLimitError("rate", response=httpx.Response(429, request=req), body=None)
    usage = ai.Usage(PRICES)
    out = ai.extract(FakeClient([ok, boom, ok]), CFG, entries, "2026-10-20", usage)
    assert sorted(out.results) == [0, 1]
    assert out.failed == {2: "RateLimitError", 3: "RateLimitError", 4: "not-attempted: RateLimitError", 5: "not-attempted: RateLimitError"}
    assert out.stopped == "RateLimitError" and usage.by_model["claude-haiku-5-5"]["errors"] == 1


def test_extract_refusal_marks_batch_failed():
    c = FakeClient([SimpleNamespace(parsed_output=None, stop_reason="refusal", usage=U())])
    out = ai.extract(c, CFG, [Entry("s", "https://x.org", "t")], "2026-10-20", ai.Usage(PRICES))
    assert out.failed == {0: "stop_reason=refusal"}


def test_usage_counts_cache_and_server_search_requests():
    usage = ai.Usage({"claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.20}}, 0.01)
    u = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=2000, cache_read_input_tokens=10000,
                        server_tool_use=SimpleNamespace(web_search_requests=3))
    usage.add("claude-opus-5-5", u, searches=1)
    m = usage.by_model["claude-opus-5-5"]
    assert (m["cache_write_tokens"], m["cache_read_tokens"], m["searches"]) == (2000, 10000, 3)
    assert usage.usd == pytest.approx((1000 * 4 + 2000 * 5 + 10000 * 0.2 + 100 * 20) / 1e6 + 0.03)


def test_household_profile_comes_from_secret_not_repo(monkeypatch):
    monkeypatch.delenv("HOUSEHOLD_PROFILE", raising=False)
    assert "F-2" not in ai.extract_system()
    monkeypatch.setenv("HOUSEHOLD_PROFILE", "PRIVATE PROFILE TEXT")
    assert ai.extract_system().endswith("PRIVATE PROFILE TEXT")


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
