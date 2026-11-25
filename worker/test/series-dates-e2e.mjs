// 같은 곳에서 같은 행사를 여러 날 하면 한 통에 날짜를 모아 보낸다(2026-11-25 사용자 지시).
import assert from "node:assert/strict";
import { formatDates } from "../src/pipeline.js";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const today = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(today + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);

// 이어지는 날은 묶고, 떨어진 날은 쉼표로
const f = formatDates([[plus(3)], [plus(4)], [plus(5)], [plus(9)]], today);
assert.match(f, /~ .* 매일, /, f);
assert.equal(formatDates([[plus(3)]], today), null);

const base = {
  bot: "family", location: "Red Rocks Amphitheatre", org: "Red Rocks", annual: true, signup: "unknown", cost_text: null,
  rec_level: "push", special_reason: "연말 몇 주간만 운영", summary_ko: "차 안에서 캐럴에 맞춰 바뀌는 조명을 봅니다.",
  highlights: ["연례 행사"], checked_at: new Date().toISOString(), urgency: "now", tags: ["festival"], time_text: "오후 5시 30분",
};
const evs = [3, 4, 5, 9].map((n, i) => ({ ...base, event_key: `cic${i}`, source_id: i < 3 ? "red-rocks" : "visitdenver",
  title: `Christmas in Color (session ${i + 1})`, title_ko: i < 3 ? "레드락스 크리스마스 인 컬러" : "레드락스 크리스마스 인 컬러 드라이브스루",
  url: `https://www.redrocksonline.com/events/christmas-in-color-${i + 2}/`, start_date: plus(n) }));
const other = { ...base, event_key: "deadmau5", title: "deadmau5 at Red Rocks", title_ko: "데드마우스 레드락스 공연", url: "https://www.redrocksonline.com/events/deadmau5/", start_date: plus(4), special_reason: "1년 한 번 내한", annual: false };

const { step, failed } = stepper();
const E = await startEnv({ tgPort: 9978, wPort: 8878, vars: { FAMILY_AVAILABLE_FROM: "2026-01-01" } });
const { admin, hook, msg, since } = E;
const sent = (n) => since(n).filter((x) => x.method === "sendMessage" && !x.failed && String(x.payload.chat_id) === OWNER && /레드락스/.test(x.payload.text));
try {
  await hook("family", msg(OWNER, "/start"));
  await hook("family", msg(OWNER, "/quiet 0 0"));
  await step("회차 4개 → 한 통, 날짜 모두 표시 / 같은 곳 다른 공연은 따로", async () => {
    const n = E.calls.length;
    await admin("/admin/enqueue", { events: [...evs, other] });
    const r = await admin("/admin/dispatch", { ignoreQuiet: true });
    const m = sent(n);
    const cic = m.filter((x) => /크리스마스 인 컬러/.test(x.payload.text));
    assert.equal(cic.length, 1, JSON.stringify(r));
    const t = cic[0].payload.text;
    assert.match(t, /📅 .* ~ .* 매일, .*오후 5시 30분/, t);
    assert.equal(m.filter((x) => /데드마우스/.test(x.payload.text)).length, 1);
  });
} finally { E.stop(); }
if (failed()) process.exitCode = 1;
