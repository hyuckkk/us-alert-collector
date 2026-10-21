"""실행당 AI 비용 상한(--usd-cap): 상한을 넘을 수 있는 호출은 아예 하지 않는다."""
from types import SimpleNamespace

import ai
import run
from conftest import FakeAPI, FakeClient, U
from fetch import Entry
from test_run import CLAY, reply_for, world  # noqa: F401

PRICES = {"claude-haiku-5-5": {"input": 0.1, "output": 0.5}, "claude-opus-5-5": {"input": 4.0, "output": 20.0}}
CFG = {"extract": {"model": "claude-haiku-5-5", "effort": "low", "batch": 1, "max_tokens": 100, "text_chars": 100, "reserve_usd": 0.02},
       "judge": {"model": "claude-opus-5-5", "effort": "low", "batch": 1, "max_tokens": 100, "reserve_usd": 0.4},
       "discover": {"model": "claude-opus-5-5", "effort": "low", "max_searches": 1, "max_tokens": 100, "reserve_usd": 1.2}}


def test_extract_stops_before_call_that_could_exceed_cap():
    # 호출당 $0.01(입력 5만·출력 1만 토큰). 상한 $0.03, 예약 $0.02 → 2번 호출 뒤 세 번째는 하지 않음
    big = lambda kw: SimpleNamespace(parsed_output=reply_for([CLAY])(kw), stop_reason="end_turn", usage=U(50000, 10000))  # noqa: E731
    u = ai.Usage(PRICES, cap=0.03)
    c = FakeClient([big] * 5)
    out = ai.extract(c, CFG, [Entry("s", f"https://x/{i}", f"t{i}") for i in range(5)], "2026-10-21", u)
    assert len(c.messages.calls) == 2
    assert out.stopped == "run-cost-cap" and len(out.failed) == 3
    assert u.usd <= 0.03


def test_judge_and_discover_skip_when_reserve_exceeds_cap():
    u = ai.Usage(PRICES, cap=0.3)
    c = FakeClient()
    jo = ai.judge(c, CFG, [({"title": "x"}, "text")], u)
    assert c.beta.messages.calls == [] and jo.missing == {0: "run-cost-cap"}
    d = ai.discover(c, CFG, "theme", [], u)
    assert c.beta.messages.calls == [] and d["events"] == []


def test_run_passes_cap_to_usage(world, tmp_path):  # noqa: F811
    c = FakeClient([reply_for([CLAY])])
    rep = run.main(["--usd-cap", "0.0001"], client=c, api=FakeAPI())
    assert c.messages.calls == []                          # 상한이 예약분보다 작으면 추출 호출 없음
    assert rep["metrics"]["usd"] == 0
