"""이슈 #43 P0 판정 규칙·경기 달력 정규화 회귀(토큰 0).

모델이 push 라고 답해도 코드 게이트가 막아야 하는 경우와, 근거가 갖춰지면 통과하는 경우를 실제 공식 원문 문장으로 고정한다.
"""
from types import SimpleNamespace

import pytest

import ai
import rules
from conftest import ev as make_ev
from test_recommend import DEFAULTS
from test_run import world  # noqa: F401

pytestmark = pytest.mark.real_recommend


def R(**k):
    base = dict(index=0, level="push", bot="family", category="outdoors", score=90, title_ko="t", summary_ko="s", highlights=[], reason="r",
                special_reason="특별", special_basis="rare_one_off", evidence_quote="", usual_alternative="", blockers=[], **DEFAULTS)
    base.update(k)
    return ai.Recommendation(**base)


USAFA = ("International guests Non-U.S. citizens must visit the Pass and Registration Office (PRO) in person with a valid passport. "
         "Background Checks: All individuals seeking access will undergo a mandatory criminal background check.")
DSS_FAQ = ("2026 Pricing: Dynamic Steel - $10 ($5 for additional gun/run) Steel Challenge - $15 ($10 for additional gun/run) USPSA - $25. "
           "How do I pay my match fee? Match fees are paid for at the range.")
RAFT = ("Minimum age is 13 during peak runoff (May 15 - June 30), 10 in July and 7 from August 1. All guests must weigh at least 50 lbs. "
        "Bighorn Sheep Canyon, Class II-III family trip.")

