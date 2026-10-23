"""추천 판정 단계(소유자 피드백 2026-10-22: 일상 봇에 시의회 회의·약물 반납·영유아·시니어 프로그램이 성인 체험 알림으로 섞임)."""
from types import SimpleNamespace

import pytest

import ai
from conftest import U, FakeClient
from test_run import world  # noqa: F401

pytestmark = pytest.mark.real_recommend
CFG = {"recommend": {"model": "claude-opus-5-5", "effort": "high", "batch": 2, "max_tokens": 100, "reserve_usd": 0.5}}


def resp(items):
    return SimpleNamespace(parsed_output=ai.RecommendBatch(items=items), stop_reason="end_turn", usage=U())


def rec(i, level, **k):
    base = dict(index=i, level=level, bot="life", category="civic" if level == "none" else "performance", score=10, title_ko=f"행사 {i}",
                summary_ko="무엇을 하는지", highlights=[], reason="이유", special_reason="한정 기간 무료" if level == "push" else "")
    base.update(k)
    return ai.Recommendation(**base)


def test_all_candidates_are_judged_in_batches_and_missing_ones_are_held():
    items = [({"title": f"e{i}", "bot": "life"}, "text") for i in range(5)]
    c = FakeClient(beta_replies=[resp([rec(0, "none"), rec(1, "push")]), resp([rec(0, "interest")]), resp([rec(0, "push")])])
    out = ai.recommend(c, CFG, items, "2026-10-22", ai.Usage({}))
    assert sorted(out.recs) == [0, 1, 2, 4] and out.missing == {3: "missing-recommendation"}
    assert len(c.beta.messages.calls) == 3
    sys = c.beta.messages.calls[0]["system"]
    for w in ("council/board/commission", "drug take-back", "toddler", "senior-only", "first-come", "purpose", "special_reason",
              "Red Rocks", "full-price", "colloquia", "family of five", "never because a venue is famous"):
        assert w in sys, w


def test_apply_recommendation_sets_level_bot_and_concise_title():
    ev = {"title": "City Council Regular Meeting", "title_ko": "시의회 정례 회의", "bot": "life"}
    ai.apply_recommendation(ev, rec(0, "none", category="civic", title_ko="시의회 회의", reason="행정 회의라 참가 경험이 아님"))
    assert ev["rec_level"] == "none" and ev["category"] == "civic" and ev["title_ko"] == "시의회 회의"
    ev2 = {"title": "Toddler Storytime", "bot": "life"}
    ai.apply_recommendation(ev2, rec(0, "interest", bot="kids", category="toddler"))
    assert ev2["bot"] == "kids" and ev2["rec_level"] == "interest"


def test_recommend_failure_holds_events(world, monkeypatch):
    """추천 판정이 실패하면 그 항목을 보내지 않고 대기열에 남긴다(판정 없는 행사는 발송하지 않음)."""
    import run
    from conftest import FakeAPI
    from test_run import CLAY, reply_for

    def boom(*a, **k):
        raise ValueError("recommend down")
    monkeypatch.setattr(ai, "recommend", boom)
    api = FakeAPI()
    rep = run.main([], client=FakeClient([reply_for([CLAY])]), api=api)
    assert api.enqueued == [] and rep["metrics"]["recommend_missing"] == 1
    assert [r["status"] for r in api._mem.values()] == ["queued"]


def test_push_without_special_reason_is_downgraded():
    """소유자 기준(이슈 #20): 적극 알림은 특별한 근거가 있어야 한다. 근거가 비면 관심 설정 시로 낮춘다(Red Rocks 정가 공연 등)."""
    ev = {"title": "Red Rocks: Regular Concert", "bot": "life"}
    ai.apply_recommendation(ev, rec(0, "push", category="performance", special_reason="  "))
    assert ev["rec_level"] == "interest" and ev["special_reason"] == ""
    ev2 = {"title": "Buckley Space Force Base open house", "bot": "family"}
    ai.apply_recommendation(ev2, rec(0, "push", category="outdoors", special_reason="평소 비공개 기지를 하루만 개방"))
    assert ev2["rec_level"] == "push" and ev2["special_reason"] == "평소 비공개 기지를 하루만 개방"
