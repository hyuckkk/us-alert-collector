"""us-alert-worker 관리 경로 호출.

dry-run 이면 Worker 에 아무것도 쓰지 않는다. 대기열·행사 목록은 메모리로 흉내 내서 실행 흐름 전체를 시험할 수 있게 한다.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

UA = "us-alert-collector/run"


class WorkerAPI:
    def __init__(self, url: str | None, key: str | None, dry_run: bool = False, client: httpx.Client | None = None):
        self.dry_run = dry_run or not (url and key)
        self.url = (url or "").rstrip("/")
        self.client = client or httpx.Client(timeout=60, headers={"authorization": f"Bearer {key}", "user-agent": UA})
        self._mem: dict[str, dict] = {}  # dry-run 대기열

    @classmethod
    def from_env(cls, dry_run: bool = False) -> "WorkerAPI":
        return cls(os.environ.get("US_WORKER_URL"), os.environ.get("US_ADMIN_KEY"), dry_run)

    def _post(self, path: str, body: dict) -> dict:
        r = self.client.post(f"{self.url}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str) -> dict:
        r = self.client.get(f"{self.url}{path}")
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------ 처리 대기열
    def queue_check(self, items: list[dict]) -> tuple[set, set]:
        if self.dry_run:
            new = {x["key"] for x in items if x["key"] not in self._mem}
            changed = {x["key"] for x in items if x["key"] in self._mem and self._mem[x["key"]]["hash"] != x["hash"]}
            return new, changed
        new, changed = set(), set()
        for i in range(0, len(items), 400):
            r = self._post("/admin/queue", {"op": "check", "items": [{"key": x["key"], "hash": x["hash"]} for x in items[i:i + 400]]})
            new.update(r["new"])
            changed.update(r["changed"])
        return new, changed

    def queue_add(self, items: list[dict]) -> int:
        if self.dry_run:
            now = datetime.now(timezone.utc).isoformat()
            for x in items:
                self._mem[x["key"]] = {**x, "status": "queued", "attempts": 0, "result": None, "first_seen": now, "lease_until": None}
            return len(items)
        n = 0
        for i in range(0, len(items), 25):
            n += self._post("/admin/queue", {"op": "add", "items": items[i:i + 25]}).get("written", 0)
        return n

    def queue_take(self, limit: int, lease_minutes: int = 30) -> list[dict]:
        """대기열에서 최대 limit 건을 임대해 가져온다. Worker 는 임대 중인 항목을 다시 주지 않으므로 60건씩 반복해 끝까지 받는다.
        서버가 같은 항목을 되풀이하면(임대 미지원) 조용히 멈추지 않고 오류로 알린다(2차 재검증 A)."""
        if self.dry_run:
            now = datetime.now(timezone.utc)
            rows = [r for r in self._mem.values() if r["status"] in ("queued", "extracted") and not (r.get("lease_until") and r["lease_until"] > now)]
            rows.sort(key=lambda r: (r["status"] != "extracted", -r.get("priority", 0), r["first_seen"]))
            rows = rows[:limit]
            for r in rows:
                r["lease_until"] = now + timedelta(minutes=lease_minutes)
            return rows
        out: list[dict] = []
        seen: set[str] = set()
        while len(out) < limit:
            got = self._post("/admin/queue", {"op": "take", "limit": min(60, limit - len(out)), "lease_minutes": lease_minutes})["items"]
            if not got:
                break
            dup = [g["key"] for g in got if g["key"] in seen]
            if dup:
                raise RuntimeError(f"queue take returned {len(dup)} items already taken — Worker must lease taken items")
            seen.update(g["key"] for g in got)
            out += got
        return out

    def queue_peek(self, limit: int) -> list[dict]:
        """임대 없이 대기 항목 보기(검증 전 정보 안내용)."""
        if self.dry_run:
            rows = [r for r in self._mem.values() if r["status"] == "queued"]
            rows.sort(key=lambda r: (-r.get("priority", 0), r["first_seen"]))
            return rows[:limit]
        return self._post("/admin/queue", {"op": "peek", "limit": limit})["items"]

    def queue_result(self, items: list[dict]) -> None:
        if self.dry_run:
            for x in items:
                r = self._mem.get(x["key"])
                if r is None:
                    continue
                r["lease_until"] = None
                if x["status"] == "release":
                    continue
                if x.get("error"):
                    r["last_error"] = x["error"]
                if x["status"] == "retry":
                    r["attempts"] += 1
                    r["status"] = "stuck" if r["attempts"] >= 6 else "queued"
                else:
                    r["status"], r["result"] = x["status"], x.get("result")
            return
        for i in range(0, len(items), 100):
            self._post("/admin/queue", {"op": "result", "items": items[i:i + 100]})

    def queue_stats(self) -> dict:
        if self.dry_run:
            st = [r["status"] for r in self._mem.values()]
            return {"queued": st.count("queued"), "extracted": st.count("extracted"), "stuck": st.count("stuck"), "oldest_queued_hours": 0}
        return self._post("/admin/queue", {"op": "stats"})

    # ------------------------------------------------ 행사·감시·상태
    def event_index(self) -> list[dict]:
        return [] if self.dry_run else self._get("/admin/event-index").get("events", [])

    def enqueue(self, events: list[dict]) -> dict:
        tot: dict = {"new": 0, "changed": 0, "same": 0, "outbox": 0, "accepted": 0, "watch": 0, "skipped": {}}
        if self.dry_run:
            return {**tot, "dry_run": True, "accepted": len(events)}
        for i in range(0, len(events), 10):
            r = self._post("/admin/enqueue", {"events": events[i:i + 10]})
            for k in ("new", "changed", "same", "outbox", "accepted", "watch"):
                tot[k] += r.get(k, 0)
            for k, v in r.get("skipped", {}).items():
                tot["skipped"][k] = tot["skipped"].get(k, 0) + v
        return tot

    def watch_due(self, limit: int) -> list[dict]:
        return [] if self.dry_run else self._post("/admin/watch", {"op": "due", "limit": limit}).get("items", [])

    def watch_update(self, items: list[dict]) -> None:
        if not self.dry_run and items:
            self._post("/admin/watch", {"op": "update", "items": items})

    def ai_health(self) -> dict:
        return {} if self.dry_run else self._get("/admin/ai-health")

    def unverified(self, items: list[dict], outage_hours: int, pending: int) -> dict:
        return {"dry_run": True} if self.dry_run else self._post("/admin/unverified", {"items": items, "outage_hours": outage_hours, "pending": pending})

    def dispatch(self) -> dict:
        if self.dry_run:
            return {"dry_run": True}
        tot = {"sent": 0, "failed": 0, "deferred": 0, "uncertain": 0, "expired": 0}
        for _ in range(5):
            r = self._post("/admin/dispatch", {})
            for k in tot:
                tot[k] += r.get(k, 0)
            if r.get("sent", 0) + r.get("failed", 0) + r.get("uncertain", 0) == 0:
                break
        return tot

    def digest(self) -> dict:
        return {"dry_run": True} if self.dry_run else self._post("/admin/digest", {})

    def month_usd(self) -> float:
        return 0.0 if self.dry_run else float(self._get("/admin/usage").get("usd", 0))

    def add_usage(self, by_model: dict, budget: float) -> None:
        if self.dry_run:
            return
        keys = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "usd", "calls", "searches", "errors")
        for model, m in by_model.items():
            self._post("/admin/usage", {"model": model, "budget": budget, **{k: m.get(k, 0) for k in keys}})

    def sources_extra(self) -> list[dict]:
        return [] if self.dry_run else self._get("/admin/sources-extra").get("sources", [])

    def candidates(self, cands: list[dict]) -> dict:
        return {"dry_run": True} if self.dry_run or not cands else self._post("/admin/candidates", {"candidates": cands})

    def candidates_unverified(self) -> list[dict]:
        return [] if self.dry_run else self._post("/admin/candidates", {"op": "unverified"}).get("items", [])

    def candidates_verify(self, results: list[dict]) -> dict:
        return {"dry_run": True} if self.dry_run or not results else self._post("/admin/candidates", {"op": "verify", "results": results})

    def run_log(self, body: dict) -> dict:
        return {"dry_run": True} if self.dry_run else self._post("/admin/run-log", body)
