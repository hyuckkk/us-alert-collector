"""수집 실행 오케스트레이터. 단계마다 실패를 격리하고(1차 검증 1-6), 수집한 항목은 처리 전까지 Worker 대기열에 보존한다.

python collector/run.py [--mode full|light] [--dry-run] [--no-ai] [--only SOURCE_ID] [--max N] [--out reports/run.json]
  full   하루 1회: 정보원 감시(A) + 키워드 검색 + AI 발굴(B) + 상세 재검사 + 대기열 처리 + 발송
  light  몇 시간마다: 상세 재검사 + 대기열 처리 + 발송 (마감 임박 항목·상한 초과분을 빨리 처리)
  --dry-run  Worker 에 쓰지 않는다(대기열은 메모리로 흉내). AI 는 키가 있으면 호출.

단계: sources → search → discover → watch → queue → ai → enqueue → annual → fallback → dispatch → log
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import sys
import traceback
import zlib
from datetime import date, datetime, timedelta, timezone
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
MIN_SOURCE_OK_RATIO = 0.5  # 정보원 조회 성공률이 이보다 낮으면 '정상 수집'으로 기록하지 않는다(2차 재검증 G)
DETAIL_SOURCE_TYPES = ("ai-discover",)
DETAIL_WORKERS = 8           # 상세 페이지 동시 조회 수(사이트별 부담은 작음: 대부분 서로 다른 기관)
RSS_DETAIL_MIN_CHARS = 400  # 공식 피드 항목 본문이 이보다 짧으면 상세 페이지를 연다
FEED_HAS_COST = re.compile(r"\$\s?\d|\bfree\b|\bcost\b|\bfees?\b|\badmission\b|\bprice\b|no charge", re.I)  # 길어도 비용 정보가 없으면 상세 페이지를 연다
LEASE_EXTEND_MIN = 60       # AI 호출·Worker 등록 직전에 대기열 임대를 이만큼 연장(3차 검증 2)


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
        # 비교 시험 전용(운영 설정 파일은 그대로): MODEL_OVERRIDE="discover=claude-sonnet-5-5,judge=claude-sonnet-5-5"
        for pair in filter(None, os.environ.get("MODEL_OVERRIDE", "").split(",")):
            stage, model = pair.split("=", 1)
            if stage.strip() in self.models and model.strip() in self.models.get("prices", {}):
                self.models[stage.strip()] = {**self.models[stage.strip()], "model": model.strip()}
        self.api = api or WorkerAPI.from_env(args.dry_run)
        self.client = client
        self.usage = None
        self.m: dict = {k: 0 for k in ("sources_total", "sources_ok", "queries", "discover_calls", "entries_fetched", "entries_new", "entries_changed",
                                       "details_ok", "details_total", "watch_checked", "watch_changed", "queue_added", "ai_items", "ai_missing",
                                       "ai_failed", "events_out", "outbox", "input_tokens", "output_tokens", "searches", "queue_queued", "queue_oldest_hours")}
        # GitHub 예약 실행이 얼마나 늦게 시작됐는지(운영 첫날 4.5시간 지연) — 실행 기록에 남겨 감시·보고에 쓴다
        if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and os.environ.get("SCHEDULE_CRON"):
            self.m["schedule_delay_min"] = schedule_delay_min(os.environ["SCHEDULE_CRON"], self.started)
        self.m["usd"] = 0.0
        # 작업량 기록(이슈 #20): 실제 검색어·URL 시도·후보 흐름·단계별 비용. 모르는 값은 "unknown" 으로 남긴다.
        self.work: dict = {"search_queries": [], "discover_queries": [], "discover_result_urls": 0,
                           "cost_usd": {"discover": 0.0, "extract": 0.0, "judge": 0.0, "recommend": 0.0}, "failure_usd": 0.0}
        self.stages: dict = {}
        self.alerts: list[str] = []
        self.report: dict = {"run_id": self.run_id, "mode": args.mode, "today": self.today.isoformat(), "dry_run": self.api.dry_run,
                             "sources": [], "dropped": {}, "events": []}
        self.entries: dict[str, Entry] = {}     # key → 이번 실행에서 모은 항목
        self.trace_ai: list[dict] = []           # 단계별 발견 측정용(--trace)
        self.source_rows: dict[str, dict] = {}  # 정보원별 상태(Worker source_health 로)
        self.official_ids: set[str] = set()
        self.core_due: list[tuple[str, bool]] = []
        self.ai_ok: bool | None = None

    # ------------------------------------------------ 단계 실행기: 한 단계의 실패가 다른 단계를 막지 않는다
    def stage(self, name: str, fn):
        t0 = datetime.now(timezone.utc)
        u0 = self.usage.usd if getattr(self, "usage", None) else 0.0
        self._progress(name, "start")
        try:
            r = fn()
            self.stages[name] = {"ok": True, **(r if isinstance(r, dict) else {})}
        except Exception as e:  # noqa: BLE001
            self.stages[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            self.alerts.append(f"{name} 단계 오류: {type(e).__name__}: {str(e)[:150]}")
            self.report.setdefault("tracebacks", {})[name] = traceback.format_exc()[-1500:]
        self.stages[name]["seconds"] = round((datetime.now(timezone.utc) - t0).total_seconds())
        if getattr(self, "usage", None):  # 단계별 AI 사용액(운영비 추정·최적화 비교용)
            self.stages[name]["usd"] = round(self.usage.usd - u0, 4)
        self._progress(name, "done")

    def _progress(self, name: str, what: str) -> None:
        """단계별 진행 기록(PROGRESS_FILE). 실행이 시간 초과로 끊겨도 어느 단계에서 오래 걸렸는지 남는다."""
        p = os.environ.get("PROGRESS_FILE")
        if p:
            with open(p, "a") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stage": name, "event": what,
                                    "seconds": self.stages.get(name, {}).get("seconds"), "usd": self.stages.get(name, {}).get("usd")}) + "\n")

    def add_entries(self, entries: list[Entry], official: bool, bots=None):
        seen_urls = getattr(self, "_seen_urls", None)
        if seen_urls is None:
            seen_urls = self._seen_urls = {}
        for e in entries:
            e.extra.setdefault("official", official)
            if bots:
                e.extra.setdefault("bots", bots)
            # 같은 페이지를 여러 정보원(목록·페이지 감시·검색·발굴)이 가리키면 한 번만 AI 에 넘긴다(13차: Thornton 접수 안내 중복)
            u = normalize_url(e.url) if "#" not in e.url else None
            if u and u in seen_urls and seen_urls[u] != e.source_id:
                continue
            if u:
                seen_urls.setdefault(u, e.source_id)
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
        due = [s for s in srcs if self.args.only or getattr(self.args, "all_sources", False) or is_due(s, self.today)]
        everything = {x["id"]: x for x in load("sources.json")["sources"]}
        for s in due:
            r = fetch_source(s, secrets=os.environ)
            if not r.ok and s.get("backup") in everything:
                # 주 경로가 안 되면 같은 기관의 예비 공식 경로로(예: Hyland Hills 일정 API 접속 시간 초과 → 같은 사이트의 iCal).
                # 항목은 주 정보원 이름으로 남겨 경로가 바뀌어도 같은 항목으로 본다.
                b = fetch_source(everything[s["backup"]], secrets=os.environ)
                if b.ok:
                    for e in b.entries:
                        e.source_id = s["id"]
                    b.error = f"주 경로 실패({r.error}) → 예비 경로 {s['backup']}"
                    r = b
            self.m["sources_total"] += 1
            self.m["sources_ok"] += int(r.ok)
            row = {"id": s["id"], "ok": r.ok, "status": r.status, "items": len(r.entries), "error": r.error}
            self.report["sources"].append(row)
            self.source_rows[s["id"]] = dict(row)
            if s.get("official"):
                self.official_ids.add(s["id"])
            if s.get("core"):
                self.core_due.append((s["id"], r.ok))
            self.add_entries(r.entries, bool(s.get("official")), s.get("bots"))
            if s["type"] == "html" and s.get("mode") != "page-change":
                for e in r.entries:
                    e.extra["needs_detail"] = True  # 목록 링크뿐이라 상세 본문 없이는 날짜·가격·자격을 알 수 없음
            elif s["type"] in ("rss", "ical", "json") and s.get("official"):
                # 피드 본문이 짧으면(예: 날짜 한 줄) 비용·연령·신청 조건은 상세 페이지에만 있다(10차 시험: 비용 누락)
                for e in r.entries:
                    # 피드 본문이 길어도 비용·시간 정보가 없으면 상세 페이지를 연다(실제 행사 원문 대조: Brighton '$40'·Erie 'Free' 누락)
                    short = len(e.text or "") < RSS_DETAIL_MIN_CHARS or not FEED_HAS_COST.search(e.text or "")
                    if short and e.url.startswith("http"):
                        e.extra["needs_detail"] = e.extra["feed_short"] = True  # 피드 본문은 그대로 둔다(변경 감지 해시 유지)
        if self.m["sources_total"] and self.m["sources_ok"] / self.m["sources_total"] < 0.5:
            self.alerts.append(f"정보원 조회 성공률 {self.m['sources_ok']}/{self.m['sources_total']}")
        return {"due": len(due)}

    # ------------------------------------------------ 키워드 검색(토큰 0)
    def search(self):
        qs = queries_for(load("queries.json"), self.today)
        # 외부 검색 대조로 놓친 행사를 찾을 수 있게 자동으로 더한 검색어(매일)
        try:
            qs += [(q.get("bot") or "any", q["query"]) for q in self.api.queries_extra() if q.get("query")]
        except Exception as e:  # noqa: BLE001
            self.alerts.append(f"추가 검색어 조회 실패: {type(e).__name__}")
        for bot, query in qs:
            r = fetch_source({"id": "gnews:" + query[:40], "type": "gnews", "query": query})
            self.m["queries"] += 1
            self.work["search_queries"].append(query)
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
            if not self.usage.allows(cfg.get("reserve_usd", 1.2)):
                self.alerts.append(f"실행 비용 상한(${self.usage.cap}) — AI 발굴 '{bot}' 이후 생략")
                break
            if ai.CREDIT["out"]:
                break
            u0 = self.usage.usd
            try:
                d = ai.discover(self.client, self.models, theme, known, self.usage, max_searches=cfg["max_searches_deep"] if bot == "deep" else cfg["max_searches"],
                                today=self.today.isoformat())
                self.m["discover_calls"] += 1
                self.work["discover_queries"] += d.get("queries", [])
                self.work["discover_result_urls"] += d.get("result_urls", 0)
                self.work["cost_usd"]["discover"] += self.usage.usd - u0
            except Exception as e:  # noqa: BLE001  분야 하나가 실패해도 나머지 분야는 계속
                self.work["cost_usd"]["discover"] += self.usage.usd - u0
                self.work["failure_usd"] += self.usage.usd - u0
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
            self.add_entries([Entry("ai-discover", u, ev.get("title") or u, ev.get("why") or "", None, {"needs_detail": True})], False, None if bot == "deep" else [bot])
        self.report["discover"] = {"sources": found_sources, "events": [e for _, e in found_events]}
        return {"calls": self.m["discover_calls"], "sources": len(found_sources), "events": len(seen_urls), "errors": errors}

    # ------------------------------------------------ 상세 페이지 재검사(1차 검증 1-3)
    def watch(self):
        """변경된 페이지는 먼저 대기열에 보존하고, 보존에 성공한 경우에만 새 지문을 기록한다(2차 재검증 E).
        한 페이지에 행사가 여럿이면 event_keys 를 모두 넘겨, 재추출 결과를 그 행사들과 대조한다(2차 재검증 F)."""
        due = self.api.watch_due(MAX_WATCH_PER_RUN)
        updates, changed = [], []
        with new_client() as hc:
            for w in due:
                keys = w.get("event_keys") or ([w["event_key"]] if w.get("event_key") else [])
                try:
                    status, text = page_text(hc, w["url"])
                except Exception:  # noqa: BLE001
                    status, text = None, ""
                self.m["watch_checked"] += 1
                if not text:
                    updates.append({"url": w["url"], "ok": False})
                    continue
                fp, basis = fingerprint(text)
                if w.get("fingerprint") and fp != w["fingerprint"]:
                    # 행사 관련 문장이 바뀐 경우만 AI 로(메뉴·장식 변경은 지문이 같아 여기서 걸러짐)
                    self.m["watch_changed"] += 1
                    e = Entry(w.get("source_id") or "watch", w["url"], f"[상세 변경] {', '.join(keys)}", basis, None,
                              {"hint": keys, "official": True, "page_fp": fp, "detail_ok": True})
                    changed.append((f"watch:{w['url']}:{fp}", e, {"url": w["url"], "ok": True, "fingerprint": fp}))
                else:
                    updates.append({"url": w["url"], "ok": True, "fingerprint": fp})
        if changed:
            try:
                self.api.queue_add([self.queue_item(k, e, fp_upd["fingerprint"]) for k, e, fp_upd in changed])
                updates += [u for _, _, u in changed]
            except Exception as e:  # noqa: BLE001  지문을 갱신하지 않으므로 다음 재검사에서 같은 변경을 다시 발견한다
                self.alerts.append(f"상세 변경 {len(changed)}건 대기열 보존 실패: {type(e).__name__} — 다음 재검사에서 다시 감지")
                updates += [{"url": u["url"], "ok": False} for _, _, u in changed]
        self.api.watch_update(updates)
        return {"checked": self.m["watch_checked"], "changed": self.m["watch_changed"]}

    @staticmethod
    def _needs_detail(e: Entry) -> bool:
        return bool(e.extra.get("needs_detail")) and (not e.text or (bool(e.extra.get("feed_short")) and not e.extra.get("detail_ok")))

    @staticmethod
    def _detail(e: Entry, hc) -> bool:
        """상세 본문 조회. 짧은 피드 본문이 있으면 상세 본문 앞에 붙여 둘 다 AI 에 넘긴다."""
        feed = e.text if e.extra.get("feed_short") else ""
        try:
            ok = fetch_detail(e, hc)
        except Exception as err:  # noqa: BLE001  동시 조회 중 한 페이지의 이상한 응답이 실행 전체를 멈추지 않게
            e.extra["detail_error"], ok = type(err).__name__, False
        if ok and feed:
            e.text = (feed + "\n" + e.text)[:5000]
        if not ok and feed:
            e.text = feed
        e.extra["detail_ok"] = ok
        return ok

    def queue_item(self, key: str, e: Entry, hash_: str) -> dict:
        hint = e.extra.get("hint")
        return {"key": key, "hash": hash_, "source_id": e.source_id, "url": e.url, "title": e.title[:300],
                "hint": ",".join(hint) if isinstance(hint, list) else hint, "priority": rules.priority(e, self.today, bool(e.extra.get("official"))),
                "body": {"text": (e.text or "")[:4000], "published": e.published,
                         "extra": {k2: v for k2, v in e.extra.items() if k2 in ("uid", "bots", "official", "location", "detail_error", "needs_detail", "detail_ok", "feed_short", "page_fp", "page_hash")}}}

    # ------------------------------------------------ 대기열 등록(새·바뀐 항목 전부 보존, 1차 검증 3)
    def enqueue_raw(self):
        keys = list(self.entries)
        hashes = {k: (self.entries[k].extra.get("page_fp") or rules.entry_hash(self.entries[k])) for k in keys}
        self.m["entries_fetched"] = len(keys)
        new, changed = self.api.queue_check([{"key": k, "hash": hashes[k]} for k in keys])
        fresh = [k for k in keys if k in new or k in changed]
        self.m["entries_new"], self.m["entries_changed"] = len([k for k in fresh if k in new]), len([k for k in fresh if k in changed])
        # 상세 본문(토큰 0)을 붙인다. 상한을 넘는 것은 본문 없이 넣고, 대기열 처리 때 다시 조회한다.
        need = [k for k in fresh if self._needs_detail(self.entries[k])]
        with new_client() as hc, ThreadPoolExecutor(DETAIL_WORKERS) as ex:
            oks = dict(zip(need[:MAX_DETAIL_FETCH], ex.map(lambda k: self._detail(self.entries[k], hc), need[:MAX_DETAIL_FETCH])))
            for k in need[:MAX_DETAIL_FETCH]:
                self.m["details_total"] += 1
                ok = oks[k]
                self.entries[k].extra["detail_ok"] = ok
                self.m["details_ok"] += int(ok)
                sid = self.entries[k].source_id
                if sid in self.source_rows:
                    self.source_rows[sid]["details_total"] = self.source_rows[sid].get("details_total", 0) + 1
                    self.source_rows[sid]["details_ok"] = self.source_rows[sid].get("details_ok", 0) + int(ok)
        items = [self.queue_item(k, self.entries[k], hashes[k]) for k in fresh]
        self.m["queue_added"] = self.api.queue_add(items)
        return {"fresh": len(fresh)}

    # ------------------------------------------------ AI 단계
    def ai_ready(self) -> bool:
        if self.args.no_ai or not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        if self.usage is None:
            import ai
            self.usage = ai.Usage(self.models["prices"], self.models.get("web_search_usd", 0.01))
            self.usage.cap = getattr(self.args, "usd_cap", None)
            self.client = self.client or ai.get_client()
            # 이슈 #20: 프로그램 자체 월 예산 상한(근거 없는 $150)으로 AI 를 멈추지 않는다. 실제 한계는 API 선불 크레딧 잔액이며,
            # 소진되면 API 가 거부하고(카드·자동 충전 없으면 추가 과금 없음) 그 실행의 AI 단계만 멈춘다(ai.CREDIT).
            self.spent = self.api.month_usd()
        import ai
        return not ai.CREDIT["out"]

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
                e.extra["hint"] = [h for h in str(r["hint"]).split(",") if h]
            if r["source_id"] and (r["source_id"] in DETAIL_SOURCE_TYPES or r["source_id"].startswith("cand-")):
                e.extra["needs_detail"] = True
            entries.append(e)
        # 상세 본문이 필요한데 없는 항목은 AI 전에 반드시 조회한다. 실패하면 AI 로 보내지 않고(추측 금지) 다시 대기(2차 재검증 B).
        no_detail: dict[int, str] = {}
        ready = bool(todo) and self.ai_ready()
        with new_client() as hc, ThreadPoolExecutor(DETAIL_WORKERS) as ex:
            want = [i for i, e in enumerate(entries) if ready and self._needs_detail(e)]  # AI 를 못 쓰면 조회해도 보관하지 않으므로 건너뜀
            oks = dict(zip(want, ex.map(lambda i: self._detail(entries[i], hc), want)))  # 상세 페이지는 동시에 여러 개 조회
            for i, e in enumerate(entries):
                if i in oks:
                    self.m["details_total"] += 1
                    if oks[i]:
                        self.m["details_ok"] += 1
                    elif not e.extra.get("feed_short"):  # 피드 본문이 있는 항목은 그 본문으로 계속 진행
                        no_detail[i] = f"detail-unavailable: {e.extra.get('detail_error', '?')}"
        self.m["detail_missing"] = len(no_detail)
        if todo:
            if not ready:
                # 처리하지 않은 항목은 임대만 풀고 대기열에 그대로 둔다(시도 횟수도 늘리지 않음)
                self.report["ai_skipped"] = "no key / --no-ai / budget"
                results += [{"key": r["key"], "status": "retry", "error": no_detail[i]} if i in no_detail else {"key": r["key"], "status": "release"}
                            for i, r in enumerate(todo)]
            else:
                import ai
                # 상세 조회로 시간이 흘렀으니 AI 호출 전에 임대를 연장한다. 이미 잃은 항목은 다른 실행이 처리하므로 AI 를 부르지 않는다(3차 검증 2)
                lost = self.api.queue_extend([r["key"] for r in todo], LEASE_EXTEND_MIN)
                lost_idx = {i for i, r in enumerate(todo) if r["key"] in lost}
                if lost_idx:
                    self.alerts.append(f"임대를 잃은 대기 항목 {len(lost_idx)}건 — 다른 실행이 처리 중이라 건너뜀")
                send = [i for i in range(len(entries)) if i not in no_detail and i not in lost_idx]
                u0 = self.usage.usd
                out = ai.extract(self.client, self.models, [entries[i] for i in send], self.today.isoformat(), self.usage)
                self.work["cost_usd"]["extract"] += self.usage.usd - u0
                if out.failed and not out.results:  # 결과 없이 쓴 비용 = 실패 비용
                    self.work["failure_usd"] += self.usage.usd - u0
                # 추출 결과의 index 를 원래 entries 기준으로 되돌린다
                out.results = {send[j]: r.model_copy(update={"index": send[j]}) for j, r in out.results.items()}
                out.failed = {**{send[j]: v for j, v in out.failed.items()}, **no_detail}
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
                    # 판정이 빠지거나 실패한 행사는 보내지 않고, 그 항목 전체를 다음 실행에서 다시(2차 재검증 D)
                    u0 = self.usage.usd
                    try:
                        jo = ai.judge(self.client, self.models, [(rules.for_worker(evs[i]), entries[evs[i]["_entry_index"]].text or "") for i in review_idx], self.usage)
                    except Exception as e:  # noqa: BLE001
                        jo = ai.JudgeOutcome(missing={j: type(e).__name__ for j in range(len(review_idx))})
                    self.work["cost_usd"]["judge"] += self.usage.usd - u0
                    if jo.missing and not jo.verdicts:
                        self.work["failure_usd"] += self.usage.usd - u0
                    if jo.missing:
                        self.alerts.append(f"정밀 판정 누락 {len(jo.missing)}건 — 보내지 않고 다음 실행에서 다시")
                    self.m["judge_missing"] = len(jo.missing)
                    held = {evs[review_idx[j]]["_entry_index"] for j in jo.missing}
                    evs, removed = rules.apply_verdicts(evs, review_idx, jo.verdicts)
                    dropped["judge-removed"] = removed
                    evs = [ev for ev in evs if ev["_entry_index"] not in held]
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
                evs, same_n = identity.collapse_same(evs)
                self.report["identity"] = {**identity.assign_keys(evs, index), "collapsed_same": same_n}
                # 상세 재검사에서 온 변경은 그 페이지에 연결된 기존 행사 키로 되돌린다.
                # 행사 하나·키 하나면 그 키, 여럿이면 그 키들 중 같은 행사(제목·날짜)로 대조한 것만(다른 행사에 잘못 붙이지 않음)
                by_key_index = {x["event_key"]: x for x in index}
                for ev in evs:
                    hints = ev.get("_hint") or []
                    if not hints or ev["event_key"] in hints:
                        continue
                    siblings = [x for x in evs if x["_entry_index"] == ev["_entry_index"]]
                    if len(hints) == 1 and len(siblings) == 1:
                        ev["event_key"] = hints[0]
                        continue
                    cand = [by_key_index[h] for h in hints if h in by_key_index and identity.same_event(ev, by_key_index[h], strict_date=len(siblings) > 1)]
                    if len(cand) == 1:
                        ev["event_key"] = cand[0]["event_key"]
                evs, dup = identity.dedupe(evs)
                if dup:
                    self.report["dropped"]["duplicate"] = dup
                # 추천 판정(큰 모델): 모든 발송 후보에 적용. 판정이 빠지거나 실패한 항목은 보내지 않고 다음 실행에서 다시
                if evs:
                    u0 = self.usage.usd
                    try:
                        ro = ai.recommend(self.client, self.models, [(rules.for_worker(ev), entries[ev["_entry_index"]].text or "") for ev in evs],
                                          self.today.isoformat(), self.usage)
                    except Exception as e:  # noqa: BLE001
                        ro = ai.RecommendOutcome(missing={j: type(e).__name__ for j in range(len(evs))})
                    self.work["cost_usd"]["recommend"] += self.usage.usd - u0
                    if ro.missing and not ro.recs:
                        self.work["failure_usd"] += self.usage.usd - u0
                    if ro.missing:
                        self.alerts.append(f"추천 판정 누락 {len(ro.missing)}건 — 보내지 않고 다음 실행에서 다시")
                    self.m["recommend_missing"] = len(ro.missing)
                    held |= {evs[j]["_entry_index"] for j in ro.missing}
                    for j, r in ro.recs.items():
                        ai.apply_recommendation(evs[j], r, entries[evs[j]["_entry_index"]].text or "")
                    evs = [ev for ev in evs if ev["_entry_index"] not in held]
                    for lv in ("push", "interest", "none"):
                        self.m[f"rec_{lv}"] = self.m.get(f"rec_{lv}", 0) + sum(1 for ev in evs if ev.get("rec_level") == lv)
                for ev in evs:
                    sid = ev.get("source_id")
                    if sid in self.source_rows:
                        self.source_rows[sid]["events"] = self.source_rows[sid].get("events", 0) + 1
                by_entry: dict[int, list[dict]] = {}
                for ev in evs:
                    by_entry.setdefault(ev["_entry_index"], []).append(ev)
                for i, r in enumerate(todo):
                    if i in lost_idx:
                        continue
                    if i in out.failed:
                        results.append({"key": r["key"], "status": "retry", "error": out.failed[i]})
                    elif i in held:
                        results.append({"key": r["key"], "status": "retry", "error": "judge-or-recommend-failed"})
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
        # 저장이 거부된 항목(임대를 잃음)의 행사는 지금 가진 실행에 맡기고 여기서는 등록하지 않는다
        rejected = self.api.queue_result(results) or set()
        self.m["lease_rejected"] = len(rejected)
        # 단계별 발견 측정용 기록(9차): 대기열 항목마다 AI 처리 결과(행사 수·실패 사유)
        by_key = {r["key"]: r for r in rows}
        self.trace_ai += [{"url": by_key[x["key"]]["url"], "source_id": by_key[x["key"]].get("source_id"), "status": x["status"],
                           "events": len(x.get("result") or []), "error": x.get("error")} for x in results if x["key"] in by_key]
        keep = [i for i in range(len(events)) if owners.get(i) not in rejected]
        self.pending_events = [events[i] for i in keep]
        self.event_owners = {j: owners[i] for j, i in enumerate(keep) if i in owners}
        return {"taken": len(rows), "events": len(self.pending_events)}

    def enqueue_events(self):
        evs = getattr(self, "pending_events", [])
        if not evs:
            return {"events": 0}
        lost = self.api.queue_extend(sorted(set(self.event_owners.values())), LEASE_EXTEND_MIN)
        if lost:
            keep = [i for i in range(len(evs)) if self.event_owners.get(i) not in lost]
            self.alerts.append(f"등록 직전 임대를 잃은 대기 항목 {len(lost)}건 — 그 행사 {len(evs) - len(keep)}건은 다른 실행이 등록")
            evs = [evs[i] for i in keep]
            self.event_owners = {j: self.event_owners[i] for j, i in enumerate(keep) if i in self.event_owners}
            if not evs:
                return {"events": 0, "lease_lost": len(lost)}
        for ev in evs:  # 3경로 대조용 발견 경로 표시(메시지에는 나오지 않음)
            ev.setdefault("found_by", "github_collector")
            ev.setdefault("run_id", self.run_id)
        try:
            r = self.api.enqueue(evs)
        except Exception:
            # AI 결과는 'extracted' 로 보존돼 있다. 임대만 풀어 다음 실행이 AI 없이 바로 등록하게 한다.
            self.api.queue_result([{"key": k, "status": "release"} for k in sorted(set(self.event_owners.values()))])
            raise
        self.m["events_out"] = len(evs)
        self.m["outbox"] = r.get("outbox", 0)
        self.report["enqueue"] = r
        self.report["events"] = evs
        done = sorted(set(self.event_owners.values()))
        rejected = self.api.queue_result([{"key": k, "status": "done"} for k in done]) or set()
        if rejected:
            # 등록은 끝났다. 다른 실행이 같은 결과를 다시 등록해도 내용 해시·알림 대기열 UNIQUE 로 중복 발송되지 않는다.
            self.alerts.append(f"'done' 보고가 거부된 항목 {len(rejected)}건(임대 만료) — 다음 실행이 같은 결과로 다시 등록(중복 발송 없음)")
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
        import ai
        blocked = ai.CREDIT["out"]
        if not (blocked or outage >= OUTAGE_NOTICE_HOURS):
            return {"needed": False}
        rows = [r for r in self.api.queue_peek(200) if not str(r.get("source_id") or "").startswith(("gnews", "ai-discover"))]
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

    def workload(self) -> dict:
        """실행 하나의 작업량·비용 기록(이슈 #20). 경로는 github_collector. 이 실행에서 알 수 없는 값은 "unknown"."""
        m, w = self.m, self.work
        dropped = self.report.get("dropped") or {}
        by_model = self.usage.by_model if self.usage else {}
        disp = (self.stages.get("dispatch") or {}).get("dispatch") or {}
        return {
            "path": "github_collector", "run_id": self.run_id, "mode": self.args.mode,
            "started_at": self.started.isoformat(timespec="seconds"), "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "schedule_delay_min": _unknown(m.get("schedule_delay_min")),
            "search_requests": m["queries"] + sum(x.get("searches", 0) for x in by_model.values()),
            "news_search_queries": w["search_queries"], "web_search_queries": w["discover_queries"],
            "web_search_requests": sum(x.get("searches", 0) for x in by_model.values()),
            "web_search_result_urls": w["discover_result_urls"],
            "source_urls_attempted": m["sources_total"], "source_urls_ok": m["sources_ok"],
            "official_pages_checked": m["details_total"] + m["watch_checked"], "official_pages_ok": m["details_ok"],
            "candidates_total": len(self.entries), "candidates_new": m["entries_new"], "candidates_changed": m["entries_changed"],
            "queued": m["queue_added"], "ai_processed": m["ai_items"], "ai_failed_retry": m["ai_failed"],
            "dropped": {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in dropped.items()},
            "held_for_retry": m.get("judge_missing", 0) + m.get("recommend_missing", 0),
            "recommended": {lv: m.get(f"rec_{lv}", 0) for lv in ("push", "interest", "none")},
            "submitted": m["events_out"], "outbox_new": m["outbox"],
            "sent_now": _unknown(disp.get("sent") if isinstance(disp, dict) else None),
            "already_found_or_missed": "unknown",  # 다른 경로와의 대조는 Worker event_finds 에서 계산
            "cost_usd": {k: round(v, 4) for k, v in w["cost_usd"].items()},
            "web_search_usd": round(sum(x.get("searches", 0) for x in by_model.values()) * (self.usage.web_search_usd if self.usage else 0.01), 4),
            "total_usd": round(self.usage.usd, 4) if self.usage else 0.0,
            "failure_usd": round(w["failure_usd"], 4),
            "billing": "anthropic_api_prepaid_credit",
            "credit_exhausted": ai_credit_out(),
        }

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
                self.api.add_usage(self.usage.by_model, self.models["limits"].get("credit_reference_usd", 0))
            except Exception as e:  # noqa: BLE001
                self.alerts.append(f"사용량 기록 실패: {type(e).__name__}")
            self.m.update(self.usage.totals())
            self.m["usd"] = round(self.usage.usd, 4)
            self.report["usage"] = self.usage.by_model
        import ai
        if ai.CREDIT["out"]:
            self.alerts.append("API 크레딧 소진 — AI 분석(발굴·추출·판정·추천)만 멈춤. 공식 정보원 수집과 기존 알림 발송은 계속하고, "
                               "AI 대기 항목은 보존합니다. 카드 등록·충전·자동 충전은 소유자 승인 없이 하지 않습니다.")
        self.report["workload"] = self.workload()
        try:
            self.api.workload(self.report["workload"])
        except Exception as e:  # noqa: BLE001  작업량 기록 실패가 수집을 막지 않음
            self.alerts.append(f"작업량 기록 실패: {type(e).__name__}")
        # 프로그램이 끝까지 돈 것과 실제로 정보를 모은 것을 구분한다(2차 재검증 G)
        stages_ok = all(self.stages.get(s, {}).get("ok", True) for s in ("sources", "queue", "ai", "enqueue"))
        ratio = self.m["sources_ok"] / self.m["sources_total"] if self.m["sources_total"] else 0.0
        core_ok = sum(ok for _, ok in self.core_due)
        self.m["sources_ok_ratio"] = round(ratio, 3)
        self.m["core_failed"] = len(self.core_due) - core_ok
        if self.core_due and core_ok < len(self.core_due):
            self.alerts.append("핵심 정보원 실패: " + ", ".join(i for i, ok in self.core_due if not ok))
        collected = full and self.m["sources_total"] > 0 and ratio >= MIN_SOURCE_OK_RATIO and (not self.core_due or core_ok > 0)
        completed = stages_ok and collected
        self.report["collected"] = collected
        self.report.update({"metrics": self.m, "alerts": self.alerts, "stages": self.stages, "completed": completed})
        self.api.run_log({"run_id": self.run_id, "started_at": self.started.isoformat(timespec="seconds"),
                          "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "completed": completed and full,
                          "metrics": self.m, "stages": self.stages, "alerts": self.alerts, "workload": self.report["workload"], "ai": {"ok": self.ai_ok} if self.ai_ok is not None else None,
                          "sources": list(self.source_rows.values())})
        return self.report


def ai_credit_out() -> bool:
    import ai
    return bool(ai.CREDIT["out"])


def _unknown(v):
    return "unknown" if v is None else v


def schedule_delay_min(cron: str, now: datetime) -> int | None:
    """GitHub 예약 실행의 지연(분): 이 cron 이 정한 시각 중 지금 직전 시각과 실제 시작 시각의 차이.
    분·시 필드만 쓰는 예약('30 12 * * *', '40 0,4,8,16,20 * * *', '20 * * * *')을 다룬다. 모르는 형식이면 None."""
    try:
        minute, hour = cron.split()[:2]
        mins = [int(x) for x in minute.split(",")]
        hours = list(range(24)) if hour == "*" else [int(x) for x in hour.split(",")]
    except (ValueError, AttributeError):
        return None
    now = now.astimezone(timezone.utc)
    best = None
    for day in (0, 1):
        base = (now - timedelta(days=day)).replace(second=0, microsecond=0)
        for h in hours:
            for m in mins:
                t = base.replace(hour=h, minute=m)
                if t <= now and (best is None or t > best):
                    best = t
    return None if best is None else int((now - best).total_seconds() // 60)


def main(argv=None, client=None, api: WorkerAPI | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "light"], default="full")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--max", type=int)
    ap.add_argument("--out")
    ap.add_argument("--all-sources", action="store_true", help="조회 주기와 관계없이 모든 활성 정보원을 조회(전체 시험용)")
    ap.add_argument("--usd-cap", type=float, help="이번 실행의 AI 비용 상한(달러). 각 호출의 최악 비용을 미리 더해 넘으면 호출하지 않는다")
    ap.add_argument("--trace", help="단계별 발견 측정용 기록 파일(수집 항목의 출처 단계·AI 처리 결과). collector/eval_stages.py 가 읽는다")
    args = ap.parse_args(argv)
    r = Run(args, client, api)
    report = r.run()
    if args.trace:
        stage = lambda sid: "search" if sid.startswith("gnews") else "discover" if sid.startswith(("ai-discover", "cand-")) else "watch" if sid.startswith("watch") else "sources"  # noqa: E731
        Path(args.trace).parent.mkdir(parents=True, exist_ok=True)
        Path(args.trace).write_text(json.dumps({
            "run_id": report["run_id"],
            "entries": [{"stage": stage(e.source_id or ""), "source_id": e.source_id, "url": e.url, "title": e.title} for e in r.entries.values()],
            "ai": r.trace_ai, "events": report.get("events", []), "dropped": report.get("dropped"),
            "discover_sources": [s.get("url") for s in (report.get("discover") or {}).get("sources", [])],
        }, ensure_ascii=False, indent=1, default=str) + "\n")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n")
    print(json.dumps({"run_id": report["run_id"], "metrics": report["metrics"], "stages": report["stages"], "alerts": report["alerts"]}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    main()