# (설명, 행사, 판정, 원문, 기대: (rec_level, hold_reason, push_blocked 시작 문자열))
CASES = [
    # (A) 사격
    ("사격장 상시 기관총 체험 패키지", {"title": "Machine Gun Experience Package"},
     R(topic="shooting", special_basis="rare_one_off", evidence_quote="Machine Gun Experience", blockers=["routine_range_offer"], fee_note="패키지 $199"),
     "Machine Gun Experience Package $199 includes rental and 50 rounds. Book any day.", ("interest", "", "blocked")),
    ("모델이 차단 사유를 빠뜨려도 비용 구분이 없으면 막음", {"title": "Try a machine gun"},
     R(topic="shooting", special_basis="rare_one_off", evidence_quote="Try a machine gun"),
     "Try a machine gun today", ("interest", "", "shooting-fee-breakdown-missing")),
    ("대회 참가비 $10 을 사격 체험가로 해석", {"title": "Dynamic Steel Match"},
     R(topic="shooting", special_basis="public_subsidy_price", evidence_quote="Match fees are paid for at the range", fee_note="참가비 $10"),
     DSS_FAQ, ("interest", "", "match-fee-is-not-experience-price")),
    ("연례 대규모 공개 사격 축제(비용 구분 있음)", {"title": "Big Sandy Machine Gun Shoot"},
     R(topic="shooting", special_basis="rare_one_off", evidence_quote="held twice a year", fee_note="관람 입장료 별도, 사격은 사수 개별 비용"),
     "The Big Sandy Shoot is held twice a year and is open to spectators.", ("push", "", "")),
    ("공식 대회 참가 접수(참가비와 총기·탄약 분리)", {"title": "Colorado State IDPA Championship"},
     R(topic="shooting", special_basis="scarce_quota_or_lottery", evidence_quote="registration opens", fee_note="참가비 $120(경기 등록) · 총기·탄약 본인 지참"),
     "State Championship registration opens Jan 5, limited to 150 shooters.", ("push", "", "")),
    # (B) 군사시설 외국인 출입
    ("기지 개방: 'open to the public' 만 있음 → 미확인 보류", {"title": "Buckley Space Force Base Open House"},
     R(topic="military", special_basis="restricted_access", evidence_quote="open to the public", foreign_access="unknown"),
     "Buckley Space Force Base open house, open to the public on Saturday only.", ("interest", "foreign-access-unverified", "foreign-access-unverified")),
    ("모델이 topic 을 빠뜨려도 기지명으로 군사시설 판정", {"title": "Peterson SFB family day"},
     R(topic="other", special_basis="restricted_access", evidence_quote="family day", foreign_access="not_applicable"),
     "Peterson SFB family day", ("interest", "foreign-access-unverified", "foreign-access-unverified")),
    ("시민권·영주권 필요 → 추천 제외", {"title": "Base tour"},
     R(topic="military", special_basis="restricted_access", evidence_quote="Base tour", foreign_access="restricted",
       access_quote="Tours are open to U.S. citizens only"),
     "Base tour. Tours are open to U.S. citizens only.", ("none", "foreign-access-restricted", "foreign-access-restricted")),
    ("확인 주장인데 인용이 원문에 없음 → 보류", {"title": "USAFA Open House"},
     R(topic="military", special_basis="restricted_access", evidence_quote="Open House", foreign_access="confirmed",
       access_quote="foreign visitors are welcome without a pass"),
     "USAFA Open House. " + USAFA, ("interest", "foreign-access-unverified", "foreign-access-unverified")),
    ("외국인 절차가 공식 문장으로 확인됨 → 적극 알림", {"title": "USAFA Open House"},
     R(topic="military", special_basis="restricted_access", evidence_quote="Open House", foreign_access="confirmed",
       access_quote="Non-U.S. citizens must visit the Pass and Registration Office (PRO) in person with a valid passport",
       access_note="여권 원본 지참, 방문 출입증 사무소 현장 심사, 신원 조회"),
     "USAFA Open House. " + USAFA, ("push", "", "")),
    # (C) 가족 카약·래프팅
    ("성수기 최소 13세 코스 → 막내 불가", {"title": "Royal Gorge Rafting"},
     R(topic="paddling", special_basis="rare_one_off", evidence_quote="Class II-III", family_water="not_all", water_note="6월 최소 13세"),
     RAFT, ("interest", "", "family-water-not_all")),
    ("연령·체중 미확인 → 적극 알림 아님", {"title": "Kayak Day"},
     R(topic="paddling", special_basis="free_limited", evidence_quote="free kayak", family_water="unknown"),
     "free kayak rides at the lake", ("interest", "", "family-water-unknown")),
    ("모델이 topic 을 빠뜨려도 제목의 래프팅으로 판정", {"title": "Family Rafting Trip"},
     R(topic="other", special_basis="free_limited", evidence_quote="free", family_water="not_applicable"),
     "Family Rafting Trip free", ("interest", "", "family-water-unknown")),
    ("관람형 급류 축제는 동승 조건 없이 일반 기준", {"title": "Whitewater Festival"},
     R(topic="paddling", special_basis="rare_one_off", evidence_quote="once a year", family_water="not_applicable"),
     "Whitewater Festival once a year, free to watch", ("push", "", "")),
    ("8월 최소 7세·50lb, 같은 보트 가능 → 적극 알림", {"title": "Bighorn Sheep Canyon family trip"},
     R(topic="paddling", special_basis="scarce_quota_or_lottery", evidence_quote="7 from August 1", family_water="all_together",
       water_note="8월부터 최소 7세·50lb, Class II-III, 같은 보트 5인 가능, 가족 총비용 약 $400"),
     RAFT, ("push", "", "")),
]


@pytest.mark.parametrize("name,event,r,text,want", CASES, ids=[c[0] for c in CASES])
def test_p0_gates(name, event, r, text, want):
    e = dict(event)
    ai.apply_recommendation(e, r, text)
    level, hold, blocked = want
    assert e["rec_level"] == level, (name, e)
    assert e["hold_reason"] == hold, (name, e)
    assert (e["push_blocked"] or "").startswith(blocked) if blocked else e["push_blocked"] == "", (name, e)


