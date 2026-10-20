// 로컬 종단 테스트(1단계): 판정 결과 → 사람별 대기열 → 발송·요약·변경 재알림·정지 시간·소유자 전용 모드.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const FRIEND = "222";
const { step, failed } = stepper();

const ev = (key, over = {}) => ({
  event_key: key, bot: "kids", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Test Org", start_date: "2027-07-10", end_date: null,
  time_text: "10:00", location: "Westminster Library", city: "Westminster", age_text: "6-10세", english: ["초보 가능"], cost_text: "$20",
  price_level: "보통", signup: "fcfs", deadline: null, status_note: null, url: `https://example.org/${key}`, checked_at: "2026-10-20T00:00:00Z",
  urgency: "weekly", tags: ["art"], annual: false, source_id: "test", ...over,
});

// ---------------------------------------------------------------- A. 모든 사람에게 보내는 모드(DELIVERY_MODE=all)
let E = await startEnv({ tgPort: 9991, wPort: 8801, vars: { DELIVERY_MODE: "all", FAMILY_AVAILABLE_FROM: "2027-01-01" } });
try {
  const { admin, hook, msg, since } = E;
  const sends = (n, to) => since(n).filter((c) => c.method === "sendMessage" && (!to || String(c.payload.chat_id) === to));

  await step("준비: 소유자 life·kids 시작, 지인 kids 시작·승인, 정지 시간 끄기", async () => {
    await hook("life", msg(OWNER, "/start"));
    await hook("kids", msg(OWNER, "/start"));
    await hook("kids", msg(FRIEND, "/start"));
    await hook("kids", msg(OWNER, `/approve ${FRIEND}`));
    await hook("kids", msg(OWNER, "/quiet 0 0"));
    await hook("kids", msg(FRIEND, "/quiet 0 0"));
    const n = E.calls.length;
    await hook("kids", msg(FRIEND, "/settings"));
    assert.match(sends(n, FRIEND)[0].payload.text, /America\/Denver · 알림 정지 0시~0시/);
  });

  await step("개인 설정 검증: 주소처럼 숫자가 들어간 도시는 거부, 잘못된 시간대 거부", async () => {
    const n = E.calls.length;
    await hook("kids", msg(FRIEND, "/city 123 Main St"));
    await hook("kids", msg(FRIEND, "/tz Mars/Base"));
    const t = sends(n, FRIEND).map((s) => s.payload.text);
    assert.match(t[0], /도시 이름만/);
    assert.match(t[1], /형식/);
  });

  await step("enqueue: 참가 가능 시작일·연례·구독 봇에 따라 사람별 대기열", async () => {
    const r = await admin("/admin/enqueue", {
      events: [
        ev("e1", { urgency: "now", start_date: "2026-11-10" }),            // 소유자: 입국 전 → 제외, 지인: 즉시
        ev("e2", { start_date: "2026-12-05", annual: true }),              // 소유자: 예고(season), 지인: 요약
        ev("e3"),                                                          // 둘 다 요약
        ev("e4", { bot: "life", urgency: "now", start_date: "2027-08-01" }), // 소유자만(life 구독)
        { event_key: "bad", bot: "kids", title: "no url" },
      ],
    });
    assert.equal(r.accepted, 4);
    assert.equal(r.new, 4);
    assert.equal(r.outbox, 6, JSON.stringify(r));
    assert.equal(r.skipped["before-available"], 1);
  });

  await step("dispatch: 즉시·예고 3건 발송, 7줄 형식 + 버튼 3개", async () => {
    const n = E.calls.length;
    const r = await admin("/admin/dispatch", {});
    assert.equal(r.sent, 3, JSON.stringify(r));
    const s = sends(n);
    const friendE1 = s.find((x) => String(x.payload.chat_id) === FRIEND);
    assert.match(friendE1.payload.text, /^🆕 행사 e1 · Test Org\n📅 2026-11-10 10:00 · 📍 Westminster Library\n👤 6-10세 · 영어: 초보 가능\n💲 \$20 \(보통\)\n📝 선착순\n🛂 미확인\n🔗 https:\/\/example.org\/e1 · 확인 2026-10-20$/);
    assert.equal(friendE1.payload.reply_markup.inline_keyboard[0].length, 3);
    assert.ok(s.some((x) => String(x.payload.chat_id) === OWNER && /^📆 예고 행사 e2/.test(x.payload.text)));
    assert.ok(s.some((x) => x.token === "tLIFE" && /행사 e4/.test(x.payload.text)));
  });

  await step("같은 내용 재전송 → 변화 없음, 대기열 추가 없음", async () => {
    const r = await admin("/admin/enqueue", { events: [ev("e1", { urgency: "now", start_date: "2026-11-10" }), ev("e3")] });
    assert.equal(r.same, 2);
    assert.equal(r.outbox, 0);
  });

  await step("중요 필드 변경 → 새 판으로 재알림(확인한 사람에게도), 관심없음은 제외", async () => {
    await hook("kids", E.cb(FRIEND, "ack:e1", 1));
    await hook("life", E.cb(OWNER, "mute:e4", 1));
    const r = await admin("/admin/enqueue", {
      events: [ev("e1", { urgency: "now", start_date: "2026-11-10", cost_text: "$25" }), ev("e4", { bot: "life", urgency: "now", start_date: "2027-08-01", location: "Denver" })],
    });
    assert.equal(r.changed, 2);
    assert.equal(r.skipped.muted, 1);
    const n = E.calls.length;
    await admin("/admin/dispatch", {});
    const s = sends(n);
    assert.equal(s.length, 1);
    assert.match(s[0].payload.text, /^🔄 변경 행사 e1/);
    assert.match(s[0].payload.text, /바뀐 점: 비용 \$20 → \$25/);
  });

  await step("주간 요약(force): 사람·봇별 1통, 줄마다 번호 버튼", async () => {
    const n = E.calls.length;
    const r = await admin("/admin/digest", { force: true });
    assert.equal(r.sent, 2, JSON.stringify(r));
    const f = sends(n, FRIEND)[0];
    assert.match(f.payload.text, /주간 요약 \(2건\)/);
    assert.deepEqual(f.payload.reply_markup.inline_keyboard.map((row) => row.map((b) => b.text)), [["1 ✅", "1 🚫"], ["2 ✅", "2 🚫"]]);
    E.digestMarkup = f.payload.reply_markup;
  });

  await step("요약 메시지의 2번 ✅ → 그 줄만 바뀜", async () => {
    const key = E.digestMarkup.inline_keyboard[1][0].callback_data.split(":")[1];
    const n = E.calls.length;
    await hook("kids", E.cb(FRIEND, `ack:${key}`, 777, E.digestMarkup));
    const edit = since(n).find((c) => c.method === "editMessageReplyMarkup");
    assert.deepEqual(edit.payload.reply_markup.inline_keyboard.map((row) => row.map((b) => b.text)), [["1 ✅", "1 🚫"], ["2 ✅✓", "2 🚫"]]);
  });

  await step("정지 시간대: 지금 시각이 정지 구간이면 미뤘다가, 풀리면 발송", async () => {
    const h = parseInt(new Intl.DateTimeFormat("en-US", { timeZone: "America/Denver", hour: "2-digit", hour12: false }).format(new Date()), 10) % 24;
    await hook("kids", msg(FRIEND, `/quiet ${h} ${(h + 1) % 24}`));
    await admin("/admin/enqueue", { events: [ev("e5", { urgency: "now", start_date: "2026-12-20" })] });
    let r = await admin("/admin/dispatch", {});
    assert.equal(r.deferred, 1);
    assert.equal(r.sent, 0);
    await hook("kids", msg(FRIEND, "/quiet 0 0"));
    r = await admin("/admin/dispatch", {});
    assert.equal(r.sent, 1);
  });

  await step("이 태그 그만 → 같은 태그 새 행사는 대기열에 안 들어감", async () => {
    await hook("kids", E.cb(FRIEND, "tag:e5", 1));
    const r = await admin("/admin/enqueue", { events: [ev("e6", { urgency: "now", start_date: "2026-12-21" })] });
    assert.equal(r.skipped["muted-tag"], 1);
  });

  await step("seen: 처음 본 것만 new, 내용 해시가 바뀌면 changed", async () => {
    let r = await admin("/admin/seen", { op: "check", items: [{ key: "a", hash: "1" }, { key: "b", hash: "1" }] });
    assert.deepEqual(r.new, ["a", "b"]);
    await admin("/admin/seen", { op: "mark", items: [{ key: "a", hash: "1", source_id: "s" }, { key: "b", hash: "1", source_id: "s" }] });
    r = await admin("/admin/seen", { op: "check", items: [{ key: "a", hash: "1" }, { key: "b", hash: "2" }, { key: "c", hash: "1" }] });
    assert.deepEqual(r.new, ["c"]);
    assert.deepEqual(r.changed, ["b"]);
  });

  await step("usage 누적 + run-log 경고는 소유자에게 1회", async () => {
    await admin("/admin/usage", { month: "2026-10", model: "claude-haiku-5-5", input_tokens: 1000, output_tokens: 200, usd: 0.0002 });
    const u = await admin("/admin/usage", { month: "2026-10", model: "claude-haiku-5-5", input_tokens: 1000, output_tokens: 200, usd: 0.0002 });
    assert.equal(u.rows[0].input_tokens, 2000);
    const n = E.calls.length;
    await admin("/admin/run-log", { run_id: "r1", started_at: new Date().toISOString(), metrics: { entries_new: 3 }, alerts: ["월 예산 80% 도달"] });
    await admin("/admin/run-log", { run_id: "r1", started_at: new Date().toISOString(), metrics: { entries_new: 3 }, alerts: ["월 예산 80% 도달"] });
    assert.equal(sends(n, OWNER).filter((s) => /예산/.test(s.payload.text)).length, 1);
  });

  await step("정보원 후보: 2회째에 소유자에게 질문 → 추가 누르면 sources-extra 에 등록", async () => {
    const c = { url: "https://newsite.org/events", name: "New Site", reason: "kids classes", bots: ["kids"] };
    let r = await admin("/admin/candidates", { candidates: [c] });
    assert.equal(r.asked, 0);
    const n = E.calls.length;
    r = await admin("/admin/candidates", { candidates: [c] });
    assert.equal(r.asked, 1);
    const q = sends(n, OWNER)[0];
    const data = q.payload.reply_markup.inline_keyboard[0][0].callback_data;
    assert.match(data, /^cpro:/);
    await hook("life", E.cb(OWNER, data, 9));
    const s = await admin("/admin/sources-extra");
    assert.equal(s.sources[0].url, c.url);
    assert.deepEqual(s.sources[0].bots, ["kids"]);
  });

  await step("주간 보고 → 소유자 DM", async () => {
    const n = E.calls.length;
    const r = await admin("/admin/report", {});
    assert.equal(r.ok, true);
    assert.match(sends(n, OWNER)[0].payload.text, /주간 보고/);
  });
} finally {
  E.stop();
}

