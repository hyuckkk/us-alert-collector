"""수집 실행 오케스트레이터. 단계마다 실패를 격리하고(1차 검증 1-6), 수집한 항목은 처리 전까지 Worker 대기열에 보존한다.

python collector/run.py [--mode full|light] [--dry-run] [--no-ai] [--only SOURCE_ID] [--max N] [--out reports/run.json]
  full   하루 1회: 정보원 감시(A) + 키워드 검색 + AI 발굴(B) + 상세 재검사 + 대기열 처리 + 발송
  light  몇 시간마다: 상세 재검사 + 대기열 처리 + 발송 (마감 임박 항목·상한 초과분을 빨리 처리)
  --dry-run  Worker 에 쓰지 않는다(대기열은 메모리로 흉내). AI 는 키가 있으면 호출.

단계: sources → search → discover → watch → queue → ai → enqueue → annual → fallback → dispatch → log
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import annual  # noqa: E402
import identity  # noqa: E402
import rules  # noqa: E402
from fetch import Entry, fetch_detail, fetch_source, fingerprint, new_client, normalize_url, page_text  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DENVER = ZoneInfo("America/Denver")
MAX_DETAIL_FETCH = 120     # 실행당 새 링크 상세 본문 조회 상한(넘는 것은 다음 실행에서 대기열 처리 전에 조회)
MAX_WATCH_PER_RUN = 120    # 실행당 상세 재검사 상한
OUTAGE_NOTICE_HOURS = 36   # AI 가 이 시간 넘게 막히면 검증 전 정보를 소유자에게
DAILY_THEMES = {           # 세 분야를 매일 발굴(1차 검증 2-1)
    "kids": "kids and teens near Westminster/Denver: after-school programs, classes, camps (incl. registration openings), youth sports leagues, library and museum kids programs",
    "family": "family outings near Denver: festivals, free days, school/university games and performances, nature and park programs, special experiences, notable family travel deals from Denver",
    "life": "adults near Westminster/Denver: hobby and skills classes, community college noncredit courses, rec-center adult sports leagues, clubs, talks, performances",
}
DEEP_THEMES = {            # 요일별 심층(월=0) — 설계안 유지
    0: "kids after-school programs, camps, youth sports and arts/science classes",
    1: "adult hobby and skills classes (crafts, cooking, tech, language exchange, sports leagues)",
    2: "Westminster / Adams County local government, schools (Adams 12, Westminster Public Schools) and community events",
    3: "aviation, military and special facilities open to the public (air shows, base open houses, Air Force Academy, space)",
    4: "nature, state parks and national parks (ranger programs, reservations, seasonal openings)",
    5: "universities near Denver (DU, CU Boulder, CSU, Mines): public lectures, games, family days",
    6: "family travel from Denver: unusual flight deals, cruise family programs and kids-sail-free promotions, road-trip festivals",
}
OFFICIAL_TLD = (".gov", ".edu", ".us", ".org", ".mil")


def load(name: str) -> dict:
    return json.loads((ROOT / "config" / name).read_text())


def is_due(source: dict, today: date) -> bool:
    n = max(int(source.get("every_days", 1)), 1)
    return (today.toordinal() + zlib.crc32(source["id"].encode())) % n == 0


def queries_for(q: dict, today: date) -> list[tuple[str, str]]:
    """(분야, 검색어). daily 는 분야별 목록, weekday 는 요일별 심층."""
    daily = q["daily"]
    out = [(bot, s) for bot, lst in daily.items() for s in lst] if isinstance(daily, dict) else [("any", s) for s in daily]
    out += [("deep", s) for s in q.get("weekday", {}).get(str(today.weekday()), [])]
    return list(dict.fromkeys(out))


class Run:
    def __init__(self, args, client=None, api: WorkerAPI | None = None):
        self.args = args
        self.started = datetime.now(timezone.utc)
        self.run_id = self.started.strftime("%Y%m%dT%H%M%SZ") + ("L" if args.mode == "light" else "")
        self.today = datetime.now(DENVER).date()
        self.checked_at = self.started.isoformat(timespec="seconds")
        self.models = load("models.json")
        self.api = api or WorkerAPI.from_env(args.dry_run)
        self.client = client
        self.usage = None
        self.m: dict = {k: 0 for k in ("sources_total", "sources_ok", "queries", "discover_calls", "entries_fetched", "entries_new", "entries_changed",
                                       "details_ok", "details_total", "watch_checked", "watch_changed", "queue_added", "ai_items", "ai_missing",
                                       "ai_failed", "events_out", "outbox", "input_tokens", "output_tokens", "searches", "queue_queued", "queue_oldest_hours")}
        self.m["usd"] = 0.0
        self.stages: dict = {}
        self.alerts: list[str] = []
        self.report: dict = {"run_id": self.run_id, "mode": args.mode, "today": self.today.isoformat(), "dry_run": self.api.dry_run,
                             "sources": [], "dropped": {}, "events": []}
        self.entries: dict[str, Entry] = {}     # key → 이번 실행에서 모은 항목
        self.source_rows: dict[str, dict] = {}  # 정보원별 상태(Worker source_health 로)
        self.official_ids: set[str] = set()
        self.ai_ok: bool | None = None

    # ------------------------------------------------ 단계 실행기: 한 단계의 실패가 다른 단계를 막지 않는다
    def stage(self, name: str, fn):
        try:
            r = fn()
            self.stages[name] = {"ok": True, **(r if isinstance(r, dict) else {})}
        except Exception as e:  # noqa: BLE001
            self.stages[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            self.alerts.append(f"{name} 단계 오류: {type(e).__name__}: {str(e)[:150]}")
            self.report.setdefault("tracebacks", {})[name] = traceback.format_exc()[-1500:]

    def add_entries(self, entries: list[Entry], official: bool, bots=None):
        for e in entries:
            e.extra.setdefault("official", official)
            if bots:
                e.extra.setdefault("bots", bots)
            self.entries.setdefault(rules.entry_key(e), e)

    # ------------------------------------------------ A. 정보원 감시
    def sources(self):
        srcs = [s for s in load("sources.json")["sources"] if s.get("enabled", True)]
        try:
            srcs += self.api.sources_extra()
        except Exception as e:  # noqa: BLE001
            self.alerts.append(f"승격 정보원 조회 실패: {e}")
        self.all_sources = srcs
        if self.args.only:
            srcs = [s for s in srcs if s["id"] == self.args.only]
        due = [s for s in srcs if self.args.only or is_due(s, self.today)]
        self.html_link_sources = {s["id"] for s in due if s["type"] == "html" and s.get("mode") != "page-change"}
        for s in due:
            r = fetch_source(s, secrets=os.environ)
            self.m["sources_total"] += 1
            self.m["sources_ok"] += int(r.ok)
            row = {"id": s["id"], "ok": r.ok, "status": r.status, "items": len(r.entries), "error": r.error}
            self.report["sources"].append(row)
            self.source_rows[s["id"]] = dict(row)
            if s.get("official"):
                self.official_ids.add(s["id"])
            self.add_entries(r.entries, bool(s.get("official")), s.get("bots"))
        if self.m["sources_total"] and self.m["sources_ok"] / self.m["sources_total"] < 0.5:
            self.alerts.append(f"정보원 조회 성공률 {self.m['sources_ok']}/{self.m['sources_total']}")
        return {"due": len(due)}

    # ------------------------------------------------ 키워드 검색(토큰 0)
    def search(self):
        qs = queries_for(load("queries.json"), self.today)
        for bot, query in qs:
            r = fetch_source({"id": "gnews:" + query[:40], "type": "gnews", "query": query})
            self.m["queries"] += 1
            self.add_entries(r.entries[:15], False, None if bot in ("any", "deep") else [bot])
        return {"queries": len(qs)}

    # ------------------------------------------------ B. AI 발굴(세 분야 매일 + 요일 심층)
    def discover(self):
        cfg = self.models["discover"]
        if not cfg.get("enabled") or not self.ai_ready():
            return {"skipped": True}
        import ai
        known = [s["url"] for s in getattr(self, "all_sources", [])]
        themes = ([(b, t) for b, t in DAILY_THEMES.items()] if cfg.get("daily_per_bot", True) else []) + [("deep", DEEP_THEMES[self.today.weekday()])]
        found_sources, found_events, errors = [], [], 0
        for bot, theme in themes:
            try:
                d = ai.discover(self.client, self.models, theme, known, self.usage, max_searches=cfg["max_searches_deep"] if bot == "deep" else cfg["max_searches"])
                self.m["discover_calls"] += 1
            except Exception as e:  # noqa: BLE001  분야 하나가 실패해도 나머지 분야는 계속
                errors += 1
                self.alerts.append(f"AI 발굴({bot}) 오류: {type(e).__name__}")
                continue
            for s in d["sources"]:
                if s["url"] not in known:
                    known.append(s["url"])
                    found_sources.append({**s, "bots": s.get("bots") or ([bot] if bot != "deep" else [])})
            found_events += [(bot, e) for e in d["events"]]
        self.api.candidates(found_sources)
        seen_urls = set()
        for bot, ev in found_events[:30]:
            u = normalize_url(ev["url"])
            if u in seen_urls:
                continue
            seen_urls.add(u)
            self.add_entries([Entry("ai-discover", u, ev.get("title") or u, ev.get("why") or "")], False, None if bot == "deep" else [bot])
        self.report["discover"] = {"sources": found_sources, "events": [e for _, e in found_events]}
        return {"calls": self.m["discover_calls"], "sources": len(found_sources), "events": len(seen_urls), "errors": errors}

    # ------------------------------------------------ 상세 페이지 재검사(1차 검증 1-3)
    def watch(self):
        due = self.api.watch_due(MAX_WATCH_PER_RUN)
        updates = []
        with new_client() as hc:
            for w in due:
                try:
                    status, text = page_text(hc, w["url"])
                except Exception:  # noqa: BLE001
                    status, text = None, ""
                self.m["watch_checked"] += 1
                if not text:
                    updates.append({"url": w["url"], "ok": False})
                    continue
                fp, basis = fingerprint(text)
                updates.append({"url": w["url"], "ok": True, "fingerprint": fp})
                if w.get("fingerprint") and fp != w["fingerprint"]:
                    # 행사 관련 문장이 바뀐 경우만 AI 로(메뉴·장식 변경은 지문이 같아 여기서 걸러짐)
                    self.m["watch_changed"] += 1
                    e = Entry(w.get("source_id") or "watch", w["url"], f"[상세 변경] {w['event_key']}", basis, None,
                              {"hint": w["event_key"], "official": True, "page_fp": fp})
                    self.entries.setdefault(f"watch:{w['url']}:{fp}", e)
        self.api.watch_update(updates)
        return {"checked": self.m["watch_checked"], "changed": self.m["watch_changed"]}

    # ------------------------------------------------ 대기열 등록(새·바뀐 항목 전부 보존, 1차 검증 3)
    def enqueue_raw(self):
        keys = list(self.entries)
        hashes = {k: (self.entries[k].extra.get("page_fp") or rules.entry_hash(self.entries[k])) for k in keys}
        self.m["entries_fetched"] = len(keys)
        new, changed = self.api.queue_check([{"key": k, "hash": hashes[k]} for k in keys])
        fresh = [k for k in keys if k in new or k in changed]
        self.m["entries_new"], self.m["entries_changed"] = len([k for k in fresh if k in new]), len([k for k in fresh if k in changed])
        # 상세 본문(토큰 0)을 붙인다. 상한을 넘는 것은 본문 없이 넣고, 대기열 처리 때 다시 조회한다.
        need = [k for k in fresh if self.entries[k].source_id in getattr(self, "html_link_sources", set()) or self.entries[k].source_id == "ai-discover"]
        need = [k for k in need if not self.entries[k].text]
        with new_client() as hc:
            for k in need[:MAX_DETAIL_FETCH]:
                self.m["details_total"] += 1
                ok = fetch_detail(self.entries[k], hc)
                self.m["details_ok"] += int(ok)
                sid = self.entries[k].source_id
                if sid in self.source_rows:
                    self.source_rows[sid]["details_total"] = self.source_rows[sid].get("details_total", 0) + 1
                    self.source_rows[sid]["details_ok"] = self.source_rows[sid].get("details_ok", 0) + int(ok)
        items = []
        for k in fresh:
            e = self.entries[k]
            items.append({"key": k, "hash": hashes[k], "source_id": e.source_id, "url": e.url, "title": e.title[:300],
                          "hint": e.extra.get("hint"), "priority": rules.priority(e, self.today, bool(e.extra.get("official"))),
                          "body": {"text": (e.text or "")[:4000], "published": e.published,
                                   "extra": {k2: v for k2, v in e.extra.items() if k2 in ("uid", "bots", "official", "location", "hint", "detail_error")}}})
        self.m["queue_added"] = self.api.queue_add(items)
        return {"fresh": len(fresh)}

    # ------------------------------------------------ AI 단계
    def ai_ready(self) -> bool:
        if self.args.no_ai or not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        if self.usage is None:
            import ai
            self.usage = ai.Usage(self.models["prices"], self.models.get("web_search_usd", 0.01))
            self.client = self.client or ai.get_client()
            budget = self.models["limits"]["monthly_budget_usd"]
            self.spent = self.api.month_usd()
            self.budget_blocked = self.spent >= budget
            if self.budget_blocked:
                self.alerts.append(f"이번 달 AI 사용액 ${self.spent:.2f} ≥ 예산 ${budget} — AI 단계를 멈추고 수집 항목은 대기열에 보존(예산 상향은 승인 필요)")
        return not self.budget_blocked

    def process_queue(self):
        cap = self.args.max or self.models["limits"]["max_ai_items_per_run"]
        rows = self.api.queue_take(cap)
        self.report["queue_taken"] = len(rows)
        if not rows:
            return {"taken": 0}
        results: list[dict] = []      # queue_result 로 보낼 것
        events: list[dict] = []
        owners: dict[int, str] = {}   # events 위치 → 대기열 key
        # 1) 이미 AI 를 거쳤지만 Worker 등록 전에 멈춘 항목: AI 를 다시 부르지 않는다
        for r in rows:
            if r["status"] == "extracted" and r.get("result"):
                for ev in r["result"]:
                    owners[len(events)] = r["key"]
                    events.append(ev)
        todo = [r for r in rows if r["status"] == "queued"]
        entries = []
        for r in todo:
            b = r.get("body") or {}
            e = Entry(r["source_id"] or "?", r["url"], r.get("title") or r["url"], b.get("text") or "", b.get("published"), dict(b.get("extra") or {}))
            if r.get("hint"):
                e.extra["hint"] = r["hint"]
            entries.append(e)
        # 본문이 비어 있는 html 링크는 지금 조회
        with new_client() as hc:
            for e in entries:
                if not e.text and (e.source_id in getattr(self, "html_link_sources", set()) or e.source_id == "ai-discover" or e.source_id.startswith("cand-")):
                    fetch_detail(e, hc)
        if todo:
            if not self.ai_ready():
                # 처리하지 않은 항목은 대기열에 그대로 둔다(시도 횟수도 늘리지 않음)
                self.report["ai_skipped"] = "no key / --no-ai / budget"
            else:
                import ai
                out = ai.extract(self.client, self.models, entries, self.today.isoformat(), self.usage)
                self.m["ai_items"] = len(out.results)
                self.m["ai_missing"] = sum(1 for v in out.failed.values() if v == "missing-in-output")
                self.m["ai_failed"] = len(out.failed)
                self.ai_ok = bool(out.results) or not out.failed
                if out.stopped:
                    self.alerts.append(f"AI 추출 중단({out.stopped}) — 남은 {len(out.failed)}건은 대기열에 보존")
                extracted = [out.results[i] for i in sorted(out.results)]
                evs, dropped = rules.finalize(extracted, entries, self.today, self.checked_at)
                self.report["dropped"] = dropped
                # 애매한 건만 큰 모델(실패하면 그 행사들만 다음 실행으로)
                review_idx = [i for i, ev in enumerate(evs) if ev.get("needs_review")]
                held: set[int] = set()
                if review_idx:
                    try:
                        verdicts = ai.judge(self.client, self.models, [(rules.for_worker(evs[i]), entries[evs[i]["_entry_index"]].text or "") for i in review_idx], self.usage)
                        evs, removed = rules.apply_verdicts(evs, review_idx, verdicts)
                        dropped["judge-removed"] = removed
                    except Exception as e:  # noqa: BLE001
                        self.alerts.append(f"정밀 판정 오류: {type(e).__name__} — 해당 {len(review_idx)}건은 다음 실행에서 다시")
                        held = {evs[i]["_entry_index"] for i in review_idx}
                        evs = [ev for i, ev in enumerate(evs) if i not in set(review_idx)]
                # 같은 행사 판별: Worker 의 최근 행사 목록과 대조
                try:
                    index = self.api.event_index()
                except Exception as e:  # noqa: BLE001
                    index = []
                    self.alerts.append(f"행사 목록 조회 실패(새 키로 처리): {type(e).__name__}")
                for ev in evs:
                    hint = entries[ev["_entry_index"]].extra.get("hint")
                    if hint:
                        ev["_hint"] = hint
                self.report["identity"] = identity.assign_keys(evs, index)
                for ev in evs:  # 상세 재검사에서 온 변경은 원래 행사 키를 유지
                    if ev.get("_hint") and len([x for x in evs if x["_entry_index"] == ev["_entry_index"]]) == 1:
                        ev["event_key"] = ev["_hint"]
                evs, dup = identity.dedupe(evs)
                if dup:
                    self.report["dropped"]["duplicate"] = dup
                for ev in evs:
                    sid = ev.get("source_id")
                    if sid in self.source_rows:
                        self.source_rows[sid]["events"] = self.source_rows[sid].get("events", 0) + 1
                by_entry: dict[int, list[dict]] = {}
                for ev in evs:
                    by_entry.setdefault(ev["_entry_index"], []).append(ev)
                for i, r in enumerate(todo):
                    if i in out.failed:
                        results.append({"key": r["key"], "status": "retry", "error": out.failed[i]})
                    elif i in held:
                        results.append({"key": r["key"], "status": "retry", "error": "judge-failed"})
                    else:
                        mine = [rules.for_worker(ev) for ev in by_entry.get(i, [])]
                        if mine:
                            results.append({"key": r["key"], "status": "extracted", "result": mine})
                            for ev in mine:
                                owners[len(events)] = r["key"]
                                events.append(ev)
                        else:
                            results.append({"key": r["key"], "status": "done"})
        # 2) AI 결과를 먼저 'extracted' 로 저장 → Worker 등록 → 성공한 항목만 'done'
        self.api.queue_result([x for x in results if x["status"] in ("extracted", "retry", "done")])
        self.pending_events = events
        self.event_owners = owners
        return {"taken": len(rows), "events": len(self.pending_events)}

    def enqueue_events(self):
        evs = getattr(self, "pending_events", [])
        if not evs:
            return {"events": 0}
        r = self.api.enqueue(evs)
        self.m["events_out"] = len(evs)
        self.m["outbox"] = r.get("outbox", 0)
        self.report["enqueue"] = r
        self.report["events"] = evs
        done = sorted(set(self.event_owners.values()))
        self.api.queue_result([{"key": k, "status": "done"} for k in done])
        return {"events": len(evs), "done": len(done)}

    def annual(self):
        evs = annual.due_items(load("annual.json"), self.today, self.checked_at)
        if evs:
            self.api.enqueue(evs)
        return {"events": len(evs)}

    # ------------------------------------------------ AI 장기 중단 시 검증 전 정보(소유자만, 1차 검증 4)
    def fallback(self):
        h = self.api.ai_health()
        since = h.get("failing_since")
        outage = (datetime.now(timezone.utc) - datetime.fromisoformat(since.replace("Z", "+00:00"))).total_seconds() / 3600 if since else 0
        blocked = getattr(self, "budget_blocked", False)
        if not (blocked or outage >= OUTAGE_NOTICE_HOURS):
            return {"needed": False}
        rows = [r for r in self.api.queue_take(200) if r["status"] == "queued" and not str(r.get("source_id") or "").startswith(("gnews", "ai-discover"))]
        items = [{"title": r.get("title"), "url": r["url"], "source_id": r.get("source_id")} for r in rows[:40]]
        res = self.api.unverified(items, int(outage) if outage else 0, len(rows))
        return {"needed": True, "items": len(items), "sent": res.get("sent")}

    def verify_candidates(self):
        todo = self.api.candidates_unverified()
        hosts = {identity._host(s["url"]): s["id"] for s in getattr(self, "all_sources", [])}
        results = []
        for c in todo:
            r = fetch_source({"id": "cand", "type": "html", "url": c["url"]})
            text = r.snippet
            host = identity._host(c["url"])
            results.append({"id": c["id"], "verified": {
                "fetchable": r.ok, "status": r.status, "eventish": len(r.entries),
                "future_dates": sum(rules.near_dates(e.title, self.today, 120) for e in r.entries),
                "official": host.endswith(OFFICIAL_TLD), "duplicate_of": hosts.get(host), "type": "html"}})
        self.api.candidates_verify(results)
        return {"verified": len(results)}

    def dispatch(self):
        return {"dispatch": self.api.dispatch(), "digest": self.api.digest()}

    # ------------------------------------------------ 전체
    def run(self) -> dict:
        full = self.args.mode == "full"
        if full:
            self.stage("sources", self.sources)
            if self.args.only is None:
                self.stage("search", self.search)
                self.stage("discover", self.discover)
        self.stage("watch", self.watch)
        self.stage("queue", self.enqueue_raw)
        self.stage("ai", self.process_queue)
        self.stage("enqueue", self.enqueue_events)
        if full:
            self.stage("annual", self.annual)
            self.stage("candidates", self.verify_candidates)
        self.stage("fallback", self.fallback)
        self.stage("dispatch", self.dispatch)
        try:
            qs = self.api.queue_stats()
            self.m["queue_queued"], self.m["queue_oldest_hours"] = qs.get("queued", 0), qs.get("oldest_queued_hours", 0)
            self.report["queue"] = qs
        except Exception:  # noqa: BLE001
            pass
        if self.usage:
            try:
                self.api.add_usage(self.usage.by_model, self.models["limits"]["monthly_budget_usd"])
            except Exception as e:  # noqa: BLE001
                self.alerts.append(f"사용량 기록 실패: {type(e).__name__}")
            self.m.update(self.usage.totals())
            self.m["usd"] = round(self.usage.usd, 4)
            self.report["usage"] = self.usage.by_model
        completed = all(self.stages.get(s, {}).get("ok", True) for s in ("sources", "queue", "ai", "enqueue"))
        self.report.update({"metrics": self.m, "alerts": self.alerts, "stages": self.stages, "completed": completed})
        self.api.run_log({"run_id": self.run_id, "started_at": self.started.isoformat(timespec="seconds"),
                          "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "completed": completed and full,
                          "metrics": self.m, "stages": self.stages, "alerts": self.alerts, "ai": {"ok": self.ai_ok} if self.ai_ok is not None else None,
                          "sources": list(self.source_rows.values())})
        return self.report


def main(argv=None, client=None, api: WorkerAPI | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "light"], default="full")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--max", type=int)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    report = Run(args, client, api).run()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n")
    print(json.dumps({"run_id": report["run_id"], "metrics": report["metrics"], "stages": report["stages"], "alerts": report["alerts"]}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    main()