def test_status_note_carries_access_water_and_fee_notes_once():
    e = {"title": "USAFA Open House", "status_note": "사진 촬영 제한"}
    r = CASES[9][2]
    ai.apply_recommendation(e, r, "USAFA Open House. " + USAFA)
    ai.apply_recommendation(e, r, "USAFA Open House. " + USAFA)   # 재평가해도 같은 문구를 두 번 붙이지 않는다
    assert e["status_note"].startswith("사진 촬영 제한; 외국인 출입: 공식 확인 — 여권 원본")
    assert e["status_note"].count("외국인 출입") == 1
    w = {"title": "Bighorn Sheep Canyon family trip"}
    ai.apply_recommendation(w, CASES[-1][2], RAFT)
    assert "가족 동승: 아빠+아이 3명 같은 일정 가능" in w["status_note"] and "50lb" in w["status_note"]
    s = {"title": "State Championship"}
    ai.apply_recommendation(s, CASES[4][2], CASES[4][3])
    assert "비용 구분: 참가비 $120(경기 등록) · 총기·탄약 본인 지참" in s["status_note"]


def test_night_car_culture_goes_to_life_only():
    e = {"title": "Import Face-Off night drag"}
    ai.apply_recommendation(e, R(topic="motorsport_night", bot="family", special_basis="rare_one_off", evidence_quote="once a year"),
                            "Import Face-Off night drag, once a year at the dragstrip")
    assert e["bot"] == "life" and e["rec_level"] == "push"


def test_street_takeover_is_blocked():
    e = {"title": "Car meet takeover"}
    ai.apply_recommendation(e, R(topic="motorsport_night", evidence_quote="meet", blockers=["unsanctioned_or_illegal"]), "Car meet takeover")
    assert e["rec_level"] == "interest" and e["push_blocked"].startswith("blocked")


def test_prompt_states_p0_rules():
    for w in ("FOREIGN NATIONALS", "\"Open to the public\" alone is unknown", "ONE parent and THREE children", "$10 match fee",
              "routine_range_offer", "bot=life only", "RIDE (tethered or free flight", "a minimum-age rule of one outfitter does not apply to others"):
        assert w in ai.RECOMMEND_RULES, w
    for w in ("One meet = ONE record", "SPECTATOR admission only", "invitational, tri-meet, quad meet"):
        assert w in ai.EXTRACT_RULES, w


# ---------------------------------------------------------------- 경기 달력 정규화(#27)
def E(**k):
    return SimpleNamespace(source_id=k.pop("source_id", "chsaa-gymnastics-athletics"), url=k.pop("url", "https://x/#1"), title="t",
                           text="", published=k.pop("published", None), extra=k.pop("extra", {}))


def norm(entry=None, source=None, **ev):
    d = make_ev(**ev).model_dump()
    return rules.sports_normalize(d, entry or E(), source)


def test_gymnastics_meet_is_one_meet_not_fake_matchup():
    d = norm(title="Front Range League Gymnastics Invitational", title_ko="프론트레인지 리그 체조", location="Thornton High School",
             participants=["Mountain Range", "Legacy", "Mountain Range", "Horizon"], cost_text=None, source={"school_level": "high"})
    assert (d["sport"], d["event_type"], d["school_level"]) == ("gymnastics", "meet", "high")
    assert d["home_team"] is None and d["away_team"] is None and d["participants"] == ["Mountain Range", "Legacy", "Horizon"]
    assert d["venue"] == "Thornton High School" and d["cost_class"] == "unknown"
    w = rules.for_worker(dict(d, event_key="e1"))
    assert w["title_ko"] == "프론트레인지 리그 체조 대회 경기" and all(k in w for k in rules.SPORTS_FIELDS)


def test_quad_meet_at_university_and_vs_match():
    d = norm(title="Women's Gymnastics at Denver Quad Meet", title_ko="여자 체조", source={"school_level": "university"})
    assert d["event_type"] == "meet" and d["home_team"] is None
    m = norm(title="Mountain Range vs Legacy Volleyball", title_ko="배구")
    assert (m["event_type"], m["home_team"], m["away_team"]) == ("match", "Mountain Range", "Legacy Volleyball")
    a = norm(title="Football: Westminster at Centaurus")
    assert (a["home_team"], a["away_team"]) == ("Centaurus", "Football: Westminster")


