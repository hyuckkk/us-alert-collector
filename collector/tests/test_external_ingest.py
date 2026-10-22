"""외부 검색 결과 연결: 검사 → 공식 페이지 재확인 → 추출·추천 → 같은 행사는 같은 키(다시 알리지 않음) → Worker 등록."""
import ai
import external_ingest as xi
import fetch
from conftest import FakeAPI, FakeClient, ev


def test_validate_rejects_non_https_and_missing_title_and_caps_count():
    raw = {"events": [{"title": "Jazz Night", "url": "https://example.org/jazz", "description": "x" * 5000, "secret": "drop"},
                      {"title": "Bad", "url": "http://example.org/a"}, {"url": "https://example.org/b"}, "oops"] + [{"title": f"t{i}", "url": f"https://e.org/{i}"} for i in range(40)]}
    ok, bad = xi.validate(raw)
    assert ok[0]["url"] == "https://example.org/jazz" and len(ok[0]["description"]) == 1500 and "secret" not in ok[0]
    assert len(ok) == 1 + 26 and any("https" in b for b in bad) and any("제목" in b for b in bad) and any("초과분" in b for b in bad)


def test_ingest_runs_full_pipeline_and_same_event_is_not_new(monkeypatch):
    monkeypatch.setattr(xi, "fetch_detail", lambda e, c, chars=4000: setattr(e, "text", "Botanic Gardens Blossoms of Light. Nov 27, 2026 5-9pm. $25.") or True)

    class NC:
        def __enter__(self): return None
        def __exit__(self, *a): return False
    monkeypatch.setattr(xi, "new_client", lambda: NC())
    reply = ai.ExtractBatch(results=[ai.EntryResult(index=0, relevant=True, reason="", events=[ev(
        title="Blossoms of Light", title_ko="블로섬 오브 라이트", bot="family", start_date="2026-11-27", url="https://www.botanicgardens.org/blossoms-light",
        org="Denver Botanic Gardens", location="Denver Botanic Gardens", city="Denver")])])
    api = FakeAPI()
    raw = {"events": [{"title": "Blossoms of Light", "url": "https://www.botanicgardens.org/blossoms-light", "audience": "family", "description": "holiday lights"}]}
    rep = xi.ingest(raw, api, FakeClient([reply]), {"extract": {"model": "claude-haiku-5-5", "effort": "low", "batch": 12, "max_tokens": 1000, "text_chars": 4500},
                                                       "prices": {}, "limits": {"monthly_budget_usd": 20}}, "2026-10-22")
    assert rep["enqueued"] == 1 and api.enqueued[0]["source_id"] == "ext-search" and api.enqueued[0]["rec_level"] == "push"
    key = api.enqueued[0]["event_key"]
    # 같은 행사가 다시 들어오면 Worker 의 행사 목록과 대조해 같은 키(새 행사가 아님)
    api.index = [{**api.enqueued[0]}]
    rep2 = xi.ingest(raw, api, FakeClient([reply]), {"extract": {"model": "claude-haiku-5-5", "effort": "low", "batch": 12, "max_tokens": 1000, "text_chars": 4500},
                                                        "prices": {}, "limits": {"monthly_budget_usd": 20}}, "2026-10-22")
    assert rep2["event_keys"] == [key]
