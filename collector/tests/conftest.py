import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai  # noqa: E402


def U(i=1000, o=200):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def ev(**over):
    base = dict(title="Kids Clay Workshop", title_ko="어린이 점토 공방", org="Butterfly Pavilion", bot="kids", start_date="2027-07-10",
                end_date=None, time_text="10:00-12:00", event_tz="America/Denver", location="Butterfly Pavilion", city="Westminster",
                age_text="6-10", english=["초보 가능"], cost_text="$20", signup="fcfs", registration_opens=None, deadline=None,
                status_note=None, tags=["art"], annual=False, paid_work=False, needs_review=False, review_reason=None, url="https://b.org/e/1")
    base.update(over)
    return ai.ExtractedEvent(**base)


class FakeMessages:
    """client.messages / client.beta.messages 대역. parse() 호출 인자를 기록하고 준비된 응답을 돌려준다."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        out = self.replies.pop(0)
        if callable(out):
            out = out(kw)
        return SimpleNamespace(parsed_output=out, stop_reason="end_turn", usage=U()) if not isinstance(out, SimpleNamespace) else out

    def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


class FakeClient:
    def __init__(self, replies=(), beta_replies=()):
        self.messages = FakeMessages(replies)
        self.beta = SimpleNamespace(messages=FakeMessages(beta_replies))


class FakeAPI:
    def __init__(self, seen=(), month_usd=0.0):
        self.dry_run = False
        self.known = set(seen)
        self.spent = month_usd
        self.log = []

    def _r(self, name, *a):
        self.log.append((name, a))

    def sources_extra(self):
        return []

    def seen_check(self, items):
        self._r("seen_check", items)
        return {x["key"] for x in items if x["key"] not in self.known}, set()

    def seen_mark(self, items):
        self._r("seen_mark", items)

    def month_usd(self):
        return self.spent

    def candidates(self, c):
        self._r("candidates", c)

    def enqueue(self, events):
        self._r("enqueue", events)
        return {"outbox": len(events)}

    def dispatch(self):
        return {"sent": 0}

    def add_usage(self, by_model):
        self._r("usage", by_model)

    def run_log(self, body):
        self._r("run_log", body)
