"""staging-test 워크플로 안전성 시험(4차 검증 1). 워크플로의 각 run 단계를 실제 bash 로 실행하되
wrangler(npx)·curl·node·할당량 점검은 가짜 Cloudflare 계정 상태(JSON)를 다루는 대역으로 바꾼다.

확인하는 것: 실행마다 고유한 이름, 이미 있는 자원은 재사용·삭제하지 않음, 이 실행이 만든 것만 정리,
중간 실패·취소 시에도 정리, 할당량 점검 실패 시 아무것도 만들지 않음, 운영 자원(us-alert-worker / us-alert-db)은 절대 건드리지 않음.
STAGING_WORKFLOW 환경변수로 다른 판(수정 전)의 워크플로 파일을 지정해 같은 시험을 돌릴 수 있다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WF = Path(os.environ.get("STAGING_WORKFLOW") or ROOT / ".github/workflows/staging-test.yml")
RUN_ID, ATTEMPT = "777", "1"
PROD = {"d1": ["us-alert-db", "korea-db"], "workers": ["us-alert-worker", "telegram-relay"]}

NPX = r'''#!/usr/bin/env python3
import json, os, re, sys, uuid
st_path = os.environ["CF_STATE"]; st = json.load(open(st_path)); a = sys.argv[1:]
st["log"].append(" ".join(a))
def save(): json.dump(st, open(st_path, "w"))
fail = os.environ.get("FAIL_AT", "")
if a[:1] != ["wrangler"]: save(); sys.exit(0)
a = a[1:]
def opt(name):
    return a[a.index(name) + 1] if name in a else None
if a[:2] == ["d1", "list"]:
    print(json.dumps([{"name": n, "uuid": "id-" + n} for n in st["d1"]])); save(); sys.exit(0)
if a[:2] == ["d1", "create"]:
    if fail == "d1create": save(); sys.exit(1)
    st["d1"].append(a[2]); st["created"].append("d1:" + a[2]); save(); sys.exit(0)
if a[:2] == ["d1", "delete"]:
    if a[2] in st["d1"]: st["d1"].remove(a[2]); st["deleted"].append("d1:" + a[2])
    save(); sys.exit(0)
if a[:2] == ["d1", "migrations"]:
    save(); sys.exit(1 if fail == "migrate" else 0)
if a[:1] == ["deploy"]:
    cfg = opt("--config") or "wrangler.toml"
    name = re.search(r'^name = "([^"]+)"', open(cfg).read(), re.M).group(1)
    if name not in st["workers"]: st["workers"].append(name); st["created"].append("worker:" + name)
    save(); sys.exit(1 if fail == "deploy" else 0)   # 실패해도 자원은 생긴 경우(부분 배포)를 흉내
if a[:1] == ["delete"]:
    cfg = opt("--config") or "wrangler.toml"
    name = opt("--name") or re.search(r'^name = "([^"]+)"', open(cfg).read(), re.M).group(1)
    if name in st["workers"]: st["workers"].remove(name); st["deleted"].append("worker:" + name)
    save(); sys.exit(0)
save(); sys.exit(0)
'''

CURL = r'''#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ["CF_STATE"])); a = sys.argv[1:]
url = next((x for x in a if x.startswith("http")), "")
if "/workers/scripts/" in url:
    name = url.rsplit("/", 1)[1]
    sys.stdout.write("200" if name in st["workers"] else "404"); sys.exit(0)
sys.stdout.write(("401" if os.environ.get("FAIL_AT") == "auth" else "200") if "-w" in a else '{"ok":true}'); sys.exit(0)
'''

STUB_EXIT = '#!/usr/bin/env bash\nexit $([ "${FAIL_AT:-}" = "%s" ] && echo 1 || echo 0)\n'


def _steps(wf: dict) -> list[dict]:
    return list(wf["jobs"].values())[0]["steps"]


def _sub(text: str) -> str:
    text = text.replace("${{ github.run_id }}", RUN_ID).replace("${{ github.run_attempt }}", ATTEMPT)
    return re.sub(r"\$\{\{\s*secrets\.(\w+)\s*\}\}", lambda m: f"dummy-{m.group(1)}", text)


def simulate(tmp: Path, fail_at: str = "", existing: dict | None = None, cancel_at: str = "") -> dict:
    wf = yaml.safe_load(_sub(WF.read_text()))
    job = list(wf["jobs"].values())[0]
    work = tmp / "repo"
    (work / "worker").mkdir(parents=True)
    (work / "scripts").mkdir()
    shutil.copy(ROOT / "worker/wrangler.toml", work / "worker/wrangler.toml")
    shutil.copy(ROOT / "scripts/run.sh", work / "scripts/run.sh")
    (work / "scripts/cf_env.sh").write_text('echo "US_WORKER_URL=https://us-alert-worker.sub.workers.dev" >> "$GITHUB_ENV"\n')
    (work / "scripts/cf_quota.py").write_text("import os, sys\nsys.exit(1 if os.environ.get('FAIL_AT') == 'quota' else 0)\n")
    bin_ = tmp / "bin"
    bin_.mkdir()
    for name, body in {"npx": NPX, "curl": CURL, "node": STUB_EXIT % "test", "openssl": "#!/usr/bin/env bash\necho deadbeef\n", "sleep": "#!/usr/bin/env bash\nexit 0\n"}.items():
        p = bin_ / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    state = {"d1": list(PROD["d1"]) + list((existing or {}).get("d1", [])), "workers": list(PROD["workers"]) + list((existing or {}).get("workers", [])),
             "log": [], "created": [], "deleted": []}
    sp = tmp / "state.json"
    sp.write_text(json.dumps(state))
    genv = tmp / "github_env"
    genv.write_text("")
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "CF_STATE": str(sp), "FAIL_AT": fail_at, "GITHUB_ENV": str(genv)}
    env.update({k: str(v) for k, v in (job.get("env") or {}).items()})
    wd = work / (job.get("defaults", {}).get("run", {}).get("working-directory") or ".")
    failed, ran = False, []
    for st in job["steps"]:
        if "run" not in st or st["run"].strip().startswith("npm ci"):
            continue
        cond = str(st.get("if", ""))
        if failed and "always()" not in cond:
            continue
        for line in genv.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
        name = st.get("name") or st["run"].split("\n")[0][:40]
        if cancel_at and cancel_at in name:
            failed = True  # 취소: 이 단계부터 always() 단계만
            continue
        r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", st["run"]], cwd=wd, env=env, capture_output=True, text=True)
        ran.append((name, r.returncode, (r.stdout + r.stderr)[-400:]))
        if r.returncode != 0 and "always()" not in cond:
            failed = True
    out = json.loads(sp.read_text())
    out["ran"], out["failed"] = ran, failed
    out["env"] = {k: env.get(k) for k in ("STG_WORKER", "STG_DB")}
    return out


def _assert_prod_untouched(s):
    for n in PROD["d1"]:
        assert n in s["d1"], f"운영/한국 D1 {n} 삭제됨"
    for n in PROD["workers"]:
        assert n in s["workers"], f"운영/한국 Worker {n} 삭제됨"
    assert not any(x.endswith((":us-alert-db", ":us-alert-worker")) for x in s["created"])


def test_names_are_unique_per_run():
    wf = yaml.safe_load(_sub(WF.read_text()))
    env = list(wf["jobs"].values())[0].get("env") or {}
    text = _sub(WF.read_text())
    assert RUN_ID in str(env.get("STG_DB", "")) and RUN_ID in str(env.get("STG_WORKER", "")), "시험 자원 이름에 실행 ID 없음"
    assert "us-alert-db-staging" not in text, "고정 이름 시험 DB 사용"


def test_success_creates_and_deletes_only_own(tmp_path):
    s = simulate(tmp_path, existing={"d1": ["us-alert-db-staging", "us-alert-db-stg-1-1"]})
    _assert_prod_untouched(s)
    assert "us-alert-db-staging" in s["d1"] and "us-alert-db-stg-1-1" in s["d1"], "남의(이전) 시험 DB 를 지움"
    assert sorted(s["created"]) == sorted(x.replace("deleted", "") for x in s["deleted"]), (s["created"], s["deleted"])
    assert len(s["created"]) == 2 and not s["failed"]


def test_existing_same_name_is_refused_not_reused_or_deleted(tmp_path):
    own = f"us-alert-db-stg-{RUN_ID}-{ATTEMPT}"
    s = simulate(tmp_path, existing={"d1": [own]})
    assert s["failed"]
    assert own in s["d1"], "이미 있던 같은 이름 DB 를 지움"
    assert s["created"] == [] and s["deleted"] == []


@pytest.mark.parametrize("fail_at", ["migrate", "deploy", "auth", "test"])
def test_failure_midway_cleans_up_what_was_created(tmp_path, fail_at):
    s = simulate(tmp_path, fail_at=fail_at)
    _assert_prod_untouched(s)
    assert s["failed"]
    assert s["created"], "시험 자원이 하나도 안 만들어짐(시나리오 확인)"
    assert sorted(s["created"]) == sorted(s["deleted"]), (s["created"], s["deleted"])


def test_cancel_during_test_still_cleans_up(tmp_path):
    s = simulate(tmp_path, cancel_at="Run remote queue test")
    _assert_prod_untouched(s)
    assert sorted(s["created"]) == sorted(s["deleted"]) and s["created"]


@pytest.mark.parametrize("fail_at", ["quota", "d1create"])
def test_failure_before_creation_deletes_nothing(tmp_path, fail_at):
    s = simulate(tmp_path, fail_at=fail_at, existing={"d1": ["us-alert-db-stg-1-1"]})
    _assert_prod_untouched(s)
    assert s["deleted"] == [], s["deleted"]
    assert "us-alert-db-stg-1-1" in s["d1"]


def test_quota_check_runs_before_any_creation():
    names = [st.get("name", "") for st in _steps(yaml.safe_load(_sub(WF.read_text())))]
    q = next((i for i, n in enumerate(names) if "quota" in n.lower()), None)
    c = next(i for i, n in enumerate(names) if "Create" in n)
    assert q is not None and q < c, "할당량 점검이 없거나 자원 생성 뒤에 있음"


def test_quota_decision_logic():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cf_quota", ROOT / "scripts/cf_quota.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    used = {"requests": 1000, "rows_read": 10_000, "rows_written": 50_000, "databases": 3}
    need = {"requests": 1000, "rows_read": 500_000, "rows_written": 25_000, "databases": 1}
    assert m.decide(used, need) == []
    assert any(x.startswith("rows_written") for x in m.decide({**used, "rows_written": 60_000}, need)), "쓴 행 안전선 초과를 못 잡음"
    assert any(x.startswith("databases") for x in m.decide({**used, "databases": 10}, need)), "D1 개수 한도를 못 잡음"


def test_remote_test_refuses_non_staging_db():
    src = (ROOT / "worker/test/remote-queue.mjs").read_text()
    assert "STG_DB" in src and "-stg-" in src and "us-alert-db-staging" not in src
