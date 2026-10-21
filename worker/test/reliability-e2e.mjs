// 1차 검증 지적 사항 재현 검사: 발송 실패·중복 실행(1-4), 접수 일정 재알림(1-5), 대기열 보존(3), 상세 재검사(1-3),
// 실행 감시(5), AI 중단 대응(4), 예산 경고(8), 요약 누락 방지(7).
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const FRIEND = "222";
const { step, failed } = stepper();
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "kids", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(30), end_date: null, time_text: "10:00",
  location: "Library", city: "Westminster", age_text: "6-10", english: [], cost_text: "$10", signup: "fcfs", registration_opens: null,
  registration_status: "open", deadline: null, eligibility: null, status_note: null, url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "now", tags: [key], annual: false, source_id: "test", ...over,
});

const E = await startEnv({ tgPort: 9993, wPort: 8803, vars: { DELIVERY_MODE: "all", FAMILY_AVAILABLE_FROM: "2026-01-01" } });
const { admin, hook, msg, since, sql } = E;
const sends = (n, re) => since(n).filter((c) => c.method === "sendMessage" && !c.failed && (!re || re.test(c.payload.text)));
const outbox = (key) => sql(`SELECT chat_id, version, status, attempts, resend, reason FROM outbox WHERE event_key = '${key}' ORDER BY id`);
const due = (key) => sql(`UPDATE outbox SET next_attempt_at = '2000-01-01T00:00:00Z' WHERE event_key = '${key}'`);

