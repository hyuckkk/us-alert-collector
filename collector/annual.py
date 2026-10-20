"""연간 고정 일정표(토큰 0): config/annual.json 의 반복 일정을 날짜가 다가오면 행사로 만든다."""
from __future__ import annotations

from datetime import date, timedelta


def due_items(cfg: dict, today: date, checked_at: str) -> list[dict]:
    out = []
    for it in cfg.get("items", []):
        for year in (today.year, today.year + 1):
            try:
                when = date(year, it["month"], it["day"])
            except ValueError:
                continue
            if today <= when <= today + timedelta(days=it.get("lead_days", 14)):
                out.append({
                    "event_key": f"a-{it['id']}-{year}"[:48], "bot": it["bot"], "title": it["title"], "title_ko": it["title_ko"], "org": None,
                    "start_date": when.isoformat(), "end_date": None, "time_text": None, "event_tz": "America/Denver", "location": None, "city": None,
                    "age_text": None, "english": [], "cost_text": None, "signup": "unknown", "deadline": None,
                    "status_note": None if it.get("verified") else "예년 기준 날짜 — 올해 공식 발표 확인 필요",
                    "url": it["url"], "checked_at": checked_at, "urgency": "now", "tags": it.get("tags", []), "annual": True,
                    "year_label": f"{year}년 일정(연간 일정표)", "source_id": "annual",
                })
    return out
