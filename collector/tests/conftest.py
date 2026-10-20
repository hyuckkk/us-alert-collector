import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai  # noqa: E402


def U(i=1000, o=200):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def ev(**over):
    base = dict(title="Kids Clay Workshop", title_ko="어린이 점토 공방", org="Butterfly Pavilion", bot="kids", start_date="2027-07-10",
                end_date=None, year_basis="stated", time_text="10:00-12:00", event_tz="America/Denver", location="Butterfly Pavilion", city="Westminster",
                age_text="6-10", english=["초보 가능"], cost_text="$20", signup="fcfs", registration_opens=None, registration_status="open",
                deadline=None, eligibility=None, status_note=None, tags=["art"], annual=False, paid_work=False, needs_review=False, review_reason=None, url="https://b.org/e/1")
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


from worker_api import WorkerAPI  # noqa: E402


class FakeAPI(WorkerAPI):
    """dry-run 메모리 대기열을 그대로 쓰고, Worker 로 가는 호출을 기록한다."""

    def __init__(self, month_usd=0.0, index=(), ai_health=None):
        super().__init__(None, None, dry_run=True)
        self.dry_run = False  # 실행 흐름은 실제 모드처럼(enqueue 결과 등) 흉내
        self.spent = month_usd
        self.index = list(index)
        self.health = ai_health or {}
        self.log = []
        self.enqueued = []

    def _r(self, name, *a):
        self.log.append((name, a))

    # 대기열은 부모의 메모리 구현 사용
    def queue_check(self, items):
        self.dry_run = True
        try:
            return super().queue_check(items)
        finally:
            self.dry_run = False

    def queue_add(self, items):
        self.dry_run = True
        try:
            self._r("queue_add", items)
            return super().queue_add(items)
        finally:
            self.dry_run = False

    def queue_take(self, limit, lease_minutes=30):
        self.dry_run = True
        try:
            return [dict(r) for r in super().queue_take(limit, lease_minutes)]
        finally:
            self.dry_run = False

    def queue_peek(self, limit):
        self.dry_run = True
        try:
            return [dict(r) for r in super().queue_peek(limit)]
        finally:
            self.dry_run = False

    def queue_result(self, items):
        self.dry_run = True
        try:
            self._r("queue_result", items)
            return super().queue_result(items)
        finally:
            self.dry_run = False

    def queue_stats(self):
        self.dry_run = True
        try:
            return super().queue_stats()
        finally:
            self.dry_run = False

    def status_of(self, key):
        return self._mem[key]["status"]

    def sources_extra(self):
        return []

    def event_index(self):
        return list(self.index)

    def enqueue(self, events):
        self._r("enqueue", events)
        self.enqueued += events
        for e in events:  # 다음 실행의 행사 목록에 반영
            self.index.append({k: e.get(k) for k in ("event_key", "title", "org", "url", "start_date", "identity")})
        return {"outbox": len(events)}

    def watch_due(self, limit):
        return getattr(self, "due_watch", [])

    def watch_update(self, items):
        self._r("watch_update", items)

    def ai_health(self):
        return self.health

    def unverified(self, items, outage_hours, pending):
        self._r("unverified", items, outage_hours, pending)
        return {"sent": 1}

    def dispatch(self):
        return {"sent": 0}

    def digest(self):
        return {"messages": 0}

    def month_usd(self):
        return self.spent

    def add_usage(self, by_model, budget):
        self._r("usage", by_model, budget)

    def candidates(self, c):
        self._r("candidates", c)

    def candidates_unverified(self):
        return []

    def candidates_verify(self, r):
        return {}

    def run_log(self, body):
        self._r("run_log", body)
