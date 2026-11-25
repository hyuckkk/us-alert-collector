"""판정·추천 배치(Message Batches, 반값) 경로: 다 끝나면 배치 결과만, 시간 안에 안 끝나면 취소 후 즉시 호출, 거절은 대체 모델로 다시."""
from types import SimpleNamespace

import ai
from conftest import FakeClient, U

CFG = {"judge": {"model": "claude-opus-5-5", "effort": "medium", "batch": 2, "max_tokens": 16000},
       "recommend": {"model": "claude-opus-5-5", "effort": "high", "batch": 2, "max_tokens": 16000}}
PRICES = {"claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.2}}


def verdicts_json(n):
    import json
    return json.dumps({"verdicts": [{"index": i, "keep": True, "reason": "ok", "bot": "family", "age_text": None,
                                     "status_note": None, "english": []} for i in range(n)]})


def msg(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, usage=U(1000, 1000))


class FakeBatches:
    def __init__(self, results, polls_until_end=1, never_end=False):
        self.results_ = results
        self.left = polls_until_end
        self.never_end = never_end
        self.created = None
        self.canceled = False

    def _b(self, status):
        return SimpleNamespace(id="b1", processing_status=status)

    def create(self, requests):
        self.created = requests
        return self._b("in_progress")

    def retrieve(self, _id):
        if self.canceled:
            return self._b("ended")
        self.left -= 1
        return self._b("ended" if self.left <= 0 and not self.never_end else "in_progress")

    def cancel(self, _id):
        self.canceled = True

    def results(self, _id):
        return self.results_


def ok(cid, text, stop="end_turn"):
    return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="succeeded", message=msg(text, stop)))


def sync_reply(n):
    return SimpleNamespace(stop_reason="end_turn", usage=U(1000, 1000), parsed_output=ai.JudgeBatch.model_validate_json(verdicts_json(n)))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run_judge(client, items, wait=15):
    clock = Clock()
    usage = ai.Usage(PRICES)
    c = CFG["judge"]
    bodies = [f"chunk{i}" for i in range(len(items))]
    res = ai.structured_calls(client, c, "sys", bodies, ai.JudgeBatch, usage, wait, sleep=clock.sleep, clock=clock)
    return res, usage


def test_batch_all_done_half_price_and_schema():
    c = FakeClient()
    c.messages.batches = FakeBatches([ok("r0", verdicts_json(2)), ok("r1", verdicts_json(1))])
    res, usage = run_judge(c, [1, 2])
    assert all(e is None and r.parsed_output is not None for r, e in res)
    assert c.beta.messages.calls == []          # 즉시 호출 없음
    p = c.messages.batches.created[0]["params"]
    assert p["output_config"]["format"]["type"] == "json_schema" and "fallbacks" not in p
    full = (1000 * 4 + 1000 * 20) / 1e6
    assert abs(usage.usd - 2 * full * 0.5) < 1e-9


def test_batch_timeout_cancels_and_falls_back_to_sync():
    c = FakeClient(beta_replies=[sync_reply(1)])
    c.messages.batches = FakeBatches([ok("r0", verdicts_json(2))], never_end=True)
    res, _ = run_judge(c, [1, 2], wait=1)
    assert c.messages.batches.canceled
    assert len(c.beta.messages.calls) == 1      # 끝나지 않은 r1 만 즉시 호출
    assert all(e is None for _, e in res)


def test_batch_refusal_retried_with_fallback():
    c = FakeClient(beta_replies=[sync_reply(1)])
    c.messages.batches = FakeBatches([ok("r0", "", stop="refusal")])
    res, _ = run_judge(c, [1])
    assert c.beta.messages.calls[0]["fallbacks"] == "default"
    assert res[0][1] is None and res[0][0].parsed_output is not None


def test_judge_with_batch_maps_indexes(monkeypatch):
    monkeypatch.setattr(ai, "BATCH_POLL_SEC", 0)
    c = FakeClient()
    c.messages.batches = FakeBatches([ok("r0", verdicts_json(2)), ok("r1", verdicts_json(1))])
    out = ai.judge(c, CFG, [({"t": i}, "") for i in range(3)], ai.Usage(PRICES), batch_wait_min=15)
    assert sorted(out.verdicts) == [0, 1, 2] and not out.missing
