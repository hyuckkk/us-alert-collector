"""#43 건식 검증 요약: run.py --dry-run 결과의 행사마다 판정과 '예상 텔레그램 처리'를 표로 만든다(발송 없음).

예상 처리는 Worker pipeline.js recLevel·sports.js 판별과 같은 순서로 본다: 경기 달력 전용 → 보류 → 적극 알림(차단 없음) → 관심 설정 시 → 제외.
  python3 scripts/verify43_summary.py reports/verify43-run.json eval/verify43.md
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
import sports_diag  # noqa: E402


def expected(e: dict) -> str:
    if sports_diag.calendar_match(e) and not e.get("sports_special_verified"):
        return "경기 달력(개별 알림 없음)"
    if e.get("hold_reason"):
        return f"보류({e['hold_reason']})"
    if e.get("rec_level") == "push" and e.get("special_reason") and not e.get("rec_blockers") and not e.get("push_blocked"):
        return f"적극 알림 → {e.get('bot')} 봇"
    if e.get("rec_level") == "push":
        return f"보내지 않음({e.get('push_blocked') or ','.join(e.get('rec_blockers') or [])})"
    return {"interest": "관심 설정 시만", "none": "일반 제외"}.get(e.get("rec_level"), "판정 없음(저장만)")


def main() -> int:
    run = json.loads(Path(sys.argv[1]).read_text())
    evs = run.get("events") or []
    rows = ["| 정보원 | 날짜 | 제목 | 분야 | 학교급·종목·형식 | 판정 | 외국인 출입·가족 동승·비용 | 예상 처리 |", "|---|---|---|---|---|---|---|---|"]
    for e in sorted(evs, key=lambda x: (x.get("source_id") or "", x.get("start_date") or "")):
        sp = " · ".join(str(x) for x in (e.get("school_level"), e.get("sport"), e.get("event_type"), e.get("start_time"), e.get("cost_class")) if x)
        note = " / ".join(x for x in (e.get("foreign_access") if e.get("foreign_access") not in (None, "not_applicable") else "",
                                      e.get("family_water") if e.get("family_water") not in (None, "not_applicable") else "", e.get("fee_note") or "") if x)
        cell = lambda s: str(s or "").replace("|", "/")[:80]  # noqa: E731
        rows.append(f"| {cell(e.get('source_id'))} | {e.get('start_date') or ''} | {cell(e.get('title'))} | {e.get('topic') or ''} | {cell(sp)} | "
                    f"{e.get('rec_level')}·{e.get('bot')} | {cell(note)} | {expected(e)} |")
    m = run.get("metrics", {})
    head = [f"# #43 건식 검증({run.get('today')}, 발송 없음)", "",
            f"- 정보원 {m.get('sources_ok')}/{m.get('sources_total')} · 새 항목 {m.get('entries_new')} · 행사 {len(evs)} · 비용 ${m.get('usd')}",
            f"- 버린 이유: {json.dumps(run.get('dropped') or {}, ensure_ascii=False)}", ""]
    src = ["", "## 정보원별 수집(무료 단계)", "", "| 정보원 | 성공 | 항목 | 오류 |", "|---|---|---|---|"]
    src += [f"| {r['id']} | {r['ok']} | {r['items']} | {r.get('error') or ''} |" for r in run.get("sources", [])]
    for sid, lst in (run.get("entries_sample") or {}).items():
        src += ["", f"### {sid}", ""] + [f"- {x['title']} — {x['url']}\n  > {x['text'][:300]}" for x in lst]
    Path(sys.argv[2]).write_text("\n".join(head + rows + src) + "\n")
    print("::notice title=verify43::" + json.dumps({"events": len(evs), "usd": m.get("usd"),
                                                     "expected": {k: sum(1 for e in evs if expected(e).startswith(k)) for k in ("경기 달력", "보류", "적극 알림", "보내지 않음", "관심", "일반")}},
                                                    ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
