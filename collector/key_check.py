"""API 키 점검 결과를 us-alert-worker 에 보고한다. 키가 거부되면 Worker 가 소유자 DM 으로 알린다."""
import json
import os
import sys
import urllib.error
import urllib.request

from ai import check_key


def main() -> int:
    result = {"name": "anthropic", **check_key()}
    print(json.dumps(result, ensure_ascii=False))
    level = "notice" if result["ok"] else "error"
    print(f"::{level} title=API key check::{result['status']} - {result['detail']}")
    url, key = os.environ.get("US_WORKER_URL"), os.environ.get("US_ADMIN_KEY")
    if not url or not key:
        print("US_WORKER_URL / US_ADMIN_KEY not set; result not reported")
        return 1
    req = urllib.request.Request(
        f"{url.rstrip('/')}/admin/key-check",
        data=json.dumps(result).encode(),
        headers={"authorization": f"Bearer {key}", "content-type": "application/json", "user-agent": "us-alert-collector/key-check"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"::error title=Worker report::HTTP {e.code} {e.read()[:200]!r}")
        return 1
    return 0 if result["ok"] or result["status"] == "transient" else 1


if __name__ == "__main__":
    sys.exit(main())
