"""운영 중인 실제 행사 표본을 원문과 대조(유료, 개발 예산).

운영 Worker 에 저장된 행사를 무작위로 뽑아, 각 행사의 원문 페이지를 다시 받고, 추출 모델(Haiku)과 다른 큰 모델(Opus)이
원문만 보고 날짜·시간·비용·연령·접수 시작일·마감일·접수 상태를 독립적으로 판정한다. 기준 표본(30건) 발견율과 별개로
'실제로 보내는 정보가 원문과 맞는가'를 본다.

  python collector/audit_sample.py --n 40 --usd-cap 3 --out eval/audit_sample.json
결과: 항목별 일치·불일치·원문에 없음 건수, 불일치 사례(원문 근거 포함).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import BaseModel  # noqa: E402

import ai  # noqa: E402
from fetch import Entry, fetch_detail, new_client  # noqa: E402
from worker_api import WorkerAPI  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("start_date", "end_date", "time_text", "cost_text", "age_text", "registration_opens", "deadline", "registration_status")
Verdict = Literal["match", "mismatch", "not_in_source", "missing_in_extract"]


class FieldCheck(BaseModel):
    field: str
    verdict: Verdict
    source_value: Optional[str]
    evidence: Optional[str]


class Audit(BaseModel):
    page_is_this_event: bool
    attendable_public: bool
    already_over: bool
    checks: list[FieldCheck]
    note: Optional[str]


SYSTEM = """You audit an alert system's extracted event data against the official source page text. Use only the page text.
For each field listed, compare the extracted value with what the page says:
- match: the page states the same value (formatting/language may differ; Korean text in the extract is fine, e.g. "무료" = "Free", "오전 10시" = "10:00 AM").
- mismatch: the page states a different value. Put the page's value in source_value and a short quote in evidence.
- not_in_source: the extract has a value but the page does not state it, or both are empty.
- missing_in_extract: the page clearly states a value but the extract is empty.
Dates are YYYY-MM-DD; if the page gives a weekday/month/day without a year, judge using the date of the audit given below.
page_is_this_event: false if the page is about a different event or a list where this event cannot be found.
attendable_public: false if only for students, staff, members, base personnel or invitation.
already_over: true if the event (or registration window, for registration notices) has ended before the audit date."""


def audit_one(client, model: str, ev: dict, text: str, today: str, usage: ai.Usage):
    shown = {k: ev.get(k) for k in FIELDS}
    prompt = (f"Audit date: {today}\nEvent title (extracted): {ev.get('title')} / {ev.get('title_ko')}\nExtracted fields: {json.dumps(shown, ensure_ascii=False)}\n"
              f"Fields to check: {', '.join(FIELDS)}\n\nSource page text:\n{text[:7000]}")
    resp = client.messages.parse(model=model, max_tokens=4000, system=SYSTEM, messages=[{"role": "user", "content": prompt}],
                                 output_config={"effort": "medium"}, output_format=Audit)
    usage.add(model, resp.usage)
    return resp.parsed_output


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--usd-cap", type=float, default=3.0)
    ap.add_argument("--out", default=str(ROOT / "eval/audit_sample.json"))
    a = ap.parse_args(argv)
    cfg = json.loads((ROOT / "config/models.json").read_text())
    model = cfg["judge"]["model"]
    usage = ai.Usage(cfg.get("prices", {}), cap=a.usd_cap)
    api = WorkerAPI.from_env()
    sample = api._get(f"/admin/events-sample?n={a.n}")["events"]
    today = sample[0]["today"] if sample else ""
    client = ai.get_client()

    def page(ev):
        e = Entry(ev.get("source_id") or "audit", ev["url"], ev.get("title") or "")
        try:
            fetch_detail(e, hc, chars=7000)
        except Exception as x:  # noqa: BLE001
            e.text = ""
            e.extra["detail_error"] = str(x)[:120]
        return e.text or ""

    with new_client() as hc, ThreadPoolExecutor(8) as ex:
        texts = list(ex.map(page, sample))
    rows = []
    for ev, text in zip(sample, texts):
        if not usage.allows(0.06):
            rows.append({"event_key": ev["event_key"], "skipped": "usd-cap"})
            continue
        if len(text) < 200:
            rows.append({"event_key": ev["event_key"], "url": ev["url"], "source_id": ev.get("source_id"), "skipped": "page-unavailable"})
            continue
        try:
            r = audit_one(client, model, ev, text, today, usage)
        except Exception as x:  # noqa: BLE001
            rows.append({"event_key": ev["event_key"], "url": ev["url"], "error": f"{type(x).__name__}: {str(x)[:150]}"})
            continue
        rows.append({"event_key": ev["event_key"], "url": ev["url"], "source_id": ev.get("source_id"), "title": ev.get("title_ko") or ev.get("title"),
                     "extracted": {k: ev.get(k) for k in FIELDS}, **r.model_dump()})
    audited = [r for r in rows if "checks" in r]
    by_field = {f: {"match": 0, "mismatch": 0, "not_in_source": 0, "missing_in_extract": 0} for f in FIELDS}
    for r in audited:
        for c in r["checks"]:
            if c["field"] in by_field:
                by_field[c["field"]][c["verdict"]] += 1
    summary = {
        "audit_date": today, "sampled": len(sample), "audited": len(audited),
        "skipped": sum(1 for r in rows if "skipped" in r), "errors": sum(1 for r in rows if "error" in r),
        "wrong_page": sum(1 for r in audited if not r["page_is_this_event"]),
        "not_public": sum(1 for r in audited if not r["attendable_public"]),
        "already_over": sum(1 for r in audited if r["already_over"]),
        "by_field": by_field,
        "events_with_any_mismatch": sum(1 for r in audited if any(c["verdict"] == "mismatch" for c in r["checks"])),
        "usd": round(usage.usd, 4),
    }
    Path(a.out).write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1) + "\n")
    live = os.environ.get("USAGE_LIVE_FILE")
    if live:
        Path(live).write_text(json.dumps({"usd": round(usage.usd, 4), "calls": sum(m["calls"] for m in usage.by_model.values()), "by_model": usage.by_model}))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