def test_source_school_level_overrides_model_and_ics_time_is_local():
    d = norm(entry=E(published="20261030T010000Z", extra={"uid": "x"}), source={"school_level": "university", "level_detail": "club"},
             title="Ice Hockey D2 vs Denver", school_level="high", start_time="7pm")
    assert d["school_level"] == "university" and d["level_detail"] == "club" and d["start_time"] == "19:00"   # 01:00Z = 덴버 19:00(전날)
    allday = norm(entry=E(published="20261030T060000Z", extra={"uid": "x"}), title="Basketball vs Mines")
    assert allday["start_time"] is None   # 덴버 자정 = 종일 일정


def test_admission_cost_class_never_free_without_word():
    assert rules.cost_class("무료") == "free" and rules.cost_class("Free admission") == "free"
    assert rules.cost_class("$5 adults, students free") == "paid"
    assert rules.cost_class("") == "unknown" and rules.cost_class("입장료 별도") == "unknown"


def test_non_sports_event_untouched_and_lessons_not_games():
    d = norm(title="Kids Clay Workshop")
    assert d.get("event_type") is None and d.get("cost_class") is None
    lesson = norm(title="Winter Gymnastics Registration", event_type=None)
    assert lesson["sport"] == "gymnastics" and lesson["event_type"] is None   # 강습 등록은 경기가 아니다
    w = rules.for_worker(dict(lesson, event_key="e2"))
    assert "경기" not in w["title_ko"]


# ---------------------------------------------------------------- 정기 경기는 달력 전용(추천 판정 생략), 콜로라도 밖 경기는 저장 안 함
def test_routine_game_vs_special_game():
    g = norm(title="University of Denver Women's Soccer vs Saint Mary's", source={"school_level": "university"})
    assert rules.routine_game(g)
    rules.calendar_only(g)
    assert g["rec_level"] == "none" and g["rec_blockers"] == ["school_sports_regular"] and g["push_blocked"] == "calendar-only" and g["category"] == "sports"
    for t in ("CHSAA Gymnastics State Championship", "Gymnastics Regionals: Mountain Range Regional", "DU Hockey Kids Day vs Omaha"):
        assert not rules.routine_game(norm(title=t, source={"school_level": "high"})), t
    assert not rules.routine_game(norm(title="Winter Gymnastics Registration"))       # 경기가 아님(학교급 없음·형식 없음)
    assert not rules.routine_game(norm(title="Kids Clay Workshop"))


def test_out_of_state_games_are_dropped_colorado_away_games_kept():
    far = norm(title="University of Denver Women's Tennis at Husker Invitational", location="Lincoln, Neb.", source={"school_level": "university"})
    near = norm(title="Air Force Women's Gymnastics at Denver Quad", location="Magness Arena, Denver, Colo.", source={"school_level": "university"})
    assert rules.out_of_state_game(far) and not rules.out_of_state_game(near)
    assert not rules.out_of_state_game(norm(title="Workshop", location="Lincoln, Neb."))   # 경기가 아니면 이 규칙 적용 안 함


def test_run_skips_paid_recommendation_for_routine_games(world, monkeypatch):
    import run
    from conftest import FakeAPI, FakeClient
    from test_run import reply_for
    seen = []

    def fake_rec(client, cfg, items, today, usage):
        seen.extend(e["title"] for e, _ in items)
        return ai.RecommendOutcome()
    monkeypatch.setattr(ai, "recommend", fake_rec)

    def extract(kw):
        n = kw["messages"][0]["content"].count('"index"')
        return ai.ExtractBatch(results=[ai.EntryResult(index=i, relevant=True, reason="", events=[
            make_ev(title="Mountain Range vs Legacy Volleyball", school_level="high", sport="volleyball", event_type="match", url="https://butterflies.org/events/clay"),
            make_ev(title="Kids Clay Workshop", url="https://butterflies.org/events/clay")]) for i in range(n)])
    api = FakeAPI()
    rep = run.main([], client=FakeClient([extract]), api=api)
    assert seen == ["Kids Clay Workshop"]                     # 정기 경기는 큰 모델에 보내지 않음
    assert rep["metrics"]["calendar_only"] == 1
    games = [e for e in api.enqueued if e["title"].startswith("Mountain Range")]
    assert games and games[0]["rec_level"] == "none" and games[0]["event_type"] == "match" and "경기" in games[0]["title_ko"]
