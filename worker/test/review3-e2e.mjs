// 3차 검증 지적 재현 검사(Worker 쪽): 1) 행사 등록과 알림 대기열(outbox)의 원자성, 2) 대기열 임대 소유권.
// 수정 전 코드에서 실패하고, 수정 후 통과해야 한다.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const { step, failed } = stepper();
const FRIEND = "222";
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "kids", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(30), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "now", tags: ["t"], annual: false, source_id: "test", registration_status: "open", rec_level: "push", ...over,
});

const E = await startEnv({ tgPort: 9994, wPort: 8804, vars: { DELIVERY_MODE: "all", FAMILY_AVAILABLE_FROM: "2026-01-01" } });
const { admin, hook, msg, sql } = E;
const sends = (chat, key) => E.calls.filter((c) => c.method === "sendMessage" && String(c.payload.chat_id) === chat && (c.payload.text || "").includes(`행사 ${key}`)).length;
const outboxOf = (key) => sql(`SELECT chat_id, version, status FROM outbox WHERE event_key = '${key}' ORDER BY chat_id`);
// 알림 대기열 쓰기를 일부러 실패시키는 장치(로컬 D1 에만 거는 트리거. 운영 코드에는 시험용 분기가 없다)
const breakOutbox = () => sql("CREATE TRIGGER IF NOT EXISTS t_fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END");
const fixOutbox = () => sql("DROP TRIGGER IF EXISTS t_fail_outbox");
const tryEnqueue = async (events) => { try { return await admin("/admin/enqueue", { events }); } catch (e) { return { ok: false, error: String(e.message) }; } };

