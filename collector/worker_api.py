"""us-alert-worker 관리 경로 호출.

dry-run 이면 Worker 에 아무것도 쓰지 않는다. 대기열·행사 목록은 메모리로 흉내 내서 실행 흐름 전체를 시험할 수 있게 한다.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx

UA = "us-alert-collector/run"


class WorkerAPI:
    def __init__(self, url: str | None, key: str | None, dry_run: bool = False, client: httpx.Client | None = None):
        self.dry_run = dry_run or not (url and key)
        self.url = (url or "").rstrip("/")
        self.client = client or httpx.Client(timeout=60, headers={"authorization": f"Bearer {key}", "user-agent": UA})
        self._mem: dict[str, dict] = {}  # dry-run 대기열
        self._leases: dict[str, dict] = {}  # 이 실행이 꺼낸 항목의 {lease_id, hash}. 결과 보고 때 함께 보내 소유권을 증명한다(3차 검증 2)

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
                old = self._mem.get(x["key"], {})
                self._mem[x["key"]] = {**x, "status": "queued", "attempts": 0, "result": None, "first_seen": old.get("first_seen", now),
                                       "lease_until": None, "lease_id": None}
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
            lease_id = uuid.uuid4().hex
            for r in rows:
                r["lease_until"] = now + timedelta(minutes=lease_minutes)
                r["lease_id"] = lease_id
                self._leases[r["key"]] = {"lease_id": lease_id, "hash": r["hash"]}
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
            for g in got:
                self._leases[g["key"]] = {"lease_id": g.get("lease_id"), "hash": g.get("hash")}
            out += got
        return out

    def queue_extend(self, keys: list[str], minutes: int = 60) -> set[str]:
        """처리가 길어질 때 자기 임대를 연장. 이미 잃은(만료 후 다른 실행이 가져간) 항목 key 를 돌려준다 — 그 항목은 처리하지 않는다."""
        mine = [{"key": k, "lease_id": self._leases[k]["lease_id"]} for k in dict.fromkeys(keys) if k in self._leases]
        lost = {k for k in keys if k not in self._leases}
        if self.dry_run:
            now = datetime.now(timezone.utc)
            for x in mine:
                r = self._mem.get(x["key"])
                if r and r.get("lease_id") == x["lease_id"] and r.get("lease_until") and r["lease_until"] >= now:
                    r["lease_until"] = now + timedelta(minutes=minutes)
                else:
                    lost.add(x["key"])
        else:
            for i in range(0, len(mine), 400):
                lost.update(self._post("/admin/queue", {"op": "extend", "minutes": minutes, "items": mine[i:i + 400]}).get("lost", []))
        for k in lost:
            self._leases.pop(k, None)
        return lost

    def queue_peek(self, limit: int) -> list[dict]:
        """임대 없이 대기 항목 보기(검증 전 정보 안내용)."""
        if self.dry_run:
            rows = [r for r in self._mem.values() if r["status"] == "queued"]
            rows.sort(key=lambda r: (-r.get("priority", 0), r["first_seen"]))
            return rows[:limit]
        return self._post("/admin/queue", {"op": "peek", "limit": limit})["items"]

    def queue_result(self, items: list[dict]) -> set[str]:
        """처리 결과 보고. 임대를 잃은 항목(만료 후 다른 실행이 가져감·내용 변경)은 Worker 가 거부하며, 그 key 집합을 돌려준다.
        거부된 항목의 결과는 쓰지 않는다(지금 가진 실행의 상태를 덮어쓰지 않음)."""
        items = [{**x, **{k: v for k, v in self._leases.get(x["key"], {}).items() if k not in x}} for x in items]
        rejected: set[str] = set()
        if self.dry_run:
            now = datetime.now(timezone.utc)
            for x in items:
                r = self._mem.get(x["key"])
                if r is None or not x.get("lease_id") or r.get("lease_id") != x.get("lease_id") or r["hash"] != x.get("hash") \
                        or not r.get("lease_until") or r["lease_until"] < now:
                    rejected.add(x["key"])
                    continue
                if x["status"] != "extracted":
                    r["lease_until"], r["lease_id"] = None, None
                if x["status"] == "release":
                    continue
                if x.get("error"):
                    r["last_error"] = x["error"]
                if x["status"] == "retry":
                    r["attempts"] += 1
                    r["status"] = "stuck" if r["attempts"] >= 6 else "queued"
                else:
                    r["status"], r["result"] = x["status"], x.get("result")
        else:
            for i in range(0, len(items), 100):
                rejected.update(self._post("/admin/queue", {"op": "result", "items": items[i:i + 100]}).get("rejected", []))
        for x in items:
            if x["key"] in rejected or x["status"] != "extracted":
                self._leases.pop(x["key"], None)
        return rejected

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

    def queries_extra(self) -> list[dict]:
        if self.dry_run:
            return []
        return self._get("/admin/queries-extra").get("queries", [])

    def queue_find(self, urls: list[str]) -> list[dict]:
        if self.dry_run or not urls:
            return []
        return self._post("/admin/queue-find", {"urls": urls}).get("items", [])

    def gaps(self, gaps: list[dict], sources: list[dict], queries: list[dict]) -> dict:
        if self.dry_run:
            return {"ok": True, "dry_run": True}
        return self._post("/admin/gaps", {"gaps": gaps, "sources": sources, "queries": queries})

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