try {
  await hook("kids", msg(OWNER, "/start"));
  await hook("kids", msg(OWNER, "/quiet 0 0"));

  // ------------------------------------------------ 1-4 발송 실패·중복 실행
  await step("1-4 네트워크 오류 → '성공 여부 불명'으로 기록, 무조건 재전송하지 않고 10분 뒤 표시를 붙여 1회 재전송", async () => {
    await admin("/admin/enqueue", { events: [ev("net")] });
    E.failNext("network");
    let r = await admin("/admin/dispatch", {});
    assert.equal(r.uncertain, 1);
    assert.equal(outbox("net")[0].status, "uncertain");
    r = await admin("/admin/dispatch", {}); // 아직 10분 전: 다시 보내지 않음
    assert.equal(r.sent, 0);
    due("net");
    const n = E.calls.length;
    r = await admin("/admin/dispatch", {});
    assert.equal(r.sent, 1);
    assert.match(sends(n)[0].payload.text, /^🔁 재전송/);
    assert.equal(outbox("net")[0].status, "sent");
  });

  await step("1-4 429 → 대기 후 재시도, 500 → 백오프 재시도 후 성공", async () => {
    await admin("/admin/enqueue", { events: [ev("r429"), ev("r500")] });
    E.failNext(429, 500);
    let r = await admin("/admin/dispatch", {});
    assert.equal(r.failed, 2);
    assert.deepEqual([outbox("r429")[0].status, outbox("r500")[0].status], ["failed", "failed"]);
    due("r429"); due("r500");
    r = await admin("/admin/dispatch", {});
    assert.equal(r.sent, 2);
    assert.equal(outbox("r500")[0].attempts, 2);
  });

  await step("1-4 500 이 계속되면 재시도 한도 후 'dead' + 소유자에게 알림(조용히 사라지지 않음)", async () => {
    await admin("/admin/enqueue", { events: [ev("dead")] });
    for (let i = 0; i < 6; i++) { E.failNext(500); due("dead"); await admin("/admin/dispatch", {}); }
    const row = outbox("dead")[0];
    assert.equal(row.status, "dead");
    assert.match(row.reason, /재시도 한도/);
    assert.ok(E.calls.some((c) => c.method === "sendMessage" && /발송 실패\(재시도 중단\)/.test(c.payload.text || "")));
  });

  await step("1-4 403(사용자가 봇 차단) → 확실한 실패, 그 사람의 봇 구독을 blocked 로", async () => {
    await hook("kids", msg(FRIEND, "/start"));
    await hook("kids", msg(OWNER, `/approve ${FRIEND}`));
    await hook("kids", msg(FRIEND, "/quiet 0 0"));
    sql("UPDATE outbox SET status = 'skipped' WHERE status IN ('pending', 'failed', 'uncertain')");
    await admin("/admin/enqueue", { events: [ev("b403")] });
    const rows = outbox("b403");
    const order = rows.map((x) => x.chat_id);
    E.failNext(...order.map((c) => (c === FRIEND ? 403 : null)).filter(Boolean));
    // FRIEND 행이 먼저 오도록 OWNER 행을 잠시 미룬다
    sql(`UPDATE outbox SET next_attempt_at = '2999-01-01', status = 'failed' WHERE event_key = 'b403' AND chat_id = '${OWNER}'`);
    await admin("/admin/dispatch", {});
    assert.equal(outbox("b403").find((x) => x.chat_id === FRIEND).status, "dead");
    assert.equal(sql(`SELECT status FROM recipients WHERE chat_id = '${FRIEND}' AND bot = 'kids'`)[0].status, "blocked");
    sql(`UPDATE recipients SET status = 'active' WHERE chat_id = '${FRIEND}'`);
    sql(`UPDATE outbox SET status = 'skipped' WHERE event_key = 'b403'`);
  });

  await step("1-4 두 실행이 동시에 dispatch → 같은 메시지는 한 번만", async () => {
    await admin("/admin/enqueue", { events: [ev("c1"), ev("c2"), ev("c3")] });
    const n = E.calls.length;
    const [a, b] = await Promise.all([admin("/admin/dispatch", {}), admin("/admin/dispatch", {})]);
    const texts = sends(n, /행사 c\d/).map((c) => `${c.payload.chat_id}|${c.payload.text.split("\n")[0]}`);
    assert.equal(texts.length, new Set(texts).size, "중복 발송: " + texts.join(", "));
    assert.equal(a.sent + b.sent, 6); // 3건 × 2명
  });

  await step("1-4 'sending' 에서 멈춘 행(발송 후 기록 실패 상황) → 10분 뒤 불명 처리 → 표시 붙여 1회 재전송", async () => {
    await admin("/admin/enqueue", { events: [ev("stale")] });
    sql(`UPDATE outbox SET status = 'sending', claimed_at = '2000-01-01T00:00:00Z' WHERE event_key = 'stale'`);
    const n = E.calls.length;
    const r = await admin("/admin/dispatch", {});
    assert.ok(r.recovered >= 2);
    assert.equal(sends(n, /^🔁 재전송/).length, 2);
  });

  await step("이미 끝난 행사는 늦게라도 보내지 않음(expired), 같은 행사의 옛 판은 superseded", async () => {
    await admin("/admin/enqueue", { events: [ev("old", { start_date: plus(-3) })] });
    let r = await admin("/admin/dispatch", {});
    assert.equal(r.expired, 2);
    sql("UPDATE outbox SET status = 'skipped' WHERE status = 'pending'");
    await admin("/admin/enqueue", { events: [ev("ver", { cost_text: "$1" })] });
    await admin("/admin/enqueue", { events: [ev("ver", { cost_text: "$2" })] });
    const n = E.calls.length;
    r = await admin("/admin/dispatch", {});
    assert.equal(r.superseded, 2);
    assert.equal(sends(n, /행사 ver/).length, 2);
    assert.ok(sends(n, /행사 ver/).every((c) => /바뀐 점: 비용 \$1 → \$2/.test(c.payload.text)));
  });

  // ------------------------------------------------ 1-5 접수 일정
  await step("1-5 접수 시작일이 새로 확인되거나 바뀌면 중요 변경으로 재알림", async () => {
    await admin("/admin/enqueue", { events: [ev("reg", { registration_status: "upcoming" })] });
    await admin("/admin/dispatch", {});
    await hook("kids", E.cb(OWNER, "ack:reg", 1));
    const r = await admin("/admin/enqueue", { events: [ev("reg", { registration_status: "upcoming", registration_opens: plus(20) })] });
    assert.equal(r.changed, 1);
    const n = E.calls.length;
    await admin("/admin/dispatch", {});
    const s = sends(n, /행사 reg/).find((c) => String(c.payload.chat_id) === OWNER);
    assert.ok(s, "확인한 소유자에게도 재알림");
    assert.match(s.payload.text, /바뀐 점: 접수 시작 미확인 → /);
  });

  await step("1-5 오늘 접수 시작 → ⏰ 알림(확인한 사람 포함), 내일 마감 → 확인 안 한 사람에게만", async () => {
    await admin("/admin/enqueue", { events: [ev("opens", { registration_opens: denverToday, urgency: "weekly" }), ev("dl", { deadline: plus(1), urgency: "weekly" })] });
    await hook("kids", E.cb(OWNER, "ack:opens", 1));
    await hook("kids", E.cb(OWNER, "ack:dl", 1));
    const n = E.calls.length;
    await admin("/admin/dispatch", {});
    const opens = sends(n, /^⏰ 행사 opens/).map((c) => String(c.payload.chat_id)).sort();
    const dl = sends(n, /^⏰ 행사 dl/).map((c) => String(c.payload.chat_id));
    assert.deepEqual(opens, [FRIEND, OWNER].sort());
    assert.deepEqual(dl, [FRIEND]);
    const n2 = E.calls.length;
    await admin("/admin/dispatch", {});
    assert.equal(sends(n2, /^⏰/).length, 0, "같은 날 두 번 알리지 않음");
  });

  // ------------------------------------------------ 7 요약
  await step("7 요약 23건 → 10·10·3 세 통, 번호 이어짐, 누락 없음", async () => {
    sql("UPDATE outbox SET status = 'skipped' WHERE kind = 'digest'");
    await admin("/admin/enqueue", { events: Array.from({ length: 23 }, (_, i) => ev(`d${i}`, { urgency: "weekly", tags: ["dg"], start_date: plus(40 + i) })) });
    sql(`UPDATE outbox SET status = 'skipped' WHERE kind = 'digest' AND chat_id = '${FRIEND}'`);
    const n = E.calls.length;
    const r = await admin("/admin/digest", { force: true });
    assert.equal(r.messages, 3, JSON.stringify(r));
    assert.equal(r.items, 23);
    const t = sends(n, /오늘의 새 정보/).map((c) => c.payload.text);
    assert.match(t[0], /\(1\/3, 총 23건\)/);
    assert.match(t[2], /^23\. 행사 d22/m);
  });

  await step("7 요약 한 통이 실패하면 그 묶음만 남았다가 다음 실행에서 시간 조건 없이 이어 보냄", async () => {
    await admin("/admin/enqueue", { events: Array.from({ length: 12 }, (_, i) => ev(`f${i}`, { urgency: "weekly", tags: ["dg2"], start_date: plus(80 + i) })) });
    sql(`UPDATE outbox SET status = 'skipped' WHERE kind = 'digest' AND chat_id = '${FRIEND}'`);
    E.failNext(500);
    let r = await admin("/admin/digest", { force: true });
    assert.equal(r.messages, 1);
    assert.equal(r.failed, 1);
    sql("UPDATE outbox SET next_attempt_at = '2000-01-01T00:00:00Z' WHERE kind = 'digest' AND status = 'failed'");
    r = await admin("/admin/digest", {}); // force 없이
    assert.equal(r.items, 10, JSON.stringify(r));
  });

  // ------------------------------------------------ 3 대기열
  await step("3 대기열: 우선순위 순서로 꺼내고, 누락·실패는 재시도, 한도 넘으면 삭제 대신 보류(stuck)", async () => {
    const it = (k, p) => ({ key: k, hash: "h1", source_id: "s", url: `https://x.org/${k}`, title: k, body: { text: "t" }, priority: p });
    let r = await admin("/admin/queue", { op: "add", items: [it("q1", 0), it("q2", 5), it("q3", 1)] });
    assert.deepEqual(r.new.sort(), ["q1", "q2", "q3"]);
    r = await admin("/admin/queue", { op: "add", items: [it("q1", 0)] });
    assert.equal(r.written, 0, "같은 내용은 다시 넣지 않음");
    r = await admin("/admin/queue", { op: "take", limit: 2 });
    assert.deepEqual(r.items.map((x) => x.key), ["q2", "q3"]);
    const lease = (x) => ({ lease_id: x.lease_id, hash: x.hash });
    await admin("/admin/queue", { op: "result", items: [{ key: "q2", status: "extracted", result: [{ event_key: "e" }], ...lease(r.items[0]) }, { key: "q3", status: "retry", error: "missing-in-output", ...lease(r.items[1]) }] });
    E.sql("UPDATE pending SET lease_until = '2000-01-01T00:00:00Z' WHERE key = 'q2'"); // 등록 전에 실행이 멈춘 경우: 임대가 끝나면 AI 결과째 다시 나온다
    r = await admin("/admin/queue", { op: "take", limit: 5 });
    assert.equal(r.items[0].key, "q2");
    assert.deepEqual(r.items[0].result, [{ event_key: "e" }]);
    await admin("/admin/queue", { op: "result", items: r.items.map((x) => ({ key: x.key, status: "release", ...lease(x) })) });
    for (let i = 0; i < 6; i++) {
      const t = (await admin("/admin/queue", { op: "take", limit: 5 })).items.find((x) => x.key === "q1");
      await admin("/admin/queue", { op: "result", items: [{ key: "q1", status: "retry", error: "x", ...lease(t) }] });
      E.sql("UPDATE pending SET lease_id = NULL, lease_until = NULL WHERE key <> 'q1'");
    }
    const st = await admin("/admin/queue", { op: "stats" });
    assert.equal(st.stuck, 1);
    r = await admin("/admin/queue", { op: "add", items: [{ ...it("q1", 0), hash: "h2" }] });
    assert.deepEqual(r.changed, ["q1"], "내용이 바뀌면 보류 항목도 다시 대기열로");
    assert.equal((await admin("/admin/queue", { op: "stats" })).stuck, 0);
  });

  // ------------------------------------------------ 1-3 상세 재검사
  await step("1-3 접수 예정 행사는 상세 페이지 재검사 목록에 짧은 주기로 등록, 결과 기록 후 다음 검사 예약", async () => {
    await admin("/admin/enqueue", { events: [ev("w1", { registration_status: "upcoming", registration_opens: plus(5) }), ev("w2", { source_id: "gnews:x" })] });
    const w = sql("SELECT url, reg_state, interval_h FROM watch WHERE url IN ('https://example.org/w1','https://example.org/w2')");
    assert.deepEqual(w.map((x) => [x.reg_state, x.interval_h]), [["upcoming-soon", 6]], "뉴스 출처는 감시하지 않음");
    sql("UPDATE watch SET next_check = '2000-01-01T00:00:00Z' WHERE url = 'https://example.org/w1'");
    const d = await admin("/admin/watch", { op: "due" });
    assert.ok(d.items.some((x) => x.event_keys.includes("w1")));
    await admin("/admin/watch", { op: "update", items: [{ url: "https://example.org/w1", ok: true, fingerprint: "fp1" }] });
    const after = sql("SELECT fingerprint, next_check, interval_h FROM watch WHERE url = 'https://example.org/w1'")[0];
    assert.equal(after.interval_h, 6, "접수 임박 행사는 6시간 주기 유지");
    assert.equal(after.fingerprint, "fp1");
    assert.ok(after.next_check > new Date().toISOString());
  });

  // ------------------------------------------------ 5 실행 감시 · 4 AI 중단 · 8 예산
  await step("5 수집 성공 기록이 26시간 넘게 없으면 소유자에게 1회 알림(매시 감시·봇 사용 시 모두 확인)", async () => {
    await admin("/admin/run-log", { run_id: "ok1", completed: true, metrics: {} });
    assert.equal((await admin("/admin/watchdog")).stale, false);
    sql(`UPDATE kv SET v = '${new Date(Date.now() - 30 * 3600000).toISOString()}' WHERE k = 'last_collect_ok_at'`);
    const n = E.calls.length;
    const w = await admin("/admin/watchdog");
    assert.equal(w.stale, true);
    await hook("kids", msg(OWNER, "/settings"));
    assert.equal(sends(n, /매일 수집이 \d+시간째/).length, 1);
  });

  await step("4 AI 실패 → 중단 시작 기록, 복구 시 소유자에게 복구 알림", async () => {
    await admin("/admin/run-log", { run_id: "ai1", ai: { ok: false, error: "429" }, metrics: {} });
    const h = await admin("/admin/ai-health");
    assert.ok(h.failing_since);
    const n = E.calls.length;
    await admin("/admin/run-log", { run_id: "ai2", ai: { ok: true }, metrics: {} });
    assert.equal(sends(n, /AI 분석 복구/).length, 1);
    assert.equal((await admin("/admin/ai-health")).failing_since, null);
  });

  await step("4 검증 전 정보 안내는 지인 발송 모드에서도 소유자에게만, 하루 1회", async () => {
    const n = E.calls.length;
    const items = Array.from({ length: 25 }, (_, i) => ({ title: `Raw ${i}`, url: `https://westminsterco.gov/e/${i}`, source_id: "westminster-calendar" }));
    let r = await admin("/admin/unverified", { items, outage_hours: 40, pending: 25 });
    assert.equal(r.sent, 2);
    const s = sends(n, /검증 전 정보/);
    assert.ok(s.every((c) => String(c.payload.chat_id) === OWNER));
    r = await admin("/admin/unverified", { items, outage_hours: 41 });
    assert.equal(r.sent, 0);
  });

  await step("8 예산 50·80·100% 사전 경고 각 1회", async () => {
    const n = E.calls.length;
    const u = (usd) => admin("/admin/usage", { model: "claude-opus-5-5", input_tokens: 1, output_tokens: 1, usd, calls: 1, budget: 1, searches: 2, cache_read_tokens: 5 });
    await u(0.6); await u(0.25); await u(0.2); await u(0.01);
    const t = sends(n, /💰/).map((c) => c.payload.text);
    assert.equal(t.length, 3);
    assert.match(t[2], /예산 \$1 도달/);
    const row = (await admin("/admin/usage")).rows.find((r) => r.model === "claude-opus-5-5");
    assert.equal(row.searches, 8);
    assert.equal(row.cache_read_tokens, 20);
  });

  await step("6 정보원 상태: 연속 실패 누적, 성공하면 0", async () => {
    await admin("/admin/run-log", { run_id: "s1", metrics: {}, sources: [{ id: "a", ok: false, status: 403, error: "HTTP 403" }, { id: "b", ok: true, status: 200, items: 4, events: 2 }] });
    await admin("/admin/run-log", { run_id: "s2", metrics: {}, sources: [{ id: "a", ok: false, status: 403, error: "HTTP 403" }, { id: "b", ok: true, status: 200, items: 5, events: 1 }] });
    const h = Object.fromEntries((await admin("/admin/source-health")).sources.map((x) => [x.id, x]));
    assert.equal(h.a.consecutive_failures, 2);
    assert.equal(h.b.events_total, 3);
    assert.ok(h.b.last_ok_at && !h.a.last_ok_at);
  });
} finally {
  E.stop();
}
if (failed()) { console.log("\n--- wrangler log (tail) ---\n" + E.log().slice(-3000)); process.exit(1); }
console.log("\n모든 신뢰성 검사 통과");
