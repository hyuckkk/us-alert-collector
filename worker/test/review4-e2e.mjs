// 4차 검증 지적 재현 검사(Worker 쪽): 2) 기준선 중 접수 시작·마감 시간 알림, 배포 직후 기준선 설정 전 첫 자동 수집.
// 수정 전 코드에서 실패하고, 수정 후 통과해야 한다.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { startEnv, stepper, OWNER, ADMIN } from "./harness.mjs";

const { step, failed } = stepper();
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "kids", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(60), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "weekly", tags: ["t"], annual: false, source_id: "test", registration_status: "open", ...over,
});

// 배포 직후 상태: 마이그레이션만 적용, 기준선 명령은 아직 아무도 실행하지 않음
const E = await startEnv({ tgPort: 9992, wPort: 8802, fresh: true, vars: { FAMILY_AVAILABLE_FROM: "2026-01-01" } });
const { admin, hook, msg, sql } = E;
const sends = (from) => E.calls.slice(from).filter((c) => c.method === "sendMessage");
const has = (list, key) => list.filter((c) => (c.payload.text || "").includes(`행사 ${key}`)).length;

// 운영 시작 순서 관문: collect·dispatch 가 쓰는 scripts/worker_ready.sh 의 판정
const ready = (url) => {
  const f = join(mkdtempSync(join(tmpdir(), "ready-")), "env");
  execFileSync("bash", ["../scripts/worker_ready.sh"], { env: { ...process.env, US_WORKER_URL: url, US_ADMIN_KEY: ADMIN, GITHUB_ENV: f }, stdio: "pipe" });
  return readFileSync(f, "utf8").trim();
};

try {
  await step("운영 시작 순서: 기준선 상태를 갖춘 Worker 면 READY=1, 배포 전·응답 없는 Worker 면 READY=0(collect·dispatch 건너뜀)", async () => {
    assert.equal(ready(E.W), "READY=1");
    assert.equal(ready("http://127.0.0.1:9"), "READY=0");
  });

  await hook("kids", msg(OWNER, "/start"));
  await hook("kids", msg(OWNER, "/quiet 0 0"));

  await step("배포 직후 기준선 설정 전에 첫 자동 수집이 돌아도 옛 정보·즉시 알림을 보내지 않는다(기준선 대기 상태로 시작)", async () => {
    const st = await admin("/admin/baseline");
    assert.equal(st.active, true, `배포 직후 기준선 비활성: ${JSON.stringify(st)}`);
    const b = E.calls.length;
    const r = await admin("/admin/enqueue", { events: [
      ...Array.from({ length: 20 }, (_, i) => ev(`old${i}`, { urgency: "now", start_date: plus(60 + i) })),
      ev("near1", { urgency: "now", start_date: plus(5) }),
    ] });
    assert.equal(r.baseline, true, JSON.stringify(r));
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(sends(b).length, 0, `즉시 발송 ${sends(b).length}건`);
    assert.deepEqual(sql("SELECT DISTINCT kind FROM outbox").map((x) => x.kind), ["digest"]);
  });

  await step("기준선 중 '오늘 접수 시작'·'내일 마감' 행사: 즉시 ⏰ 알림 없이 일간 요약에 남는다", async () => {
    await admin("/admin/baseline", { hours: 36 });
    const b = E.calls.length;
    await admin("/admin/enqueue", { events: [
      ev("open1", { registration_opens: denverToday, registration_status: "upcoming", start_date: plus(40) }),
      ev("dead1", { deadline: plus(1), start_date: plus(45) }),
    ] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const instant = sends(b);
    assert.equal(has(instant, "open1") + has(instant, "dead1"), 0, `기준선 중 즉시 시간 알림 ${instant.length}건`);
    const log = sql("SELECT event_key FROM trigger_log ORDER BY event_key").map((x) => x.event_key);
    assert.deepEqual(log, ["dead1", "open1"], "시간 알림 처리 기록");
    const pend = sql("SELECT event_key, kind, version FROM outbox WHERE event_key IN ('open1','dead1') AND status = 'pending' ORDER BY event_key, version");
    assert.ok(pend.length >= 2 && pend.every((x) => x.kind === "digest"), JSON.stringify(pend));
    await admin("/admin/digest", { force: true });
    const dg = sends(b).filter((c) => /행사 open1/.test(c.payload.text || "") && /행사 dead1/.test(c.payload.text || ""));
    assert.equal(dg.length, 1, "두 접수 기회가 요약 1통에 남아야 함");
  });

  await step("기준선이 끝난 같은 날 다시 돌려도 같은 시간 알림이 즉시 중복으로 나가지 않는다", async () => {
    await admin("/admin/baseline", { hours: 0 });
    const b = E.calls.length;
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(has(sends(b), "open1") + has(sends(b), "dead1"), 0);
  });

  await step("기준선이 꺼진 뒤 새 접수 기회는 정상적으로 즉시 ⏰ 알림", async () => {
    const b = E.calls.length;
    await admin("/admin/enqueue", { events: [ev("open2", { registration_opens: denverToday, registration_status: "upcoming", start_date: plus(40) })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.ok(sends(b).some((c) => /^⏰/.test(c.payload.text || "") && /행사 open2/.test(c.payload.text)), "기준선 해제 후 시간 알림 없음");
  });
} finally {
  E.stop();
}
if (failed()) process.exit(1);
