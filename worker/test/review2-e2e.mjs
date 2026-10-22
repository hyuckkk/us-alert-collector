// 2차 검증 지적 재현 검사(Worker 쪽): A 대기열 60건 반복 반환, C 시간 알림 20건 제한, F 한 URL 의 여러 행사.
// 수정 전 코드에서 실패하고, 수정 후 통과해야 한다.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const { step, failed } = stepper();
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "kids", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(30), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "weekly", tags: ["t"], annual: false, source_id: "test", registration_status: "open", ...over,
});

const E = await startEnv({ tgPort: 9995, wPort: 8805, vars: { DELIVERY_MODE: "all", FAMILY_AVAILABLE_FROM: "2026-01-01" } });
const { admin, hook, msg, sql } = E;
try {
  await hook("kids", msg(OWNER, "/start"));
  await hook("kids", msg(OWNER, "/quiet 0 0"));

  await step("A 대기열 350건: 60건씩 반복해서 꺼내면 겹치지 않고 350건 전부 나온다", async () => {
    const items = Array.from({ length: 350 }, (_, i) => ({ key: `k${String(i).padStart(3, "0")}`, hash: "h", source_id: "s", url: `https://x.org/${i}`, title: `t${i}`, body: {}, priority: i % 7 }));
    for (let i = 0; i < items.length; i += 40) await admin("/admin/queue", { op: "add", items: items.slice(i, i + 40) });
    const got = [];
    for (let i = 0; i < 8; i++) {
      const r = await admin("/admin/queue", { op: "take", limit: 60 });
      if (!r.items.length) break;
      got.push(...r.items.map((x) => x.key));
    }
    assert.equal(new Set(got).size, got.length, "같은 항목이 두 번 나옴");
    assert.equal(got.length, 350, `꺼낸 건수 ${got.length}`);
  });

  await step("A 동시에 두 실행이 꺼내도 서로 다른 항목(같은 항목 중복 AI 판정 없음)", async () => {
    sql("UPDATE pending SET lease_until = NULL, lease_id = NULL");
    const [a, b] = await Promise.all([admin("/admin/queue", { op: "take", limit: 60 }), admin("/admin/queue", { op: "take", limit: 60 })]);
    const ka = new Set(a.items.map((x) => x.key));
    assert.equal(b.items.filter((x) => ka.has(x.key)).length, 0);
    assert.equal(a.items.length + b.items.length, 120);
  });

  await step("A 처리 결과를 보고하면 임대가 풀리고, 보고 없이 임대 시간이 지나면 다시 꺼낼 수 있다", async () => {
    sql("UPDATE pending SET lease_until = NULL, lease_id = NULL");
    const r = await admin("/admin/queue", { op: "take", limit: 2 });
    await admin("/admin/queue", { op: "result", items: [{ key: r.items[0].key, status: "retry", error: "x", lease_id: r.items[0].lease_id, hash: r.items[0].hash }] });
    const again = await admin("/admin/queue", { op: "peek", limit: 400 });
    assert.ok(again.items.some((x) => x.key === r.items[0].key), "보고된 항목은 다시 대기");
    sql(`UPDATE pending SET lease_until = '2000-01-01T00:00:00Z' WHERE key = '${r.items[1].key}'`);
    const r2 = await admin("/admin/queue", { op: "take", limit: 400 });
    assert.ok(r2.items.some((x) => x.key === r.items[1].key), "임대가 끝난 항목은 다시 꺼냄");
  });

  await step("C 같은 날 접수 시작 120건: dispatch 를 거듭하면 120건 모두 ⏰ 알림 대기열에, 중복 없음", async () => {
    sql("DELETE FROM outbox");
    const evs = Array.from({ length: 120 }, (_, i) => ev(`r${i}`, { registration_opens: denverToday }));
    for (let i = 0; i < evs.length; i += 30) await admin("/admin/enqueue", { events: evs.slice(i, i + 30) });
    sql("UPDATE outbox SET status = 'skipped'");
    for (let i = 0; i < 6; i++) await admin("/admin/dispatch", { limit: 1 });
    const n = sql("SELECT COUNT(DISTINCT event_key) AS n, COUNT(*) AS rows FROM outbox WHERE kind = 'now' AND status != 'skipped' OR (kind = 'now' AND status = 'sent')")[0];
    const trig = sql("SELECT COUNT(*) AS n FROM events WHERE change_note = '⏰ 오늘 접수 시작'")[0].n;
    assert.equal(trig, 120, `알림이 만들어진 행사 ${trig}건`);
    assert.equal(n.n, n.rows, "같은 행사에 알림 중복");
  });

  await step("H 연도를 추정한 날짜로는 ⏰ 알림을 만들지 않고, 본문에 '연도 추정' 표시", async () => {
    sql("UPDATE outbox SET status = 'skipped' WHERE status IN ('pending', 'failed', 'uncertain')"); // 앞 단계의 알림 120건 정리
    await admin("/admin/enqueue", { events: [ev("u1", { registration_opens: denverToday, date_uncertain: true, urgency: "now" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const t = sql("SELECT COUNT(*) AS n FROM trigger_log WHERE event_key = 'u1'")[0].n;
    assert.equal(t, 0);
    const sent = E.calls.filter((c) => c.method === "sendMessage" && /행사 u1/.test(c.payload.text || ""));
    assert.ok(sent.length >= 1 && sent.every((c) => /연도 추정/.test(c.payload.text)));
  });

  await step("F 한 페이지(URL)에 두 행사가 있으면 감시 목록이 두 행사를 모두 기억한다", async () => {
    const url = "https://example.org/programs";
    await admin("/admin/enqueue", { events: [ev("pA", { url, registration_status: "upcoming", registration_opens: plus(5) }), ev("pB", { url, registration_status: "upcoming", registration_opens: plus(6) })] });
    const w = sql(`SELECT * FROM watch WHERE url = '${url}'`)[0];
    const keys = JSON.parse(w.event_keys || JSON.stringify([w.event_key]));
    assert.deepEqual(keys.sort(), ["pA", "pB"]);
  });

  await step("F 그 페이지에서 한 행사만 바뀌면 그 행사만 새 판, 다른 행사는 그대로", async () => {
    const url = "https://example.org/programs";
    const r = await admin("/admin/enqueue", { events: [ev("pA", { url, registration_status: "upcoming", registration_opens: plus(5), cost_text: "$30" }),
      ev("pB", { url, registration_status: "upcoming", registration_opens: plus(6) })] });
    assert.equal(r.changed, 1);
    assert.equal(r.same, 1);
    const v = Object.fromEntries(sql("SELECT event_key, version FROM events WHERE event_key IN ('pA','pB')").map((x) => [x.event_key, x.version]));
    assert.deepEqual(v, { pA: 2, pB: 1 });
  });
} finally {
  E.stop();
}
if (failed()) { console.log("\n--- wrangler log (tail) ---\n" + E.log().slice(-2000)); process.exit(1); }
console.log("\n모든 2차 지적 재현 검사 통과");
