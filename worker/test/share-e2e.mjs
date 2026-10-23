// 지인 공유 실제 시나리오: 가입 → 승인 → 첫 요약(쌓인 행사를 쏟아내지 않음) → 거리 판정 → 알림 수신 → 버튼 → 구독 해지 → 재신청.
// 운영은 소유자 전용이지만, 지인 발송을 켰을 때의 동작을 이 시험에서만 DELIVERY_MODE=all 로 확인한다.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const { step, failed } = stepper();
const FRIEND = "222";
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "kids", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(30), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "weekly", tags: ["t"], annual: false, source_id: "test", registration_status: "open", rec_level: "push", special_reason: "시험용 특별 근거", ...over,
});

const E = await startEnv({ tgPort: 9999, wPort: 8809, vars: { DELIVERY_MODE: "all" } });
const { admin, hook, msg, sql } = E;
const to = (n, id) => E.calls.slice(n).filter((c) => c.method === "sendMessage" && String(c.payload.chat_id) === id);
const has = (list, key) => list.some((c) => new RegExp(`행사 ${key}\\b`).test(c.payload.text || ""));

try {
  await step("가입 전에 쌓인 행사: 먼 미래 행사 40건 + 2주 안 접수 시작 3건 + 2주 안 마감 1건 + 한 달 뒤 접수 1건", async () => {
    await hook("kids", msg(OWNER, "/start"));
    const evs = [
      ...Array.from({ length: 40 }, (_, i) => ev(`old${i}`, { start_date: plus(20 + i) })),
      ...Array.from({ length: 3 }, (_, i) => ev(`reg${i}`, { start_date: plus(60), registration_opens: plus(3 + i), registration_status: "upcoming" })),
      ev("dead0", { start_date: plus(50), deadline: plus(5) }),
      ev("later0", { start_date: plus(90), registration_opens: plus(30), registration_status: "upcoming" }),
    ];
    for (let i = 0; i < evs.length; i += 30) await admin("/admin/enqueue", { events: evs.slice(i, i + 30) });
  });

  await step("지인 /start → 소유자에게 신청 알림, 지인은 승인 대기", async () => {
    const n = E.calls.length;
    await hook("kids", msg(FRIEND, "/start"));
    assert.match(to(n, FRIEND)[0].payload.text, /승인 후 알림이 옵니다/);
    assert.ok(to(n, OWNER).some((c) => /새 수신 신청/.test(c.payload.text) && c.payload.text.includes(`/approve ${FRIEND}`)));
    assert.equal(sql(`SELECT status FROM recipients WHERE chat_id = '${FRIEND}'`)[0].status, "pending");
  });

  await step("소유자 승인 → 지인에게 승인 안내, 첫 요약은 2주 안 접수 시작·마감 4건만(먼 행사 40건·한 달 뒤 접수는 넣지 않음)", async () => {
    const n = E.calls.length;
    await hook("kids", msg(OWNER, `/approve ${FRIEND}`));
    assert.ok(to(n, OWNER).some((c) => /첫 요약 4건/.test(c.payload.text)), JSON.stringify(to(n, OWNER).map((c) => c.payload.text)));
    assert.ok(to(n, FRIEND).some((c) => /승인되었습니다/.test(c.payload.text)));
    const q = sql(`SELECT event_key FROM outbox WHERE chat_id = '${FRIEND}' ORDER BY event_key`).map((x) => x.event_key);
    assert.deepEqual(q, ["dead0", "reg0", "reg1", "reg2"]);
    await hook("kids", msg(FRIEND, "/quiet 0 0"));
    const m = E.calls.length;
    await admin("/admin/digest", { force: true });
    const got = to(m, FRIEND);
    assert.equal(got.length, 4, `주요 행사는 한 건씩: ${got.map((c) => c.payload.text.split("\n")[0]).join(" | ")}`);
    assert.ok(["dead0", "reg0", "reg1", "reg2"].every((k) => has(got, k)) && !has(got, "old0"));
  });

  await step("/city Westminster 30: 먼 일반 행사는 받지 않고, 먼 곳이라도 국립공원 허가·추첨·근거가 확인된 희귀 기회는 받는다('연례'·'축제'·'예약' 글자만으로는 예외 아님, #22)", async () => {
    const n0 = E.calls.length;
    await hook("kids", msg(FRIEND, "/city Westminster 30"));
    assert.match(to(n0, FRIEND)[0].payload.text, /기준 도시: Westminster, 30마일/);
    await hook("kids", msg(FRIEND, "/city Springfield 30"));
    assert.match(E.calls.slice(-1)[0].payload.text, /목록에 없는 도시/);
    const n = E.calls.length;
    await admin("/admin/enqueue", { events: [
      ev("near1", { urgency: "now", city: "Thornton", location: "Thornton Community Center" }),
      ev("far1", { urgency: "now", city: "Grand Junction", location: "Grand Junction, CO" }),
      ev("farGeneral2", { urgency: "now", location: "Lincoln Park, Grand Junction, CO 81501" }),
      ev("permit1", { urgency: "now", city: "Estes Park", title: "Timed entry permit reservations open", tags: ["permit"] }),
      ev("annual1", { urgency: "now", city: "Durango", annual: true }),
      ev("fest1", { urgency: "now", city: "Durango", title: "Fall Festival - reservation recommended", tags: ["festival", "reservation"] }),
      ev("rare1", { urgency: "now", city: "Colorado Springs", special_basis: "restricted_access" }),
      ev("nocity1", { urgency: "now", location: "Online" }),
    ] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const got = to(n, FRIEND);
    assert.ok(has(got, "near1") && has(got, "permit1") && has(got, "rare1") && has(got, "nocity1"), got.map((c) => c.payload.text.split("\n")[0]).join(" | "));
    assert.ok(!has(got, "far1") && !has(got, "farGeneral2") && !has(got, "annual1") && !has(got, "fest1"), "먼 일반 행사가 감");
    assert.ok(has(to(n, OWNER), "far1"), "소유자(기준 도시 없음)는 모두 받음");
  });

  await step("지인이 버튼 ✅ 확인 → 기록되고 그 메시지 버튼이 바뀜", async () => {
    const m = to(0, FRIEND).find((c) => /행사 near1/.test(c.payload.text));
    const key = m.payload.reply_markup.inline_keyboard[1][0].callback_data.split(":")[1];
    const n = E.calls.length;
    await hook("kids", E.cb(FRIEND, `ack:${key}`, 901, m.payload.reply_markup));
    assert.equal(sql(`SELECT COUNT(*) AS n FROM clicks WHERE chat_id = '${FRIEND}' AND item_key = '${key}' AND action = 'ack'`)[0].n, 1);
    assert.ok(E.calls.slice(n).some((c) => c.method === "editMessageReplyMarkup"));
  });

  await step("지인 /stop → 이후 알림 없음, 남은 대기열은 건너뜀, 소유자에게 알림", async () => {
    await admin("/admin/enqueue", { events: [ev("pre1", { urgency: "weekly", start_date: plus(10) })] });
    const n = E.calls.length;
    await hook("kids", msg(FRIEND, "/stop"));
    assert.match(to(n, FRIEND)[0].payload.text, /더 보내지 않습니다/);
    assert.ok(to(n, OWNER).some((c) => /수신 중단/.test(c.payload.text)));
    await admin("/admin/enqueue", { events: [ev("after1", { urgency: "now" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    await admin("/admin/digest", { force: true });
    const got = to(n + 2, FRIEND);
    assert.ok(!has(got, "after1") && !has(got, "pre1"), "중단 뒤 발송됨");
    assert.equal(sql(`SELECT status FROM recipients WHERE chat_id = '${FRIEND}'`)[0].status, "stopped");
  });

  await step("지인 /start 다시 → 승인 없이 바로 재개, 2주 안 접수 정보만 다시 안내, 새 알림 정상 수신", async () => {
    const n = E.calls.length;
    await hook("kids", msg(FRIEND, "/start"));
    assert.match(to(n, FRIEND)[0].payload.text, /다시 받습니다/);
    assert.ok(to(n, OWNER).some((c) => /수신 재개/.test(c.payload.text)));
    assert.equal(sql(`SELECT status FROM recipients WHERE chat_id = '${FRIEND}'`)[0].status, "active");
    await admin("/admin/enqueue", { events: [ev("back1", { urgency: "now", city: "Westminster" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.ok(has(to(n, FRIEND), "back1"));
  });
} finally {
  E.stop();
}
if (failed()) process.exit(1);