try {
  for (const who of [OWNER, FRIEND]) {
    await hook("kids", msg(who, "/start"));
    await hook("kids", msg(who, "/quiet 0 0"));
  }
  await hook("kids", msg(OWNER, `/approve ${FRIEND}`));

  // ---------------------------------------------------------------- 1. 원자성
  await step("1 행사 저장 중 알림 대기열 생성 실패 → 같은 행사 재처리 → 두 사람 알림 모두 복구", async () => {
    breakOutbox();
    const r1 = await tryEnqueue([ev("a1")]);
    fixOutbox();
    assert.notEqual(r1.ok, true, "주입한 실패가 보고되어야 함");
    const r2 = await tryEnqueue([ev("a1")]);
    assert.equal(r2.ok, true, JSON.stringify(r2));
    const rows = outboxOf("a1");
    assert.deepEqual(rows.map((r) => r.chat_id), [OWNER, FRIEND], `대기열: ${JSON.stringify(rows)}`);
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(sends(OWNER, "a1"), 1);
    assert.equal(sends(FRIEND, "a1"), 1);
  });

  await step("1 실패가 나도 행사만 저장되고 알림이 빠진 어중간한 상태가 남지 않는다(한 트랜잭션)", async () => {
    breakOutbox();
    const r = await tryEnqueue([ev("a2")]);
    fixOutbox();
    assert.notEqual(r.ok, true);
    const n = sql("SELECT COUNT(*) AS n FROM events WHERE event_key = 'a2'")[0].n;
    assert.equal(n, 0, "알림 대기열 없이 행사만 저장됨");
  });

  await step("1 이전 판에서 어긋난 상태(행사 저장, 한 사람 발송 완료, 다른 사람 대기열 없음) → 재처리 시 빠진 사람만 생성, 보낸 사람 재발송 없음", async () => {
    await tryEnqueue([ev("a3")]);
    // 수정 전 코드의 실패 결과를 그대로 만든다: 지인 몫 대기열이 없고, 행사는 '알림 생성 안 됨' 상태
    sql(`DELETE FROM outbox WHERE event_key = 'a3' AND chat_id = '${FRIEND}'`);
    sql("UPDATE events SET fanout_version = NULL WHERE event_key = 'a3'");
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(sends(OWNER, "a3"), 1);
    assert.equal(sends(FRIEND, "a3"), 0);
    const before = E.calls.length;
    const r = await tryEnqueue([ev("a3")]);
    assert.equal(r.ok, true, JSON.stringify(r));
    assert.equal(r.recovered, 1, `복구 건수 ${r.recovered}`);
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(sends(OWNER, "a3"), 1, "이미 받은 사람에게 다시 보냄");
    assert.equal(sends(FRIEND, "a3"), 1, "빠진 사람이 못 받음");
    assert.ok(E.calls.length > before);
  });

  await step("1 정상 재처리(내용 같음, 알림 생성 완료)는 아무것도 새로 만들지 않는다 + 나중에 가입한 사람에게 지난 행사를 몰아 보내지 않는다", async () => {
    await hook("kids", msg("333", "/start"));
    await hook("kids", msg(OWNER, "/approve 333"));
    const r = await tryEnqueue([ev("a1"), ev("a3")]);
    assert.equal(r.ok, true);
    assert.equal(r.outbox, 0, JSON.stringify(r));
    assert.equal(sql("SELECT COUNT(*) AS n FROM outbox WHERE chat_id = '333'")[0].n, 0);
  });

  // ---------------------------------------------------------------- 2. 임대 소유권
  const item = (key, hash = "h1") => ({ key, hash, source_id: "s", url: `https://x.org/${key}`, title: key, body: { text: "t" } });
  const take = (limit = 10, lease_minutes = 30) => admin("/admin/queue", { op: "take", limit, lease_minutes });
  const result = (items) => admin("/admin/queue", { op: "result", items });
  const row = (key) => sql(`SELECT status, attempts, result, lease_id FROM pending WHERE key = '${key}'`)[0];

  await step("2 take 는 항목마다 임대 ID 와 해시를 돌려준다", async () => {
    await admin("/admin/queue", { op: "add", items: [item("q1")] });
    const a = await take();
    assert.equal(a.items.length, 1);
    assert.ok(a.items[0].lease_id && a.items[0].hash === "h1", JSON.stringify(a.items[0]));
    await result([{ key: "q1", status: "release", lease_id: a.items[0].lease_id, hash: "h1" }]);
  });

  await step("2 A 임대 만료 → B 재확보 → A 가 늦게 결과 보고 → 거부되고 B 의 상태는 그대로, B 보고는 반영", async () => {
    const a = (await take()).items.find((x) => x.key === "q1");
    assert.ok(a, "A 가 q1 을 꺼냄");
    sql("UPDATE pending SET lease_until = '2000-01-01T00:00:00Z' WHERE key = 'q1'"); // A 의 임대 시간 경과
    const b = (await take()).items.find((x) => x.key === "q1");
    assert.ok(b && b.lease_id !== a.lease_id, "B 가 새 임대로 재확보");
    const late = await result([{ key: "q1", status: "extracted", result: [{ from: "A" }], lease_id: a.lease_id, hash: a.hash }]);
    assert.deepEqual(late.rejected, ["q1"], JSON.stringify(late));
    let r = row("q1");
    assert.equal(r.status, "queued");
    assert.equal(r.result, null, "A 의 늦은 결과가 B 의 항목을 덮어씀");
    assert.equal(r.lease_id, b.lease_id, "B 의 임대가 풀림");
    const ok = await result([{ key: "q1", status: "extracted", result: [{ from: "B" }], lease_id: b.lease_id, hash: b.hash }]);
    assert.equal(ok.updated, 1, JSON.stringify(ok));
    r = row("q1");
    assert.equal(r.status, "extracted");
    assert.equal(JSON.parse(r.result)[0].from, "B");
  });

  await step("2 임대 없이·다른 해시로·만료 뒤 보고하면 거부(아무도 다시 안 가져갔어도)", async () => {
    await admin("/admin/queue", { op: "add", items: [item("q2")] });
    const a = (await take()).items.find((x) => x.key === "q2");
    const noLease = await result([{ key: "q2", status: "retry", error: "x" }]);
    assert.deepEqual(noLease.rejected, ["q2"]);
    const wrongHash = await result([{ key: "q2", status: "retry", error: "x", lease_id: a.lease_id, hash: "other" }]);
    assert.deepEqual(wrongHash.rejected, ["q2"]);
    sql("UPDATE pending SET lease_until = '2000-01-01T00:00:00Z' WHERE key = 'q2'");
    const expired = await result([{ key: "q2", status: "retry", error: "x", lease_id: a.lease_id, hash: a.hash }]);
    assert.deepEqual(expired.rejected, ["q2"]);
    assert.equal(row("q2").attempts, 0);
  });

  await step("2 처리 중 내용이 바뀌어 다시 들어오면(해시 변경) 이전 임대의 보고는 거부", async () => {
    await admin("/admin/queue", { op: "add", items: [item("q3")] });
    const a = (await take()).items.find((x) => x.key === "q3");
    await admin("/admin/queue", { op: "add", items: [item("q3", "h2")] });
    const late = await result([{ key: "q3", status: "done", lease_id: a.lease_id, hash: a.hash }]);
    assert.deepEqual(late.rejected, ["q3"]);
    assert.equal(row("q3").status, "queued");
  });

  await step("2 임대 연장: 자기 임대만 연장되고, 연장하면 다른 실행이 가져가지 못한다", async () => {
    await admin("/admin/queue", { op: "add", items: [item("q4")] });
    const a = (await take(10, 5)).items.find((x) => x.key === "q4");
    const ext = await admin("/admin/queue", { op: "extend", minutes: 60, items: [{ key: "q4", lease_id: a.lease_id }, { key: "q4", lease_id: "nope" }] });
    assert.deepEqual(ext.extended, ["q4"], JSON.stringify(ext));
    const until = sql("SELECT lease_until FROM pending WHERE key = 'q4'")[0].lease_until;
    assert.ok(Date.parse(until) - Date.now() > 50 * 60000);
    const other = await take(50);
    assert.ok(!other.items.some((x) => x.key === "q4"));
  });

  await step("2 'extracted' 보고 뒤에도 임대를 유지해 같은 실행이 등록 후 'done' 을 보고할 수 있다", async () => {
    await admin("/admin/queue", { op: "add", items: [item("q5")] });
    const a = (await take()).items.find((x) => x.key === "q5");
    await result([{ key: "q5", status: "extracted", result: [{ x: 1 }], lease_id: a.lease_id, hash: a.hash }]);
    const other = await take(50);
    assert.ok(!other.items.some((x) => x.key === "q5"), "등록 전 다른 실행이 가져감");
    const done = await result([{ key: "q5", status: "done", lease_id: a.lease_id, hash: a.hash }]);
    assert.equal(done.updated, 1);
    assert.equal(row("q5").status, "done");
    assert.equal(row("q5").lease_id, null);
  });
} finally {
  E.stop();
}
if (failed()) process.exit(1);
