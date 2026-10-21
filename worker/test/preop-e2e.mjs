// 운영 전 검증(3차 검증 5) 중 로컬 D1 로 할 수 있는 것: 1,000건 이상 대기열 분할 처리, AI 실패·수집 중단 후 재실행,
// 사람별 대기열 생성 실패·복구, 새 정보 없는 날 무발송, 운영 첫날 기준선. 실제 Cloudflare D1·텔레그램 시험은 승인 후 별도.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const { step, failed } = stepper();
const FRIEND = "222";
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "kids", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(30), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "weekly", tags: ["t"], annual: false, source_id: "test", registration_status: "open", ...over,
});
const N = 1200;

const E = await startEnv({ tgPort: 9993, wPort: 8803, vars: { DELIVERY_MODE: "all", FAMILY_AVAILABLE_FROM: "2026-01-01" } });
const { admin, hook, msg, sql } = E;
const q = (body) => admin("/admin/queue", body);
const sendCount = () => E.calls.filter((c) => c.method === "sendMessage").length;
const metrics = {};

// 수집기 한 번의 실행을 흉내: limit 건을 60건씩 임대해 가져오고, decide(item) 결과로 보고. crash 면 보고하지 않음
async function runOnce(limit, decide) {
  const got = [];
  while (got.length < limit) {
    const r = await q({ op: "take", limit: Math.min(60, limit - got.length), lease_minutes: 30 });
    if (!r.items.length) break;
    got.push(...r.items);
  }
  const reports = got.map((x) => ({ ...decide(x), key: x.key, lease_id: x.lease_id, hash: x.hash })).filter((x) => x.status);
  let accepted = [];
  for (let i = 0; i < reports.length; i += 100) {
    const r = await q({ op: "result", items: reports.slice(i, i + 100) });
    accepted = accepted.concat(reports.slice(i, i + 100).filter((x) => !r.rejected.includes(x.key)));
  }
  return { got, accepted };
}

