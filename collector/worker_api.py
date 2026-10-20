"""us-alert-worker 관리 경로 호출. dry-run 이면 아무것도 쓰지 않고 '처음 본 항목'으로만 대답한다."""
from __future__ import annotations

import os

import httpx

UA = "us-alert-collector/run"


class WorkerAPI:
    def __init__(self, url: str | None, key: str | None, dry_run: bool = False, client: httpx.Client | None = None):
        self.dry_run = dry_run or not (url and key)
        self.url = (url or "").rstrip("/")
        self.client = client or httpx.Client(timeout=60, headers={"authorization": f"Bearer {key}", "user-agent": UA})

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

    def seen_check(self, items: list[dict]) -> tuple[set, set]:
        if self.dry_run:
            return {x["key"] for x in items}, set()
        new, changed = set(), set()
        for i in range(0, len(items), 300):
            r = self._post("/admin/seen", {"op": "check", "items": items[i:i + 300]})
            new.update(r["new"])
            changed.update(r["changed"])
        return new, changed

    def seen_mark(self, items: list[dict]) -> None:
        if self.dry_run:
            return
        for i in range(0, len(items), 300):
            self._post("/admin/seen", {"op": "mark", "items": items[i:i + 300]})

    def enqueue(self, events: list[dict]) -> dict:
        tot: dict = {"new": 0, "changed": 0, "same": 0, "outbox": 0, "accepted": 0, "skipped": {}}
        if self.dry_run:
            return {**tot, "dry_run": True, "accepted": len(events)}
        for i in range(0, len(events), 10):
            r = self._post("/admin/enqueue", {"events": events[i:i + 10]})
            for k in ("new", "changed", "same", "outbox", "accepted"):
                tot[k] += r.get(k, 0)
            for k, v in r.get("skipped", {}).items():
                tot["skipped"][k] = tot["skipped"].get(k, 0) + v
        return tot

    def dispatch(self) -> dict:
        if self.dry_run:
            return {"dry_run": True}
        tot = {"sent": 0, "failed": 0, "deferred": 0}
        for _ in range(5):
            r = self._post("/admin/dispatch", {})
            for k in tot:
                tot[k] += r.get(k, 0)
            if r.get("sent", 0) + r.get("failed", 0) == 0 or r.get("pending", 0) <= r.get("deferred", 0):
                break
        return tot

    def month_usd(self) -> float:
        return 0.0 if self.dry_run else float(self._get("/admin/usage").get("usd", 0))

    def add_usage(self, by_model: dict) -> None:
        if self.dry_run:
            return
        for model, m in by_model.items():
            self._post("/admin/usage", {"model": model, **{k: m[k] for k in ("input_tokens", "output_tokens", "usd", "calls")}})

    def sources_extra(self) -> list[dict]:
        return [] if self.dry_run else self._get("/admin/sources-extra").get("sources", [])

    def candidates(self, cands: list[dict]) -> dict:
        return {"dry_run": True} if self.dry_run or not cands else self._post("/admin/candidates", {"candidates": cands})

    def run_log(self, body: dict) -> dict:
        return {"dry_run": True} if self.dry_run else self._post("/admin/run-log", body)
