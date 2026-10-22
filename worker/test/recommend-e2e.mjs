// 추천 판정에 따른 발송 선별(소유자 피드백 2026-10-22: 일상 봇에 시의회 회의·약물 반납·영유아·시니어 프로그램이 섞임).
// 수집·저장은 모두 하고, 발송은 '적극 알림'만 기본. '관심 설정 시'·'일반 제외'는 그 분야를 직접 고른 사람에게만.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const { step, failed } = stepper();
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "life", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(10), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "weekly", tags: ["t"], annual: false, source_id: "test", registration_status: "open", ...over,
});

const E = await startEnv({ tgPort: 9989, wPort: 8790, vars: {} });
const { admin, hook, msg, sql } = E;
const to = (n) => E.calls.slice(n).filter((c) => c.method === "sendMessage" && String(c.payload.chat_id) === OWNER);
const has = (list, key) => list.some((c) => new RegExp(`행사 ${key}\\b`).test(c.payload.text || ""));

try {
  await hook("life", msg(OWNER, "/start"));
  await hook("life", msg(OWNER, "/quiet 0 0"));

  await step("적극 알림만 기본 발송: 시의회 회의(제외)·약물 반납(제외)·영유아 동화(관심 설정 시)·판정 없는 행사는 저장만", async () => {
    const n = E.calls.length;
    const r = await admin("/admin/enqueue", { events: [
      ev("concert", { urgency: "now", rec_level: "push", category: "performance", rec_score: 80, summary_ko: "야외 재즈 공연과 푸드트럭", highlights: ["무료"] }),
      ev("council", { urgency: "now", rec_level: "none", category: "civic", title_ko: "행사 council 시의회 정례 회의" }),
      ev("takeback", { urgency: "now", rec_level: "none", category: "public-service" }),
      ev("storytime", { urgency: "now", rec_level: "interest", category: "toddler" }),
      ev("legacy", { urgency: "now" }),
    ] });
    assert.equal(r.accepted, 5, "수집한 행사는 모두 저장");
    assert.equal(sql("SELECT COUNT(*) AS n FROM events")[0].n, 5);
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const got = to(n);
    assert.ok(has(got, "concert"));
    for (const k of ["council", "takeback", "storytime", "legacy"]) assert.ok(!has(got, k), `${k} 가 발송됨`);
    const m = got.find((c) => /행사 concert/.test(c.payload.text));
    assert.match(m.payload.text, /🆓 무료\n야외 재즈 공연과 푸드트럭\n\n📅/);
  });

  await step("관심 분야를 고르면(/interests add toddler) 그 분야는 받고, 고르지 않은 회의는 여전히 받지 않음", async () => {
    let n = E.calls.length;
    await hook("life", msg(OWNER, "/interests add toddler"));
    assert.match(to(n)[0].payload.text, /✅ toddler/);
    n = E.calls.length;
    await admin("/admin/enqueue", { events: [
      ev("storytime2", { urgency: "now", rec_level: "interest", category: "toddler" }),
      ev("council2", { urgency: "now", rec_level: "none", category: "civic" }),
    ] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.ok(has(to(n), "storytime2") && !has(to(n), "council2"));
  });

  await step("하루 상한으로 알림 수를 줄여도 접수 시작 ⏰ 은 빠지지 않는다", async () => {
    await admin("/admin/enqueue", { events: [
      ...Array.from({ length: 12 }, (_, i) => ev(`many${i}`, { rec_level: "push", category: "class", rec_score: 30, start_date: plus(30 + i) })),
      ev("permit", { rec_level: "push", category: "outdoors", rec_score: 95, start_date: plus(60), registration_opens: denverToday, registration_status: "upcoming", highlights: ["허가 필요", "선착순"] }),
    ] });
    const n = E.calls.length;
    await admin("/admin/dispatch", { ignoreQuiet: true });
    await admin("/admin/digest", { force: true });
    const got = to(n);
    assert.ok(got.some((c) => /^⏰/.test(c.payload.text) && /행사 permit/.test(c.payload.text)), "접수 시작 ⏰ 누락");
    const singles = got.filter((c) => c.payload.reply_markup && /행사 many/.test(c.payload.text));
    assert.ok(singles.length <= 5, `하루 개별 알림 ${singles.length}통`);
    assert.ok(got.some((c) => /나머지 \d+건은 \/more/.test(c.payload.text)));
  });

  await step("재평가(/admin/recommend-update): 판·변경 알림 없이 추천 결과만 바뀌고, 이후 발송에 반영", async () => {
    const v0 = sql("SELECT version FROM events WHERE event_key = 'council'")[0].version;
    const n = E.calls.length;
    const r = await admin("/admin/recommend-update", { items: [{ event_key: "legacy", rec_level: "none", category: "civic", rec_reason: "행정 회의" },
      { event_key: "council", rec_level: "none", category: "civic", title_ko: "시의회 회의", summary_ko: "회의" }] });
    assert.equal(r.updated, 2);
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(to(n).length, 0, "재평가가 알림을 만듦");
    assert.equal(sql("SELECT version FROM events WHERE event_key = 'council'")[0].version, v0);
    assert.equal(JSON.parse(sql("SELECT data FROM events WHERE event_key = 'council'")[0].data).title_ko, "시의회 회의");
  });

  await step("소유자 시험 발송(/admin/preview): 실제 저장된 '적극 알림' 행사를 실제 형식으로, 버튼이 그 행사에 연결", async () => {
    const n = E.calls.length;
    const r = await admin("/admin/preview", { bot: "life", n: 3 });
    assert.equal(r.sent, 3, JSON.stringify(r));
    for (const c of to(n)) {
      const kb = c.payload.reply_markup.inline_keyboard;
      const key = kb[0][0].url.split("/").pop();
      assert.ok(c.payload.text.includes(`행사 ${key}`));
      assert.deepEqual(kb[1].map((b) => b.callback_data), [`ack:${key}`, `mute:${key}`]);
      assert.ok(!/https?:/.test(c.payload.text));
    }
  });
} finally {
  E.stop();
}
if (failed()) process.exit(1);