// ---------------------------------------------------------------- B. 기본값(소유자 전용) 모드
E = await startEnv({ tgPort: 9992, wPort: 8802 });
try {
  const { admin, hook, msg, since } = E;
  await step("소유자 전용 모드: 승인된 지인이 있어도 대기열·발송은 소유자만", async () => {
    await hook("kids", msg(OWNER, "/start"));
    await hook("kids", msg(FRIEND, "/start"));
    await hook("kids", msg(OWNER, `/approve ${FRIEND}`));
    await hook("kids", msg(OWNER, "/quiet 0 0"));
    const r = await admin("/admin/enqueue", { events: [ev("o1", { urgency: "now", start_date: "2027-07-01" })] });
    assert.equal(r.skipped["owner-only"], 1);
    assert.equal(r.outbox, 1);
    const n = E.calls.length;
    await admin("/admin/dispatch", {});
    await admin("/admin/digest", { force: true });
    const to = since(n).filter((c) => c.method === "sendMessage").map((c) => String(c.payload.chat_id));
    assert.deepEqual([...new Set(to)], [OWNER]);
    const st = await admin("/admin/status");
    assert.equal(st.delivery_mode, "owner-only");
  });
} finally {
  E.stop();
}

if (failed()) { console.log("\n--- wrangler log (tail) ---\n" + E.log().slice(-3000)); process.exit(1); }
console.log("\n모든 파이프라인 검사 통과");
