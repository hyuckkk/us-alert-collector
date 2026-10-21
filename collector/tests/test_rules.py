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


def test_bad_date_becomes_null_and_undated_is_held():
    # 10차 시험 이후: 날짜·접수일이 전혀 없으면 지난 행사인지 알 수 없어 보내지 않는다(이전: 'info' 로 주간 요약)
    out, dropped = rules.finalize([res(0, ev(start_date="July 10", tags=["art"]))], E, TODAY)
    assert out == [] and dropped.get("no-date") == 1
    out, _ = rules.finalize([res(0, ev(start_date="July 10", annual=True))], E, TODAY)
    assert out[0]["start_date"] is None                     # 연례 행사는 예고용으로 남김


def test_urgency_rules():
    assert rules.urgency({"deadline": "2026-10-25"}, TODAY) == "now"
    assert rules.urgency({"deadline": "2026-11-25"}, TODAY) == "weekly"
    assert rules.urgency({"signup": "fcfs", "registration_opens": "2026-10-22"}, TODAY) == "now"
    assert rules.urgency({"signup": "open", "registration_opens": "2026-10-22"}, TODAY) == "weekly"


def test_priority_prefers_registration_words_and_near_dates():
    e1 = Entry("a", "https://x.org/1", "Summer camp registration opens Oct 25", "Limited spots")
    e2 = Entry("b", "https://x.org/2", "City council meeting minutes")
    assert rules.priority(e1, TODAY, True) > rules.priority(e2, TODAY, True)
    assert rules.near_dates("Join us Oct 25 and Nov 30", TODAY) == 1


def test_year_label_and_annual():
    assert rules.year_label({"start_date": "2027-07-01"}, TODAY) == "2027년 행사"
    assert rules.year_label({"start_date": "2026-12-01", "annual": True}, TODAY).startswith("연례행사")


def test_apply_verdicts():
    events = [{"event_key": "a", "bot": "kids", "age_text": None, "english": []}, {"event_key": "b", "bot": "life"}]
    V = lambda **k: SimpleNamespace(**{"index": 0, "keep": True, "reason": "ok", "bot": "family", "age_text": "6+", "status_note": None, "english": ["보호자 동반"], **k})  # noqa: E731
    out, removed = rules.apply_verdicts(events, [0, 1], [V(), V(index=1, keep=False)])
    assert removed == 1 and out == [{"event_key": "a", "bot": "family", "age_text": "6+", "status_note": None, "english": ["보호자 동반"], "review_reason": "ok"}]


def test_for_worker_drops_internal_fields_and_keeps_registration():
    out, _ = rules.finalize([res(0, ev(registration_opens="2027-01-05", registration_status="upcoming", eligibility="주민 우선"))], E, TODAY)
    import identity
    identity.assign_keys(out, [])
    w = rules.for_worker(out[0])
    assert "_entry_index" not in w and "_anchor" not in w and "paid_work" not in w and w["bot"] == "kids"
    assert (w["registration_opens"], w["registration_status"], w["eligibility"]) == ("2027-01-05", "upcoming", "주민 우선")
    assert w["event_key"].startswith("e") and w["identity"]


def test_cancelled_is_urgent():
    assert rules.urgency({"registration_status": "cancelled"}, TODAY) == "now"
