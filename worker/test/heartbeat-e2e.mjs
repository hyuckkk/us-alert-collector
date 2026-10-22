// GitHub 예약 실행과 독립된 매시 실행 경로(Durable Object 알람) 검사.
// 운영 첫날 GitHub 예약 작업이 4.5시간 늦어 수집·발송이 멈췄던 문제를 재현하고, Worker 스스로 발송하고 수집 재실행을 요청하는지 본다.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const { step, failed } = stepper();
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "kids", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(30), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "now", tags: ["t"], annual: false, source_id: "test", registration_status: "open", ...over,
});
const hoursAgo = (h) => new Date(Date.now() - h * 3600000).toISOString();

// 1) 토큰 없음
const A = await startEnv({ tgPort: 9997, wPort: 8807, vars: { FAMILY_AVAILABLE_FROM: "2026-01-01" } });
// 2) 수집 실행 토큰 있음(가짜 GitHub = 가짜 텔레그램 서버)
const B = await startEnv({ tgPort: 9998, wPort: 8808, vars: { FAMILY_AVAILABLE_FROM: "2026-01-01", GH_DISPATCH_TOKEN: "ghp_test", GH_API: "http://127.0.0.1:9998" } });
const sends = (E, n) => E.calls.slice(n).filter((c) => c.method === "sendMessage");

try {
  await step("배포 직후 setup 이 매시 알람을 건다(다음 알람이 1시간 안, 매시 35분)", async () => {
    const r = await A.admin("/admin/setup", {});
    const next = Date.parse(r.heartbeat.next_alarm);
    assert.ok(next > Date.now() && next <= Date.now() + 3600000 + 60000, JSON.stringify(r.heartbeat));
    assert.equal(new Date(next).getUTCMinutes(), 35);
  });

  await step("GitHub 발송 작업이 돌지 않아도 매시 실행이 즉시 알림을 보낸다", async () => {
    await A.admin("/admin/heartbeat", {});               // 첫 실행: 소유자 수신자 등록
    await A.hook("kids", A.msg(OWNER, "/quiet 0 0"));    // 시험 시각이 정지 시간대여도 보내도록
    await A.admin("/admin/enqueue", { events: [ev("hb1")] });
    const n = A.calls.length;
    const r = await A.admin("/admin/heartbeat", {});
    assert.equal(r.dispatch.sent, 1, JSON.stringify(r));
    assert.ok(sends(A, n).some((c) => String(c.payload.chat_id) === OWNER && /행사 hb1/.test(c.payload.text)));
  });

  await step("수집이 7시간째 없으면(토큰 없음) 소유자에게 하루 한 번 알리고, 같은 구간에 다시 요청하지 않는다", async () => {
    A.sql(`INSERT INTO kv (k, v, updated_at) VALUES ('last_run_at', '${hoursAgo(7)}', '${hoursAgo(7)}'), ('last_collect_ok_at', '${hoursAgo(7)}', '${hoursAgo(7)}') ON CONFLICT(k) DO UPDATE SET v = excluded.v`);
    const n = A.calls.length;
    let r = await A.admin("/admin/heartbeat", {});
    assert.deepEqual([r.collect.late, r.collect.mode, r.collect.action], [true, "light", "no-token"], JSON.stringify(r.collect));
    assert.equal(sends(A, n).filter((c) => /수집이 7시간째/.test(c.payload.text)).length, 1);
    r = await A.admin("/admin/heartbeat", {});
    assert.equal(r.collect.action, "already-requested");
    const st = await A.admin("/admin/status");
    assert.match(String(st.last_heartbeat), /"via":"admin"/);
  });

  await step("토큰이 있으면 GitHub 에 수집 실행을 직접 요청한다(전체 수집이 26시간 넘게 성공하지 않으면 full)", async () => {
    B.sql(`INSERT INTO kv (k, v, updated_at) VALUES ('last_run_at', '${hoursAgo(30)}', '${hoursAgo(30)}'), ('last_collect_ok_at', '${hoursAgo(30)}', '${hoursAgo(30)}') ON CONFLICT(k) DO UPDATE SET v = excluded.v`);
    const n = B.calls.length;
    const r = await B.admin("/admin/heartbeat", {});
    assert.deepEqual([r.collect.mode, r.collect.action], ["full", "requested"], JSON.stringify(r.collect));
    const gh = B.calls.slice(n).filter((c) => c.method === "github");
    assert.equal(gh.length, 1);
    assert.equal(gh[0].url, "/repos/hyuckkk/us-alert-collector/actions/workflows/collect.yml/dispatches");
    assert.deepEqual(gh[0].payload, { ref: "main", inputs: { mode: "full", dry_run: "false" } });
    assert.equal(gh[0].auth, "Bearer ghp_test");
  });

  await step("수집이 제때 돌고 있으면 아무 요청도 하지 않는다", async () => {
    B.sql(`UPDATE kv SET v = '${hoursAgo(1)}' WHERE k IN ('last_run_at', 'last_collect_ok_at')`);
    const n = B.calls.length;
    const r = await B.admin("/admin/heartbeat", {});
    assert.equal(r.collect.late, false);
    assert.equal(B.calls.slice(n).filter((c) => c.method === "github").length, 0);
  });
} finally {
  A.stop(); B.stop();
}
if (failed()) process.exit(1);