try {
  for (const who of [OWNER, FRIEND]) {
    await hook("kids", msg(who, "/start"));
    await hook("kids", msg(who, "/quiet 0 0"));
  }
  await hook("kids", msg(OWNER, `/approve ${FRIEND}`));

  await step(`대기열 ${N}건: 40건씩 넣고, 실행당 300건씩 나눠 처리. 중간 실행이 죽어도 유실·중복 없이 전부 처리`, async () => {
    const t0 = Date.now();
    const items = Array.from({ length: N }, (_, i) => ({ key: `p${String(i).padStart(4, "0")}`, hash: "h", source_id: `s${i % 20}`, url: `https://x.org/${i}`,
      title: `t${i}`, body: { text: "x".repeat(2000) }, priority: i % 5 }));
    for (let i = 0; i < N; i += 40) await q({ op: "add", items: items.slice(i, i + 40) });
    metrics.add_ms = Date.now() - t0;
    assert.equal((await q({ op: "stats" })).queued, N, "넣은 건수 보존");
    const doneCount = new Map();
    const mark = (acc) => acc.filter((x) => x.status === "done").forEach((x) => doneCount.set(x.key, (doneCount.get(x.key) || 0) + 1));
    // 실행 1: 300건 중 250건 처리 후 죽음(50건은 보고 없음)
    let n = 0;
    const r1 = await runOnce(300, () => (n++ < 250 ? { status: "done" } : {}));
    mark(r1.accepted);
    // 실행 2: 죽은 실행의 50건은 임대 중이라 받지 않고 새 300건
    const r2 = await runOnce(300, () => ({ status: "done" }));
    assert.equal(r2.got.filter((x) => r1.got.some((y) => y.key === x.key)).length, 0, "임대 중 항목을 다른 실행이 받음");
    mark(r2.accepted);
    // 죽은 실행의 임대 만료 → 이후 실행들이 남은 것 전부 처리
    sql("UPDATE pending SET lease_until = '2000-01-01T00:00:00Z' WHERE status = 'queued' AND lease_id IS NOT NULL");
    let runs = 2;
    const t1 = Date.now();
    for (;;) {
      const r = await runOnce(300, () => ({ status: "done" }));
      if (!r.got.length) break;
      mark(r.accepted);
      runs++;
    }
    metrics.process_ms = Date.now() - t1;
    metrics.runs = runs;
    const st = await q({ op: "stats" });
    assert.equal(doneCount.size, N, `처리 완료 ${doneCount.size}/${N}`);
    assert.ok([...doneCount.values()].every((v) => v === 1), "같은 항목 두 번 처리");
    assert.equal(st.queued, 0);
    assert.equal(st.counts.done, N);
    const body = sql("SELECT COUNT(*) AS n FROM pending WHERE body <> '{}'")[0].n;
    assert.equal(body, 0, "처리 끝난 항목 본문 비움(용량)");
  });

  await step("AI 실패(재시도 보고) → 다음 실행에서 다시 처리, 6번 연속 실패만 보류(stuck), 유실 없음", async () => {
    const fi = Array.from({ length: 100 }, (_, i) => ({ key: `f${i}`, hash: "h", source_id: "s", url: `https://f.org/${i}`, title: "t", body: { text: "t" } }));
    for (let i = 0; i < fi.length; i += 40) await q({ op: "add", items: fi.slice(i, i + 40) });
    for (let k = 0; k < 6; k++) {
      // f0~f9 는 계속 실패, 나머지는 첫 실행에서만 실패
      await runOnce(300, (x) => (x.key.match(/^f\d$/) || k === 0 ? { status: "retry", error: "AI 529" } : { status: "done" }));
    }
    const rows = sql("SELECT status, COUNT(*) AS n FROM pending WHERE key LIKE 'f%' GROUP BY status");
    const by = Object.fromEntries(rows.map((r) => [r.status, r.n]));
    assert.equal(by.done, 90, JSON.stringify(by));
    assert.equal(by.stuck, 10, JSON.stringify(by));
  });

  await step("수집 중단(보고 없이 종료) → 임대 만료 후 다음 실행이 같은 항목을 한 번만 처리", async () => {
    await q({ op: "add", items: Array.from({ length: 30 }, (_, i) => ({ key: `c${i}`, hash: "h", source_id: "s", url: `https://c.org/${i}`, title: "t", body: { text: "t" } })) });
    const dead = await runOnce(300, () => ({}));
    assert.equal(dead.got.length, 30);
    assert.equal((await runOnce(300, () => ({ status: "done" }))).got.length, 0, "임대 중에는 안 나옴");
    sql("UPDATE pending SET lease_until = '2000-01-01T00:00:00Z' WHERE key LIKE 'c%'");
    const r = await runOnce(300, () => ({ status: "done" }));
    assert.equal(r.accepted.length, 30);
    const late = await q({ op: "result", items: dead.got.map((x) => ({ key: x.key, status: "retry", lease_id: x.lease_id, hash: x.hash })) });
    assert.equal(late.rejected.length, 30, "죽었던 실행의 늦은 보고가 반영됨");
    assert.equal(sql("SELECT COUNT(*) AS n FROM pending WHERE key LIKE 'c%' AND status = 'done'")[0].n, 30);
  });

  await step("행사 1,020건 등록(30건씩 34회): 사람별 대기열 생성, 호출당 시간 기록", async () => {
    const t0 = Date.now();
    const evs = Array.from({ length: 1020 }, (_, i) => ev(`big${i}`));
    for (let i = 0; i < evs.length; i += 30) {
      const r = await admin("/admin/enqueue", { events: evs.slice(i, i + 30) });
      assert.equal(r.ok, true, JSON.stringify(r));
    }
    metrics.enqueue_1020_ms = Date.now() - t0;
    const n = sql("SELECT COUNT(*) AS n, COUNT(DISTINCT chat_id) AS people FROM outbox WHERE event_key LIKE 'big%'")[0];
    assert.equal(n.n, 2040, `대기열 ${n.n}`);
    assert.equal(n.people, 2);
    sql("UPDATE outbox SET status = 'skipped' WHERE event_key LIKE 'big%'"); // 이후 단계와 분리
  });

  await step("한 사람 몫 대기열 쓰기 실패 → 행사째 되돌림 → 재처리 시 두 사람 모두 생성", async () => {
    sql(`CREATE TRIGGER t_fail_friend BEFORE INSERT ON outbox WHEN NEW.chat_id = '${FRIEND}' BEGIN SELECT RAISE(ABORT, 'injected'); END`);
    let r;
    try { r = await admin("/admin/enqueue", { events: [ev("pp1", { urgency: "now" })] }); } catch (e) { r = { ok: false }; }
    sql("DROP TRIGGER t_fail_friend");
    assert.notEqual(r.ok, true);
    assert.equal(sql("SELECT COUNT(*) AS n FROM outbox WHERE event_key = 'pp1'")[0].n, 0, "한 사람 몫만 생김");
    r = await admin("/admin/enqueue", { events: [ev("pp1", { urgency: "now" })] });
    assert.deepEqual(sql("SELECT chat_id FROM outbox WHERE event_key = 'pp1' ORDER BY chat_id").map((x) => x.chat_id), [OWNER, FRIEND]);
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const got = E.calls.filter((c) => c.method === "sendMessage" && (c.payload.text || "").includes("행사 pp1")).map((c) => String(c.payload.chat_id)).sort();
    assert.deepEqual(got, [OWNER, FRIEND]);
  });

  await step("새 정보 없는 날: 같은 행사 재등록·발송·요약을 돌려도 텔레그램 발송 0건", async () => {
    await admin("/admin/dispatch", { ignoreQuiet: true });
    await admin("/admin/digest", { force: true });
    const before = sendCount();
    const r = await admin("/admin/enqueue", { events: [ev("pp1", { urgency: "now" }), ev("big1"), ev("big2")] });
    assert.equal(r.outbox, 0, JSON.stringify(r));
    await admin("/admin/dispatch", { ignoreQuiet: true });
    await admin("/admin/digest", {});
    await admin("/admin/digest", { force: true });
    assert.equal(sendCount() - before, 0, "새 정보가 없는데 보냄");
  });

  await step("운영 첫날 기준선: 쌓인 행사 60건 중 곧 행동할 것만 요약(즉시 알림 0), 나머지는 조용히 등록 → 나중에 바뀌면 정상 알림", async () => {
    const st = await admin("/admin/baseline", { hours: 24 });
    assert.equal(st.active, true);
    const evs = [
      ...Array.from({ length: 50 }, (_, i) => ev(`old${i}`, { start_date: plus(60 + i), urgency: "now" })),     // 먼 행사(즉시 알림 후보였던 것 포함)
      ...Array.from({ length: 5 }, (_, i) => ev(`soon${i}`, { start_date: plus(3 + i), urgency: "now" })),       // 3주 안
      ...Array.from({ length: 5 }, (_, i) => ev(`reg${i}`, { start_date: plus(90), registration_opens: plus(2 + i), registration_status: "upcoming" })), // 2주 안 접수
    ];
    const before = sendCount();
    let silent = 0, digestN = 0;
    for (let i = 0; i < evs.length; i += 30) {
      const r = await admin("/admin/enqueue", { events: evs.slice(i, i + 30) });
      silent += r.baseline_silent; digestN += r.baseline_digest;
    }
    assert.equal(silent, 50);
    assert.equal(digestN, 10);
    const kinds = sql("SELECT kind, COUNT(*) AS n FROM outbox WHERE event_key LIKE 'old%' OR event_key LIKE 'soon%' OR event_key LIKE 'reg%' GROUP BY kind");
    assert.deepEqual(kinds, [{ kind: "digest", n: 20 }], JSON.stringify(kinds));
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(sendCount() - before, 0, "기준선 중 즉시 알림 발송");
    await admin("/admin/digest", { force: true });
    const msgs = E.calls.slice(before).filter((c) => c.method === "sendMessage");
    assert.equal(msgs.length, 2, `요약 ${msgs.length}통(사람당 1통)`);
    assert.ok(msgs.every((m) => /soon0/.test(m.payload.text) && !/old\d/.test(m.payload.text)));
    await admin("/admin/baseline", { hours: 0 });
    const b2 = sendCount();
    await admin("/admin/enqueue", { events: [ev("old3", { start_date: plus(63), urgency: "now", cost_text: "$10 → 무료" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const changed = E.calls.slice(b2).filter((c) => c.method === "sendMessage" && /행사 old3/.test(c.payload.text || ""));
    assert.equal(changed.length, 2, "기준선 뒤 바뀐 행사는 정상 알림");
  });
  console.log("METRICS", JSON.stringify(metrics));
} finally {
  E.stop();
}
if (failed()) process.exit(1);
