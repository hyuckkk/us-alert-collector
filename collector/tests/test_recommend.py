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
                summary_ko="무엇을 하는지", highlights=[], reason="이유", special_reason="한정 기간 무료" if level == "push" else "",
                special_basis="free_limited" if level == "push" else "none", evidence_quote="free admission" if level == "push" else "",
                usual_alternative="", blockers=[])
    base.update(k)
    return ai.Recommendation(**base)


def test_all_candidates_are_judged_in_batches_and_missing_ones_are_held():
    items = [({"title": f"e{i}", "bot": "life"}, "text") for i in range(5)]
    c = FakeClient(beta_replies=[resp([rec(0, "none"), rec(1, "push")]), resp([rec(0, "interest")]), resp([rec(0, "push")])])
    out = ai.recommend(c, CFG, items, "2026-10-22", ai.Usage({}))
    assert sorted(out.recs) == [0, 1, 2, 4] and out.missing == {3: "missing-recommendation"}
    assert len(c.beta.messages.calls) == 3
    sys = c.beta.messages.calls[0]["system"]
    for w in ("council/board/commission", "drug take-back", "toddler", "senior-only", "first-come", "special_reason", "THE KEY QUESTION",
              "Red Rocks", "full-price", "colloquia", "family of five", "a famous place is not a reason", "sound bath", "evidence_quote",
              "school_sports_regular", "english_listening_core", "pro_sports", "promo_wording_only", "applies to all three channels"):
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


# ---------------------------------------------------------------- 이슈 #21·#22·#24: 일반화된 적극 알림 게이트(모델 판단 + 코드 재검증)
def P(**k):
    base = dict(index=0, level="push", bot="life", category="other", score=90, title_ko="t", summary_ko="s", highlights=[], reason="r",
                special_reason="특별", special_basis="rare_one_off", evidence_quote="", usual_alternative="", blockers=[])
    base.update(k)
    return ai.Recommendation(**base)


NEGATIVE = [  # (설명, 판정, 원문) — 모델이 push 라고 해도 보내지 않아야 함
    ("$20 Hyland Hills 사운드 배스", P(special_basis="rare_one_off", evidence_quote="Sound Bath", blockers=["routine_commercial_program"]),
     "Sound Bath. Hyland Hills Fitness Center. $20. Bring a yoga mat. Registration required."),
    ("평소 $25 요가·명상 수업", P(special_basis="scarce_quota_or_lottery", evidence_quote="limited spots", blockers=["routine_commercial_program", "promo_wording_only"]),
     "Gentle Yoga & Meditation $25 drop-in. Limited spots!"),
    ("유료 도자기·쿠킹 클래스", P(special_basis="rare_one_off", evidence_quote="one-time workshop", blockers=["routine_commercial_program"]),
     "Pottery Wheel one-time workshop $65 per person"),
    ("시립 오케스트라 정기공연", P(special_basis="public_subsidy_price", evidence_quote="season concert", blockers=["routine_performance"]),
     "Westminster Symphony season concert tickets $30"),
    ("정상가 프로경기", P(special_basis="rare_one_off", evidence_quote="Nuggets vs", blockers=["pro_sports"]), "Nuggets vs Lakers tickets from $85"),
    ("Red Rocks 통상 공연", P(special_basis="rare_one_off", evidence_quote="Red Rocks", blockers=["routine_performance"]), "Live at Red Rocks tickets $79.50"),
    ("영어 강연·다큐 대담", P(special_basis="free_limited", evidence_quote="free admission", blockers=["english_listening_core"]),
     "Documentary screening and panel discussion with the director. Free admission."),
    ("외계행성 이론 강연", P(special_basis="public_subsidy_price", evidence_quote="free lecture", blockers=["english_listening_core"]),
     "Exoplanet climates and habitability: a free lecture by Prof. X"),
    ("대학 클럽 아이스하키 정기 경기", P(special_basis="school_community_nonregular", evidence_quote="vs Denver", blockers=["school_sports_regular"]),
     "CU Club Ice Hockey D2 vs Denver. Student Recreation Center Ice Rink."),
    ("고교 정규 시즌 경기", P(special_basis="school_community_nonregular", evidence_quote="Varsity Football", blockers=["school_sports_regular"]),
     "Varsity Football: Legacy vs Mountain Range, Friday 7pm"),
    ("홍보 문구만 '특별'(근거 없음)", P(special_basis="rare_one_off", evidence_quote="exclusive one-night-only experience", blockers=[]),
     "Wine tasting $45. Book now!"),  # 인용이 원문에 없음
    ("매주 금요일 가이드 투어 한 회차", P(special_basis="rare_one_off", evidence_quote="Highlights Tour", blockers=["recurring_series"]),
     "Friday Highlights Tour every Friday 10:30am, $3 plus admission"),
    ("근거 종류 없음", P(special_basis="none", evidence_quote="Fall Festival"), "Fall Festival"),
]
POSITIVE = [
    ("주정부 지원 무료 체험", P(special_basis="public_subsidy_price", evidence_quote="free thanks to Colorado Parks and Wildlife"),
     "Learn to fish - free thanks to Colorado Parks and Wildlife. Equipment provided."),
    ("공군기지 특별 개방", P(special_basis="restricted_access", evidence_quote="open to the public"),
     "Buckley Space Force Base open house, open to the public on Saturday only."),
    ("한정 비행장 직접체험", P(special_basis="scarce_quota_or_lottery", evidence_quote="limited to 40 participants"),
     "Airport rescue vehicle hands-on day, limited to 40 participants, free."),
    ("학교 지역 토너먼트(비정기)", P(special_basis="school_community_nonregular", evidence_quote="district tournament"),
     "Adams 12 district tournament finals, open to families, free entry"),
    ("공공지원 대폭 할인", P(special_basis="public_subsidy_price", evidence_quote="SCFD free day"), "Denver Zoo SCFD free day, reserve timed tickets"),
]


@pytest.mark.parametrize("name,r,text", NEGATIVE, ids=[n for n, _, _ in NEGATIVE])
def test_negative_cases_never_push(name, r, text):
    ev = {"title": "x"}
    ai.apply_recommendation(ev, r, text)
    assert ev["rec_level"] != "push" and ev["special_reason"] == "" and ev["push_blocked"], (name, ev)


@pytest.mark.parametrize("name,r,text", POSITIVE, ids=[n for n, _, _ in POSITIVE])
def test_positive_cases_push(name, r, text):
    ev = {"title": "x"}
    ai.apply_recommendation(ev, r, text)
    assert ev["rec_level"] == "push" and ev["special_reason"] and ev["push_blocked"] == "", (name, ev)


def test_evidence_may_come_from_extracted_fields_and_is_case_insensitive():
    ev = {"title": "Free Night at Wings Over the Rockies", "cost_text": "Free with ticket"}
    ai.apply_recommendation(ev, P(special_basis="free_limited", evidence_quote="FREE NIGHT at Wings"), "")
    assert ev["rec_level"] == "push"
