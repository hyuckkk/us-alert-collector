from datetime import date
from types import SimpleNamespace

import rules
from conftest import ev
from fetch import Entry
from ai import EntryResult

TODAY = date(2026, 10, 20)
E = [Entry("butterfly", "https://b.org/e/1", "Kids Clay"), Entry("gnews:x", "https://news.com/a", "News about clay")]


def res(i, *events, relevant=True):
    return EntryResult(index=i, relevant=relevant, reason="", events=list(events))


def test_url_not_from_input_is_replaced_by_collected_url():
    out, dropped = rules.finalize([res(0, ev(url="https://made-up.org/x"))], E, TODAY)
    assert out[0]["url"] == "https://b.org/e/1" and dropped["url-replaced"] == 1


def test_drops_past_paid_and_irrelevant():
    out, dropped = rules.finalize([res(0, ev(start_date="2026-10-01"), ev(title="Hiring instructors", paid_work=True)), res(1, relevant=False)], E, TODAY)
    assert out == [] and dropped == {"past": 1, "paid-work": 1, "not-relevant": 1}


def test_bad_date_becomes_null_and_undated_is_info():
    out, _ = rules.finalize([res(0, ev(start_date="July 10", tags=["art"]))], E, TODAY)
    assert out[0]["start_date"] is None and "info" in out[0]["tags"]


def test_urgency_rules():
    assert rules.urgency({"deadline": "2026-10-25"}, TODAY) == "now"
    assert rules.urgency({"deadline": "2026-11-25"}, TODAY) == "weekly"
    assert rules.urgency({"signup": "fcfs", "registration_opens": "2026-10-22"}, TODAY) == "now"
    assert rules.urgency({"signup": "open", "registration_opens": "2026-10-22"}, TODAY) == "weekly"


def test_event_key_is_stable_across_sources_and_prefers_official():
    a = res(0, ev(title="The Kids Clay Workshop 2027"))
    b = res(1, ev(title="kids clay workshop", url="https://news.com/a"))
    out, dropped = rules.finalize([b, a], E, TODAY)
    assert len(out) == 1 and dropped["duplicate"] == 1 and out[0]["source_id"] == "butterfly"
    assert len(out[0]["event_key"]) == 16


def test_year_label_and_annual():
    assert rules.year_label({"start_date": "2027-07-01"}, TODAY) == "2027년 행사"
    assert rules.year_label({"start_date": "2026-12-01", "annual": True}, TODAY).startswith("연례행사")


def test_apply_verdicts():
    events = [{"event_key": "a", "bot": "kids", "age_text": None, "english": []}, {"event_key": "b", "bot": "life"}]
    V = lambda **k: SimpleNamespace(**{"index": 0, "keep": True, "reason": "ok", "bot": "family", "age_text": "6+", "status_note": None, "english": ["보호자 동반"], **k})  # noqa: E731
    out, removed = rules.apply_verdicts(events, [0, 1], [V(), V(index=1, keep=False)])
    assert removed == 1 and out == [{"event_key": "a", "bot": "family", "age_text": "6+", "status_note": None, "english": ["보호자 동반"], "review_reason": "ok"}]


def test_for_worker_drops_internal_fields():
    out, _ = rules.finalize([res(0, ev())], E, TODAY)
    w = rules.for_worker(out[0])
    assert "_entry_index" not in w and "paid_work" not in w and w["bot"] == "kids"
