// 실제 Cloudflare D1 검사(3차 검증 5): 시험용 Worker·D1(us-alert-*-staging)에 대기열 1,200건·행사 1,020건을 넣고 나눠 처리한다.
// staging-test 워크플로에서만 실행. 봇 토큰이 없고 TG_API 가 닫힌 주소라 텔레그램으로는 아무것도 나가지 않는다.
// 사용: STAGING_URL=... STAGING_KEY=... node test/remote-queue.mjs
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";

const W = process.env.STAGING_URL, KEY = process.env.STAGING_KEY;
if (!W || !KEY) throw new Error("STAGING_URL / STAGING_KEY required");
const N = 1200;
const out = { at: new Date().toISOString(), steps: [], timings: {} };
const post = async (path, body) => {
  const t = Date.now();
  const r = await fetch(`${W}${path}`, { method: "POST", headers: { authorization: `Bearer ${KEY}`, "content-type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json();
  (out.timings[path + (body.op ? ":" + body.op : "")] ||= []).push(Date.now() - t);
  if (!r.ok || j.ok === false) throw new Error(`${path} ${r.status} ${JSON.stringify(j).slice(0, 300)}`);
  return j;
};
const sql = (command) => JSON.parse(execFileSync("npx", ["wrangler", "d1", "execute", "us-alert-db-staging", "--remote", "--json", "--config", "wrangler.staging.toml", "--command", command]).toString())[0].results;
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
  mark((await runOnce(300, () => (n++ < 250 ? { status: "done" } : {}))).accepted);
  sql("UPDATE pending SET lease_until = '2000-01-01T00:00:00Z' WHERE status = 'queued' AND lease_id IS NOT NULL");
  for (let i = 0; i < 10; i++) {
    const r = await runOnce(300, () => ({ status: "done" }));
    if (!r.got.length) break;
    mark(r.accepted);
  }
  assert.equal(done.size, N);
  assert.ok([...done.values()].every((v) => v === 1));
});

await step("실제 D1 행사 1,020건 등록(30건씩) — 호출당 시간·D1 제한 확인", async () => {
  const today = new Date().toISOString().slice(0, 10);
  for (let i = 0; i < 1020; i += 30) {
    await post("/admin/enqueue", { events: Array.from({ length: 30 }, (_, j) => ({ event_key: `big${i + j}`, bot: "kids", title: `E${i + j}`, url: `https://example.org/${i + j}`,
      start_date: today, tags: ["t"], source_id: "staging", registration_status: "open" })) });
  }
  assert.equal(sql("SELECT COUNT(*) AS n FROM events")[0].n, 1020);
});

for (const [k, v] of Object.entries(out.timings)) out.timings[k] = { calls: v.length, avg_ms: Math.round(v.reduce((a, b) => a + b, 0) / v.length), max_ms: Math.max(...v) };
writeFileSync("remote-queue.json", JSON.stringify(out, null, 1));
console.log(JSON.stringify(out.timings));
if (out.steps.some((s) => !s.ok)) process.exit(1);
