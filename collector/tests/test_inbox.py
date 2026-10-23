"""외부 입력 파일(이슈 #20): 경로·크기 제한, 폴더로 발견 경로 결정, 같은 파일 재처리 방지, 오류 격리."""
import json

import inbox
import external_ingest as xi
from conftest import FakeAPI


def _files(tmp_path, files):
    for p, body in files.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())


def test_inbox_limits_finder_and_dedupe(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(xi, "ingest", lambda raw, api, client, models, today, finder, run_id: seen.append((finder, run_id, raw)) or {"enqueued": 1})
    ok = {"events": [{"title": "A", "url": "https://a.org/x"}]}
    _files(tmp_path, {"external-inbox/chatgpt/r1.json": ok, "external-inbox/claude/r2.json": ok,
                      "external-inbox/chatgpt/big.json": b"x" * (inbox.MAX_BYTES + 1), "external-inbox/chatgpt/bad.json": b"{not json"})
    api = FakeAPI()
    paths = ["external-inbox/chatgpt/r1.json", "external-inbox/claude/r2.json", "external-inbox/chatgpt/big.json", "external-inbox/chatgpt/bad.json",
             "external-inbox/other/x.json", "external-inbox/chatgpt/../../config/models.json", "external-inbox/chatgpt/$(rm -rf).json"]
    res = {r["path"]: r for r in inbox.process(paths, api, None, {}, "2026-10-23", root=tmp_path)}
    assert [s[:2] for s in seen] == [("chatgpt", "chatgpt-r1"), ("claude", "claude-r2")]
    assert res["external-inbox/chatgpt/big.json"]["status"] == "rejected" and "크기" in res["external-inbox/chatgpt/big.json"]["reason"]
    assert res["external-inbox/chatgpt/bad.json"]["status"] == "failed"
    assert all(res[p]["status"] == "rejected" for p in paths[4:6])
    assert res[paths[6][:120]]["status"] == "rejected"
    # 같은 파일·같은 내용으로 다시 실행(워크플로 재실행) → 다시 처리하지 않음
    res2 = {r["path"]: r for r in inbox.process(paths[:2], api, None, {}, "2026-10-23", root=tmp_path)}
    assert len(seen) == 2 and all(r["status"] == "skipped" for r in res2.values())
    # 실패했던 파일은 다시 처리할 수 있음(실패로 기록)
    _files(tmp_path, {"external-inbox/chatgpt/bad.json": ok})
    inbox.process(["external-inbox/chatgpt/bad.json"], api, None, {}, "2026-10-23", root=tmp_path)
    assert seen[-1][:2] == ("chatgpt", "chatgpt-bad")


def test_inbox_caps_file_count(tmp_path, monkeypatch):
    monkeypatch.setattr(xi, "ingest", lambda *a, **k: {"enqueued": 0})
    paths = [f"external-inbox/claude/f{i}.json" for i in range(12)]
    _files(tmp_path, {p: {"events": []} for p in paths})
    res = inbox.process(paths, FakeAPI(), None, {}, "2026-10-23", root=tmp_path)
    assert sum(r["status"] == "done" for r in res) == 10 and sum(r["status"] == "rejected" for r in res) == 2
