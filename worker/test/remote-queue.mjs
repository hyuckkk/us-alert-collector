// 실제 Cloudflare D1 검사(3차 검증 5): 실행마다 새로 만든 시험용 Worker·D1(us-alert-*-stg-<실행ID>)에 대기열 1,200건·행사 1,020건을 넣고 나눠 처리한다.
// staging-test 워크플로에서만 실행. 봇 토큰이 없고 TG_API 가 닫힌 주소라 텔레그램으로는 아무것도 나가지 않는다.
// 사용: STAGING_URL=... STAGING_KEY=... STG_DB=us-alert-db-stg-<실행ID>-<시도> node test/remote-queue.mjs
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";

const W = process.env.STAGING_URL, KEY = process.env.STAGING_KEY;
if (!W || !KEY) throw new Error("STAGING_URL / STAGING_KEY required");
const N = 1200;
// 재시도는 일부러 하지 않는다: take 응답이 끊긴 뒤 다시 부르면 임대된 항목이 30분 묶여 '유실'처럼 보이고, result 재전송은 거부로 집계가 흐려진다.
// 오류는 모두 errors 에 남긴다(retries 는 항상 0).
const T0 = Date.now();
const out = { at: new Date().toISOString(), steps: [], timings: {}, requests: 0, errors: [], retries: 0, queue: {}, events: {} };
const post = async (path, body) => {
  const t = Date.now();
  out.requests++;
  let r, j;
  try {
    r = await fetch(`${W}${path}`, { method: "POST", headers: { authorization: `Bearer ${KEY}`, "content-type": "application/json" }, body: JSON.stringify(body) });
    j = await r.json();
  } catch (e) {
    out.errors.push(`${path}${body.op ? ":" + body.op : ""} ${e.name}: ${String(e.message).slice(0, 120)}`);
    throw e;
  }
  (out.timings[path + (body.op ? ":" + body.op : "")] ||= []).push(Date.now() - t);
  if (!r.ok || j.ok === false) {
    out.errors.push(`${path}${body.op ? ":" + body.op : ""} HTTP ${r.status} ${JSON.stringify(j).slice(0, 160)}`);
    throw new Error(`${path} ${r.status} ${JSON.stringify(j).slice(0, 300)}`);
  }
  return j;
};
const DB = process.env.STG_DB, CONFIG = process.env.STG_CONFIG || "wrangler.staging.toml";
if (!DB || !/-stg-/.test(DB)) throw new Error("STG_DB must be the per-run staging database name"); // 운영 DB 이름으로는 실행하지 않음
const sql = (command) => JSON.parse(execFileSync("npx", ["wrangler", "d1", "execute", DB, "--remote", "--json", "--config", CONFIG, "--command", command]).toString())[0].results;
const step = async (name, fn) => {
  try { await fn(); out.steps.push({ name, ok: true }); console.log("✔", name); } catch (e) { out.steps.push({ name, ok: false, error: e.message }); console.log("✘", name, "\n ", e.message); }
};
const q = (b) => post("/admin/queue", b);
async function runOnce(limit, decide) {
  const got = [];
  while (got.length < limit) {
    const r = await q({ op: "take", limit: Math.min(60, limit - got.length), lease_minutes: 30 });
    if (!r.items.length) break;
    got.push(...r.items);
  }
  const reports = got.map((x) => ({ ...decide(x), key: x.key, lease_id: x.lease_id, hash: x.hash })).filter((x) => x.status);
  const accepted = [];
  for (let i = 0; i < reports.length; i += 100) {
    const r = await q({ op: "result", items: reports.slice(i, i + 100) });
    accepted.push(...reports.slice(i, i + 100).filter((x) => !r.rejected.includes(x.key)));
  }
  return { got, accepted };
}

