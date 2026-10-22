"""AI 발굴 모델 비교 시험(2차 재검증 2): 같은 주제로 여러 모델을 돌려 발견 수, 공식 링크 정확성, 중복, 비용을 잰다.

python collector/compare_discover.py --models claude-opus-5-5,claude-sonnet-5-5 [--out reports/model-compare.json]
- 발견한 URL 은 실제로 조회해(토큰 0) 열리는지·행사 링크가 있는지·공식 도메인인지 확인한다.
- Worker 에는 아무것도 쓰지 않는다. 결과의 '실제 유효 행사 여부'는 사람이 기준 목록으로 따로 확인해야 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
import identity  # noqa: E402
import run  # noqa: E402
from fetch import fetch_source, normalize_url  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def check_url(url: str) -> dict:
    r = fetch_source({"id": "cmp", "type": "html", "url": url})
    host = identity._host(url)
    return {"ok": r.ok, "status": r.status, "event_links": len(r.entries), "official": host.endswith(run.OFFICIAL_TLD), "error": r.error}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="claude-opus-5-5,claude-sonnet-5-5")
    ap.add_argument("--out", default="reports/model-compare.json")
    a = ap.parse_args(argv)
    models = run.load("models.json")
    known = [s["url"] for s in run.load("sources.json")["sources"]]
    known_hosts = {identity._host(u) for u in known}
    themes = list(run.DAILY_THEMES.items()) + [("deep", run.DEEP_THEMES[0])]
    client = ai.get_client()
    prices = {**models["prices"], "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.20}}
    out: dict = {"themes": [t for _, t in themes], "models": {}}
    url_checks: dict[str, dict] = {}
    for model in a.models.split(","):
        cfg = {**models, "discover": {**models["discover"], "model": model}}
        usage = ai.Usage(prices, models.get("web_search_usd", 0.01))
        found_s, found_e, errors, t0, raw = [], [], [], time.time(), {}
        for bot, theme in themes:
            try:
                d = ai.discover(client, cfg, theme, known, usage, max_searches=models["discover"]["max_searches_deep" if bot == "deep" else "max_searches"])
                raw[bot] = {"text_tail": d.get("raw_text", "")[-800:], "dropped_urls": d.get("dropped_urls"), "result_urls": d.get("result_urls")}
                found_s += [{**s, "theme": bot} for s in d["sources"]]
                found_e += [{**e, "theme": bot} for e in d["events"]]
            except Exception as e:  # noqa: BLE001
                errors.append(f"{bot}: {type(e).__name__}: {str(e)[:150]}")
        urls = list(dict.fromkeys(normalize_url(x["url"]) for x in found_s + found_e))
        for u in urls:
            if u not in url_checks:
                url_checks[u] = check_url(u)
        src_urls = list(dict.fromkeys(normalize_url(x["url"]) for x in found_s))
        out["models"][model] = {
            "sources": len(src_urls), "events": len({normalize_url(x["url"]) for x in found_e}),
            "unique_urls": len(urls), "duplicate_mentions": len(found_s) + len(found_e) - len(urls),
            "fetchable": sum(url_checks[u]["ok"] for u in urls), "official_domain": sum(url_checks[u]["official"] for u in urls),
            "sources_with_event_links": sum(1 for u in src_urls if url_checks[u]["ok"] and url_checks[u]["event_links"] >= 3),
            "already_known_host": sum(1 for u in src_urls if identity._host(u) in known_hosts),
            "usd": round(usage.usd, 4), "usage": usage.by_model, "seconds": round(time.time() - t0),
            "errors": errors, "source_list": found_s, "event_list": found_e, "raw": raw,
        }
    names = list(out["models"])
    if len(names) == 2:
        sa = {normalize_url(x["url"]) for x in out["models"][names[0]]["source_list"] + out["models"][names[0]]["event_list"]}
        sb = {normalize_url(x["url"]) for x in out["models"][names[1]]["source_list"] + out["models"][names[1]]["event_list"]}
        out["overlap"] = {"both": len(sa & sb), f"only_{names[0]}": len(sa - sb), f"only_{names[1]}": len(sb - sa)}
    out["url_checks"] = url_checks
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({m: {k: v for k, v in r.items() if k not in ("source_list", "event_list", "usage")} for m, r in out["models"].items()}, ensure_ascii=False, indent=1))
    print(json.dumps(out.get("overlap"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY required")
    sys.exit(main())
