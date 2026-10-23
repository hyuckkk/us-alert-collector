// 추천 판정에 따른 발송 선별(소유자 피드백 2026-10-22: 일상 봇에 시의회 회의·약물 반납·영유아·시니어 프로그램이 섞임).
// 수집·저장은 모두 하고, 발송은 '적극 알림'만 기본. '관심 설정 시'·'일반 제외'는 그 분야를 직접 고른 사람에게만.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const { step, failed } = stepper();
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "life", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(10), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "weekly", tags: ["t"], annual: false, source_id: "test", registration_status: "open", special_reason: "시험용 특별 근거", ...over,
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
    assert.match(m.payload.text, /🆓 무료\n✨ <b>시험용 특별 근거<\/b>\n야외 재즈 공연과 푸드트럭\n\n📅/);
  });

  await step("특별한 근거가 없는 '적극 알림'은 보내지 않는다(Red Rocks 정가 공연·학제간 토론·일반 공연장 정가 공연 부정 사례)", async () => {
    const n = E.calls.length;
    await admin("/admin/enqueue", { events: [
      ev("redrocks", { urgency: "now", rec_level: "push", category: "performance", special_reason: "", title_ko: "행사 redrocks Red Rocks 정기 콘서트", cost_text: "$89.50" }),
      ev("seminar", { urgency: "now", rec_level: "push", category: "university", special_reason: "  ", title_ko: "행사 seminar 학제간 토론" }),
      ev("theater", { urgency: "now", rec_level: "push", category: "performance", special_reason: null, title_ko: "행사 theater 극장 정기 공연" }),
      ev("airshow", { urgency: "now", rec_level: "push", category: "outdoors", special_reason: "평소 비공개인 공군기지 비행장을 하루만 개방" }),
    ] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const got = to(n);
    for (const k of ["redrocks", "seminar", "theater"]) assert.ok(!has(got, k), `${k} 가 근거 없이 발송됨`);
    assert.ok(got.some((c) => /행사 airshow/.test(c.payload.text) && /✨ <b>평소 비공개인 공군기지 비행장을 하루만 개방<\/b>/.test(c.payload.text)));
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

  await step("추천된 행사는 모두 한 통씩, 접수 시작 ⏰ 도 따로 빠짐없이", async () => {
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
    assert.equal(singles.length, 12, `추천된 행사 개별 알림 ${singles.length}통(하루 건수 제한 없음)`);
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

  await step("대기열에 들어간 뒤 재평가로 '일반 제외'가 되면 보내기 직전에 걸러진다(요약·즉시 모두)", async () => {
    await admin("/admin/enqueue", { events: [ev("late1", { rec_level: "push", category: "class", start_date: plus(5) }), ev("late2", { urgency: "now", rec_level: "push", category: "class", start_date: plus(6) })] });
    sql("UPDATE outbox SET status = 'skipped' WHERE event_key LIKE 'many%' OR event_key = 'permit'");
    await admin("/admin/recommend-update", { items: [{ event_key: "late1", rec_level: "none", category: "civic" }, { event_key: "late2", rec_level: "none", category: "civic" }] });
    const n = E.calls.length;
    await admin("/admin/dispatch", { ignoreQuiet: true });
    await admin("/admin/digest", { force: true });
    assert.ok(!has(to(n), "late1") && !has(to(n), "late2"), "재평가로 제외된 행사가 발송됨");
  });

  await step("같은 행사를 표현만 다르게 다시 받으면 '변경'이 아니다(외부 검색 재입력), 실제로 바뀐 값은 변경 알림", async () => {
    await admin("/admin/enqueue", { events: [ev("blossoms", { urgency: "now", rec_level: "push", category: "festival", start_date: plus(30), time_text: "오후 4:30–9시", location: "Denver Botanic Gardens, York St.", cost_text: "$25" })] });
    const n = E.calls.length;
    let r = await admin("/admin/enqueue", { events: [ev("blossoms", { urgency: "now", rec_level: "push", category: "festival", start_date: plus(30), time_text: "4:30-9 p.m.", location: "Denver Botanic Gardens (York Street)", cost_text: "$25 per person" })] });
    assert.deepEqual([r.changed, r.same, r.outbox], [0, 1, 0], JSON.stringify(r));
    // 이번 입력에 비용 정보가 없을 뿐이면 기존 값을 지우지 않고 변경도 아니다
    r = await admin("/admin/enqueue", { events: [ev("blossoms", { urgency: "now", rec_level: "push", category: "festival", start_date: plus(30), time_text: "4:30-9 p.m.", location: "Denver Botanic Gardens (York Street)", cost_text: null })] });
    assert.deepEqual([r.changed, r.outbox], [0, 0], JSON.stringify(r));
    assert.equal(JSON.parse(sql("SELECT data FROM events WHERE event_key = 'blossoms'")[0].data).cost_text, "$25 per person");
    r = await admin("/admin/enqueue", { events: [ev("blossoms", { urgency: "now", rec_level: "push", category: "festival", start_date: plus(30), time_text: "4:30-9 p.m.", location: "Denver Botanic Gardens (York Street)", cost_text: "$30" })] });
    assert.equal(r.changed, 1, JSON.stringify(r));
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const ch = to(n).filter((c) => /🔄 변경/.test(c.payload.text) && /행사 blossoms/.test(c.payload.text));
    assert.equal(ch.length, 1);
    assert.match(ch[0].payload.text, /비용 \$25 per person → \$30/);
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
