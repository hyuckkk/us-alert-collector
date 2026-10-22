"""전체 수집 성능 평가(eval_stages)와 실행 기록(--trace) 시험(9차)."""
import json
from datetime import date

import eval_stages
import run
from conftest import FakeAPI, FakeClient
from test_run import CLAY, reply_for, world  # noqa: F401

GT = {"events": [
    {"id": "a", "title": "Kids Clay Workshop", "url": CLAY, "participation": "open", "date_confirmed": True, "start_date": "2027-07-10",
     "cost_text": "$20", "age_limit": "6–10세"},
    {"id": "b", "title": "Winter Swim Registration", "url": "https://city.gov/reg", "participation": "registration_opening", "date_confirmed": True,
     "start_date": "2026-11-30", "cost_text": "미확인", "age_limit": None},
    {"id": "c", "title": "Free Museum Day November", "url": "https://museum.org/free-day", "participation": "open", "date_confirmed": True,
     "start_date": "2026-11-07", "cost_text": "무료", "age_limit": None}],
    "excluded": [{"id": "x", "title": "Staff Mindfulness Series", "url": "https://u.edu/staff", "participation": "restricted"}]}


def test_stage_attribution_accuracy_and_notify():
    trace = {"entries": [
        {"stage": "sources", "url": CLAY, "title": "Kids Clay Workshop"},
        {"stage": "search", "url": "https://news.google.com/x", "title": "Free Museum Day November at the museum"},
        {"stage": "discover", "url": "https://city.gov/reg", "title": "Winter swim registration dates"},
        {"stage": "sources", "url": "https://u.edu/staff", "title": "Staff Mindfulness Series"}],
        "ai": [{"url": CLAY, "status": "extracted", "events": 1}],
        "events": [
            {"url": CLAY, "title": "Kids Clay Workshop", "start_date": "2027-07-10", "cost_text": "$20", "age_text": "6-10", "bot": "kids", "urgency": "weekly"},
            {"url": "https://city.gov/reg", "title": "Winter Swim Registration", "start_date": "2027-01-05", "registration_opens": "2026-12-01", "bot": "kids", "urgency": "now"},
            {"url": "https://u.edu/staff", "title": "Staff Mindfulness Series", "start_date": "2026-11-01", "bot": "life"}]}
    res = eval_stages.evaluate(GT, trace, date(2026, 10, 21), "2027-06-01")
    rows = {r["id"]: r for r in res["rows"]}
    assert rows["a"]["stages"] == ["sources"] and rows["a"]["date"] == "일치" and rows["a"]["cost"] == "일치" and rows["a"]["age"] == "일치"
    assert rows["b"]["stages"] == ["discover"] and rows["b"]["date"].startswith("불일치")      # 접수 시작일 오류를 잡는다
    assert rows["c"]["stages"] == ["search"] and not rows["c"]["event"]                      # 검색에서만 보였고 행사가 되지 못함
    assert rows["a"]["notify_owner"] == "대상(요약)" and rows["b"]["notify_owner"].startswith("아님(참가 가능일")
    assert rows["x"]["notify_friend"].startswith("대상")
    s = res["summary"]
    assert s["found_any_stage"].startswith("3/3") and s["became_event"].startswith("2/3") and s["excluded_wrongly_notified"] == "1/1"


def test_run_writes_trace_with_stage_and_ai_outcome(world, tmp_path):  # noqa: F811
    t = tmp_path / "trace.json"
    run.main(["--trace", str(t)], client=FakeClient([reply_for([CLAY])]), api=FakeAPI())
    tr = json.loads(t.read_text())
    assert any(e["stage"] == "sources" and e["url"] == CLAY for e in tr["entries"])
    assert any(a["url"] == CLAY and a["status"] == "extracted" and a["events"] == 1 for a in tr["ai"])
    assert tr["events"] and tr["events"][0]["url"] == CLAY
