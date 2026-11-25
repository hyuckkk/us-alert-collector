// 맥북 추천 판정기(Codex) 경로: await_rec 저장 → /rec/ 전용 키로 꺼내기·등록·완료, 권한 제한, 대체 경로(오래 기다린 것만)
import assert from "node:assert/strict";
import { startEnv, stepper } from "./harness.mjs";

const REC = "r".repeat(40);
const { step, failed } = stepper();
const E = await startEnv({ tgPort: 9979, wPort: 8879, vars: { REC_RUNNER_KEY: REC } });
const { admin, sql } = E;
const rec = (path, body, key = REC) => fetch(`${E.W}${path}`, {
  method: "POST", headers: { authorization: `Bearer ${key}`, "content-type": "application/json" }, body: JSON.stringify(body),
}).then(async (r) => ({ status: r.status, ...(await r.json()) }));
const lease = (x) => ({ lease_id: x.lease_id, hash: x.hash });
const it = (k) => ({ key: k, hash: "h1", source_id: "s", url: `https://x.org/${k}`, title: k, body: { text: "원문 " + k }, priority: 0 });

try {
  await step("수집 실행이 추천 대기(await_rec)로 저장하면 일반 꺼내기에는 안 나오고 판정기 꺼내기에만 나온다", async () => {
    await admin("/admin/queue", { op: "add", items: [it("a1"), it("a2")] });
    const t = await admin("/admin/queue", { op: "take", limit: 5 });
    const r = await admin("/admin/queue", { op: "result", items: t.items.map((x) => ({ key: x.key, status: "await_rec", result: [{ event_key: "e-" + x.key }], ...lease(x) })) });
    assert.equal(r.updated, 2);
    assert.equal((await admin("/admin/queue", { op: "take", limit: 5 })).items.length, 0, "일반 꺼내기에는 없음");
    const st = await admin("/admin/queue", { op: "stats" });
    assert.equal(st.await_rec, 2);
    const g = await rec("/rec/queue", { op: "take_rec", limit: 5 });
    assert.equal(g.items.length, 2);
    assert.deepEqual(g.items.map((x) => x.result[0].event_key).sort(), ["e-a1", "e-a2"]);
    assert.equal(g.items[0].body.text.startsWith("원문"), true, "원문 본문을 함께 받음");
    // a1: 판정 성공 → 등록 → 완료 / a2: 실패 → 돌려놓기(상태 유지)
    const ev = { event_key: "e-a1", bot: "kids", title: "Ev", title_ko: "행사", url: "https://x.org/a1", start_date: "2099-01-01", source_id: "s",
      checked_at: "2026-11-25T00:00:00Z", urgency: "digest", tags: [], rec_level: "interest", category: "class" };
    const en = await rec("/rec/enqueue", { events: [ev] });
    assert.equal(en.ok, true);
    const a1 = g.items.find((x) => x.key === "a1"), a2 = g.items.find((x) => x.key === "a2");
    const res = await rec("/rec/queue", { op: "result", items: [{ key: "a1", status: "done", ...lease(a1) }, { key: "a2", status: "release", ...lease(a2) }] });
    assert.equal(res.updated, 2);
    assert.deepEqual(sql("SELECT key, status FROM pending WHERE key IN ('a1','a2') ORDER BY key").map((x) => x.status), ["done", "await_rec"]);
  });

  await step("판정기 키는 /rec/ 의 허용된 일만: 관리 경로·다른 대기열 작업·다른 상태 보고는 거부", async () => {
    assert.equal((await rec("/admin/queue", { op: "stats" })).status, 401);
    assert.equal((await rec("/rec/queue", { op: "add", items: [it("x")] })).status, 400);
    assert.equal((await rec("/rec/queue", { op: "take_rec" }, "wrong-key".repeat(5))).status, 401);
    const g = await rec("/rec/queue", { op: "take_rec", limit: 5 });
    const a2 = g.items.find((x) => x.key === "a2");
    const r = await rec("/rec/queue", { op: "result", items: [{ key: "a2", status: "extracted", result: [], ...lease(a2) }] });
    assert.equal(r.updated, 0, "extracted 같은 상태 바꾸기는 판정기에 허용하지 않음");
    await rec("/rec/queue", { op: "result", items: [{ key: "a2", status: "release", ...lease(a2) }] });
  });

  await step("대체 경로: older_than_minutes 를 주면 오래 기다린 것만, 돌려놓기는 대기 시작 시각을 바꾸지 않는다", async () => {
    assert.equal((await admin("/admin/queue", { op: "take_rec", older_than_minutes: 120, limit: 5 })).items.length, 0, "방금 들어온 것은 아직");
    sql("UPDATE pending SET updated_at = '2000-01-01T00:00:00Z' WHERE key = 'a2'");
    const g = await rec("/rec/queue", { op: "take_rec", limit: 5 });
    await rec("/rec/queue", { op: "result", items: [{ key: "a2", status: "release", ...lease(g.items[0]) }] });
    assert.equal(sql("SELECT updated_at FROM pending WHERE key = 'a2'")[0].updated_at, "2000-01-01T00:00:00Z");
    const old = await admin("/admin/queue", { op: "take_rec", older_than_minutes: 120, limit: 5 });
    assert.deepEqual(old.items.map((x) => x.key), ["a2"]);
  });
} finally {
  E.stop();
}
if (failed()) { console.log("\n--- wrangler log (tail) ---\n" + E.log().slice(-3000)); process.exit(1); }
console.log("\n모든 추천 판정기 검사 통과");
