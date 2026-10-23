"""경로별 누락 보고: 실제로 돈 경로만 평가, 적극 알림만, 이유·보완을 교훈 파일에 쌓고 보고문은 누락이 있을 때만."""
import ai
import miss_report as mr

SINCE = "2026-10-23T00:00:00+00:00"


def ev(key, finds, level="push", title=None):
    return {"event_key": key, "title": title or f"행사 {key}", "final_level": level,
            "finds": {p: {"first_seen": "2026-10-23T05:00:00Z", "url": f"https://org.example/{key}"} for p in finds}}


def test_compute_counts_only_paths_that_ran_and_push_events():
    compare = {"events": [ev("a", ["github_collector", "chatgpt"]), ev("b", ["chatgpt"]), ev("c", ["github_collector"]),
                          ev("d", ["chatgpt"], level="interest"),
                          {**ev("old", ["chatgpt"]), "finds": {"chatgpt": {"first_seen": "2026-10-01T00:00:00Z"}}}]}
    workload = [{"path": "github_collector", "started_at": "2026-10-23T02:00:00Z"}, {"path": "chatgpt", "started_at": "2026-10-23T03:00:00Z"}]
    r = mr.compute(compare, workload, SINCE)
    assert r["ran"] == {"github_collector": True, "claude": False, "chatgpt": True}
    assert r["total"] == 3
    assert [e["event_key"] for e in r["missed"]["github_collector"]] == ["b"]
    assert [e["event_key"] for e in r["missed"]["chatgpt"]] == ["c"]
    assert r["missed"]["claude"] == [], "돌지 않은 경로는 누락으로 세지 않음"


def test_lessons_written_and_deduped_and_report(tmp_path, monkeypatch):
    fix = ai.GapFix(index=0, cause_code="query_gap", cause_ko="공항 행사는 항공 키워드로만 공지", monitor_url="https://org.example/events",
                    monitor_type="html", monitor_mode="list", monitor_name=None, queries=["airport open house Colorado"], query_bot="family")
    monkeypatch.setattr(ai, "gap_analysis", lambda client, cfg, items, hosts, usage: {0: fix})
    misses = [("chatgpt", ev("c", ["github_collector"], title="공항 카니발"))]
    ls = mr.lessons_for(misses, None, {}, ai.Usage({}))
    f = tmp_path / "LESSONS.md"
    assert mr.write_lessons(f, ls, "2026-10-23") == 1
    assert mr.write_lessons(f, ls, "2026-10-24") == 0, "같은 누락은 다시 쌓지 않음"
    t = f.read_text()
    assert "ChatGPT 검색 누락" in t and "airport open house Colorado" in t and "https://org.example/events" in t
    r = {"ran": {"github_collector": True, "claude": False, "chatgpt": True}, "total": 3, "found": {"github_collector": 2, "claude": 0, "chatgpt": 2},
         "missed": {"github_collector": [ev("b", ["chatgpt"])], "claude": [], "chatgpt": misses[0][1:]}}
    text = mr.render(r, {"b": {"cause": "등록 정보원이 아님", "rediscovered": 1}}, ls, "2026-10-23")
    assert "GitHub 수집기 누락 1건" in text and "등록 정보원이 아님 · 보완 후 다시 잡힘" in text
    assert "ChatGPT 검색 누락 1건 → 다음 ChatGPT 검색 범위에 반영" in text
    assert "Claude 검색 실행 기록 없음" in text
    none = {**r, "missed": {"github_collector": [], "claude": [], "chatgpt": []}}
    assert mr.render(none, {}, [], "2026-10-23") is None, "누락이 없으면 보내지 않음"
