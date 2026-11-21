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


# ---------------------------------------------------------------- Claude 지난 제출 대조
def test_norm_url_host_path_encoding_and_google_news():
    assert mr.norm_url("https://www.Denverartmuseum.org/en/free-days/") == "denverartmuseum.org/en/free-days"
    assert mr.norm_url("https://botanicgardens%2Eorg/events?x=1#y") == "botanicgardens.org/events"
    assert mr.norm_url("https://news.google.com/rss/articles/abc?url=https%3A%2F%2Fwww.org.example%2Fe%2F") == "org.example/e"
    assert mr.norm_url("https://news.google.com/rss/articles/CBMiXYZ?oc=5") is None, "원 주소를 모르면 비교하지 않음"


def test_compute_does_not_count_claude_miss_when_claude_submitted_earlier():
    e = ev("a", ["github_collector"])
    e["finds"]["github_collector"]["url"] = "https://www.org.example/a/"
    e["start_date"] = "2026-12-06"
    workload = [{"path": "claude", "started_at": "2026-10-23T02:00:00Z"}, {"path": "github_collector", "started_at": "2026-10-23T02:00:00Z"}]
    bundle = [{"key": "org.example/a", "start": "2026-12-01", "end": "2026-12-20", "title": "시즌 묶음"}]
    r = mr.compute({"events": [e]}, workload, SINCE, bundle)
    assert r["missed"]["claude"] == [] and r["found"]["claude"] == 1, "같은 페이지·날짜 범위 안 → 이미 제출"
    other_dates = [{"key": "org.example/a", "start": "2027-01-10", "end": "2027-01-12"}]
    assert len(mr.compute({"events": [e]}, workload, SINCE, other_dates)["missed"]["claude"]) == 1, "날짜가 다르면 다른 행사"
    other_page = [{"key": "org.example/b", "start": "2026-12-01", "end": "2026-12-20"}]
    assert len(mr.compute({"events": [e]}, workload, SINCE, other_page)["missed"]["claude"]) == 1


def test_claude_submissions_reads_worktree_and_search_branches(tmp_path):
    import json
    import subprocess
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com"); run("config", "user.name", "t")
    d = tmp_path / "external-inbox/claude"; d.mkdir(parents=True)
    (d / "a.json").write_text(json.dumps({"events": [{"url": "https://x.example/a", "start_date": "2026-12-01"}]}))
    run("add", "."); run("commit", "-qm", "a")
    (d / "b.json").write_text(json.dumps({"events": [{"url": "https://x.example/b%2Ehtml", "start_date": "2026-12-02", "end_date": "2026-12-03"}]}))
    run("add", "."); run("commit", "-qm", "b")
    run("update-ref", "refs/remotes/origin/claude/search-20261121", "HEAD")
    run("reset", "-q", "--hard", "HEAD~1")
    keys = sorted(s["key"] for s in mr.claude_submissions(tmp_path))
    assert keys == ["x.example/a", "x.example/b.html"]


def lesson(path, url, monitor, date, q):
    return {"path": path, "title": "t", "host": "h", "url": url, "found_by": ["github_collector"], "cause": "c",
            "queries": [q], "monitor_url": monitor, "date": date}


def test_lessons_merged_by_monitor_url_and_pruned_when_claude_submitted(tmp_path):
    import json
    f = tmp_path / "LESSONS.md"
    old = [lesson("claude", "https://denverartmuseum.org/e1", "https://www.denverartmuseum.org/en/free-days", f"2026-11-1{i}", f"q{i}") for i in range(5)]
    old += [lesson("chatgpt", "https://denverartmuseum.org/e9", "https://denverartmuseum.org/en/free-days/", "2026-11-19", "qc")]
    old += [lesson("claude", "https://botanicgardens.org/x", "https://botanicgardens.org/events", "2026-11-12", "qb")]
    f.write_text("\n".join(f"<!-- lesson {json.dumps(x)} -->" for x in old))
    mr.write_lessons(f, [], "2026-11-21")
    rows = [json.loads(m) for m in __import__("re").findall(r"<!-- lesson (\{.*?\}) -->", f.read_text())]
    assert len(rows) == 2, "덴버 미술관 free-days 6건 → 1건"
    dam = next(x for x in rows if "denverartmuseum" in x["monitor_url"])
    assert dam["paths"] == ["claude", "chatgpt"] and dam["date"] == "2026-11-19" and "q4" in dam["queries"]
    assert "Claude·ChatGPT 검색 누락" in f.read_text()
    # Claude 가 보태닉 가든 페이지와 덴버 미술관 행사를 올림 → Claude 누락에서 지움(ChatGPT 누락은 남김)
    subs = [{"key": "botanicgardens.org/x", "start": None, "end": None}, {"key": "denverartmuseum.org/e1", "start": None, "end": None}]
    mr.write_lessons(f, [], "2026-11-21", subs)
    rows = [json.loads(m) for m in __import__("re").findall(r"<!-- lesson (\{.*?\}) -->", f.read_text())]
    assert [x["paths"] for x in rows] == [["chatgpt"]]
