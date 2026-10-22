// 운영 첫날 실제 문제 재현: 소유자가 봇에 /start 를 하지 못한(응답이 없던) 상태에서 수신자가 0명이라 알림이 하나도 가지 않았다.
// 수정: 발송 작업이 소유자를 봇 3개의 수신자로 스스로 등록하고, 처음 등록될 때 곧 행동할 행사만 일간 요약에 한 번 넣는다.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const { step, failed } = stepper();
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "kids", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(60), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "weekly", tags: ["t"], annual: false, source_id: "test", registration_status: "open", ...over,
});

const E = await startEnv({ tgPort: 9996, wPort: 8806, fresh: true, vars: { FAMILY_AVAILABLE_FROM: "2026-01-01" } });
const { admin, sql } = E;
const rows = (q) => sql(q);

try {
  await step("소유자가 /start 하지 않았어도 발송 작업이 봇 3개의 수신자로 등록한다", async () => {
    // 기준선 대기 상태에서 행사 등록(실제 운영 첫날과 같은 순서): 가까운 행사 1건, 먼 행사 1건
    await admin("/admin/enqueue", { events: [ev("near", { start_date: plus(5) }), ev("far", { start_date: plus(60) })] });
    assert.equal(rows("SELECT COUNT(*) AS n FROM recipients")[0].n, 0);
    const r = await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(r.owner_recipients.added, 3);
    const rec = rows("SELECT chat_id, bot, status FROM recipients ORDER BY bot");
    assert.deepEqual(rec.map((x) => [x.chat_id, x.bot, x.status]), [[OWNER, "family", "active"], [OWNER, "kids", "active"], [OWNER, "life", "active"]]);
  });

  await step("처음 등록될 때 곧 행동할 행사만 요약 대기열에 한 번 들어가고, 먼 행사는 쏟아내지 않는다", async () => {
    const ob = rows("SELECT event_key, kind FROM outbox WHERE chat_id = '" + OWNER + "' ORDER BY event_key");
    assert.deepEqual(ob.map((x) => [x.event_key, x.kind]), [["near", "digest"]]);
    const r = await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(r.owner_recipients.added, 0);
    assert.equal(rows("SELECT COUNT(*) AS n FROM outbox")[0].n, 1);
  });
} finally {
  E.stop();
}
if (failed()) process.exit(1);