await step(`실제 D1 대기열 ${N}건 보존·분할 처리(중간 실행 중단 포함), 유실·중복 0`, async () => {
  const items = Array.from({ length: N }, (_, i) => ({ key: `p${i}`, hash: "h", source_id: `s${i % 20}`, url: `https://x.org/${i}`, title: `t${i}`, body: { text: "x".repeat(2000) }, priority: i % 5 }));
  for (let i = 0; i < N; i += 40) await q({ op: "add", items: items.slice(i, i + 40) });
  assert.equal((await q({ op: "stats" })).queued, N);
  const done = new Map();
  const mark = (acc) => acc.filter((x) => x.status === "done").forEach((x) => done.set(x.key, (done.get(x.key) || 0) + 1));
  let n = 0;
  const r1 = await runOnce(300, () => (n++ < 250 ? { status: "done" } : {}));   // 실행 1: 250건 처리 후 중단(50건 보고 없음)
  mark(r1.accepted);
  const dead = r1.got.slice(250);
  const r2 = await runOnce(300, () => ({ status: "done" }));                    // 실행 2: 임대 중인 50건은 받지 않아야 함
  mark(r2.accepted);
  const overlap = r2.got.filter((x) => dead.some((d) => d.key === x.key)).length;
  sql("UPDATE pending SET lease_until = '2000-01-01T00:00:00Z' WHERE status = 'queued' AND lease_id IS NOT NULL"); // 실행 1 의 임대 만료
  let runs = 2, retaken = 0;
  for (let i = 0; i < 10; i++) {
    const r = await runOnce(300, () => ({ status: "done" }));
    if (!r.got.length) break;
    retaken += r.got.filter((x) => dead.some((d) => d.key === x.key)).length;
    mark(r.accepted);
    runs++;
  }
  // 죽었던 실행 1 이 늦게 보고 → 모두 거부되어야 함
  const late = await q({ op: "result", items: dead.map((x) => ({ key: x.key, status: "retry", error: "late", lease_id: x.lease_id, hash: x.hash })) });
  const st = await q({ op: "stats" });
  const dbDone = sql("SELECT COUNT(*) AS n, SUM(attempts) AS att FROM pending WHERE status = 'done'")[0];
  out.queue = { added: N, processed_unique: done.size, duplicates: [...done.values()].filter((v) => v > 1).length, lost: N - done.size,
    runs, leased_overlap_while_alive: overlap, retaken_after_expiry: retaken, late_reports: dead.length, late_rejected: late.rejected.length,
    db_done: dbDone.n, db_attempts_sum: dbDone.att, stats_after: st.counts };
  assert.equal(overlap, 0, "임대 중 항목을 다른 실행이 받음");
  assert.equal(retaken, dead.length, "임대 만료 항목 재처리 누락");
  assert.equal(late.rejected.length, dead.length, "늦은 보고가 반영됨");
  assert.equal(done.size, N);
  assert.ok([...done.values()].every((v) => v === 1));
  assert.equal(dbDone.n, N);
  assert.equal(dbDone.att, 0, "늦은 보고가 시도 횟수를 바꿈");
});

await step("실제 D1 행사 1,020건 등록(30건씩) — 호출당 시간·D1 제한 확인", async () => {
  const today = new Date().toISOString().slice(0, 10);
  for (let i = 0; i < 1020; i += 30) {
    await post("/admin/enqueue", { events: Array.from({ length: 30 }, (_, j) => ({ event_key: `big${i + j}`, bot: "kids", title: `E${i + j}`, url: `https://example.org/${i + j}`,
      start_date: today, tags: ["t"], source_id: "staging", registration_status: "open" })) });
  }
  const c = sql("SELECT COUNT(*) AS n, COUNT(DISTINCT event_key) AS k, (SELECT COUNT(*) FROM items) AS items, (SELECT COUNT(*) FROM watch) AS watch, (SELECT COUNT(*) FROM outbox) AS outbox FROM events")[0];
  out.events = { sent: 1020, stored: c.n, distinct: c.k, items: c.items, watch: c.watch, outbox: c.outbox };
  assert.equal(c.n, 1020);
  assert.equal(c.k, 1020);
  assert.equal(c.outbox, 0, "시험 Worker 에 수신자가 없으므로 알림 대기열 0");
});

for (const [k, v] of Object.entries(out.timings)) out.timings[k] = { calls: v.length, avg_ms: Math.round(v.reduce((a, b) => a + b, 0) / v.length), max_ms: Math.max(...v) };
out.duration_ms = Date.now() - T0;
writeFileSync("remote-queue.json", JSON.stringify(out, null, 1));
console.log(JSON.stringify(out.timings));
// 산출물 내려받기가 막힌 환경에서도 볼 수 있게 요약을 주석(annotation)으로 남긴다(주소·키 없음)
const brief = { steps: out.steps.map((x) => `${x.ok ? "OK" : "FAIL"} ${x.name}${x.error ? " — " + x.error.slice(0, 200) : ""}`), duration_ms: out.duration_ms,
  requests: out.requests, retries: out.retries, errors: out.errors, queue: out.queue, events: out.events };
console.log(`::notice title=remote-queue result::${JSON.stringify(brief)}`);
console.log(`::notice title=remote-queue timings::${JSON.stringify(out.timings)}`);
if (out.steps.some((s) => !s.ok)) process.exit(1);
