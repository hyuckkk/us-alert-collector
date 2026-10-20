from datetime import date

import annual

CFG = {"items": [{"id": "x", "bot": "family", "title": "T", "title_ko": "티", "month": 5, "day": 1, "lead_days": 14, "url": "https://n.gov", "verified": False}]}


def test_due_window_and_unverified_note():
    assert annual.due_items(CFG, date(2027, 4, 10), "t") == []
    out = annual.due_items(CFG, date(2027, 4, 20), "t")
    assert out[0]["event_key"] == "a-x-2027" and out[0]["urgency"] == "now" and "공식 발표 확인" in out[0]["status_note"]


def test_year_wrap():
    cfg = {"items": [{**CFG["items"][0], "month": 1, "day": 5}]}
    assert annual.due_items(cfg, date(2026, 12, 28), "t")[0]["start_date"] == "2027-01-05"
