// 이슈 #20: 3경로(GitHub 수집기·Claude·ChatGPT) 발견 기록·대조, 작업량 저장(텔레그램 요약 없음), 외부 입력 파일 재처리 방지, 내보내기, 경고 전용 사용액 알림.
import assert from "node:assert/strict";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const { step, failed } = stepper();
const denverToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
const plus = (n) => new Date(Date.parse(denverToday + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const ev = (key, over = {}) => ({
  event_key: key, bot: "family", title: `Event ${key}`, title_ko: `행사 ${key}`, org: "Org", start_date: plus(20), url: `https://example.org/${key}`,
  checked_at: "2026-10-20T00:00:00Z", urgency: "now", tags: ["t"], annual: false, source_id: "test", rec_level: "push", special_reason: "연 1회 특별 개방", ...over,
});

const E = await startEnv({ tgPort: 9987, wPort: 8811, vars: { NOTIFY_FAR_DAYS: "90" } });
const { admin, hook, msg, sql } = E;
const to = (n) => E.calls.slice(n).filter((c) => c.method === "sendMessage" && String(c.payload.chat_id) === OWNER);

try {
  await hook("family", msg(OWNER, "/start"));
  await hook("family", msg(OWNER, "/quiet 0 0"));

  await step("경로별 최초 발견 기록: GitHub 먼저 → ChatGPT 나중(같은 행사, 다시 알리지 않음) / Claude 만 찾은 행사 / 메시지에 경로 표시 없음", async () => {
    const n = E.calls.length;
    await admin("/admin/enqueue", { events: [ev("both", { found_by: "github_collector", run_id: "r1" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    await admin("/admin/enqueue", { events: [ev("both", { found_by: "chatgpt", source_id: "ext-search", found_at: "2026-10-23T01:00:00Z", ext_level: "push", run_id: "chatgpt-x" }),
      ev("solo", { found_by: "claude", source_id: "ext-search", ext_level: "interest", run_id: "claude-y" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const got = to(n);
    assert.equal(got.filter((c) => /행사 both\b/.test(c.payload.text)).length, 1, "같은 행사는 한 번만");
    assert.equal(got.filter((c) => /행사 solo/.test(c.payload.text)).length, 1);
    for (const c of got) assert.ok(!/GPT|ChatGPT|Claude|클로드|github|미국/i.test(c.payload.text), "출처 표시 없음: " + c.payload.text);
    const rows = sql("SELECT event_key, finder, ext_level FROM event_finds ORDER BY event_key, finder");
    assert.deepEqual(rows.map((r) => `${r.event_key}:${r.finder}`), ["both:chatgpt", "both:github_collector", "solo:claude"]);
  });

  await step("대조 요약: 경로별 건수·단독·중복·최초 발견 경로·발송을 일으킨 경로·원인 코드", async () => {
    await admin("/admin/gaps", { gaps: [{ event_key: "solo", url: "https://example.org/solo", found: "unregistered", finder: "claude",
      cause_code: "query_gap", cause: "검색어에 행사 이름이 없음", action: "검색어 추가: x", rediscovered: true }, { event_key: "zz", url: "https://e.org/z", found: "found", cause_code: "bogus" }] });
    const c = await admin("/admin/compare?since=2026-01-01");
    assert.equal(c.summary.events, 2);
    assert.deepEqual(c.summary.by_finder, { github_collector: 1, chatgpt: 1, claude: 1 });
    assert.deepEqual(c.summary.only, { claude: 1 });
    assert.deepEqual(c.summary.overlap, { "chatgpt+github_collector": 1 });
    assert.deepEqual(c.summary.sent_via, { github_collector: 1, claude: 1 });
    assert.deepEqual(c.summary.causes, { query_gap: 1 });
    const g = sql("SELECT cause_code, rediscovered FROM ext_gaps WHERE event_key = 'zz'")[0];
    assert.equal(g.cause_code, null, "모르는 원인 코드는 저장하지 않음");
    const both = c.events.find((x) => x.event_key === "both");
    assert.equal(both.first_finder, "chatgpt", "외부가 밝힌 발견 시각(found_at)이 더 이르면 그 경로가 최초");
    assert.equal(both.sent_via, "github_collector", "실제 발송은 GitHub 등록 직후");
  });

  await step("작업량 저장은 조용히(텔레그램 없음), 내보내기에 경로별 작업량·대조·원인·사용량이 모두 들어감", async () => {
    const n = E.calls.length;
    const w = await admin("/admin/workload", { workload: { path: "github_collector", run_id: "r1", started_at: "2026-10-23T00:00:00Z", search_requests: 40, sent_now: "unknown" } });
    assert.ok(w.ok);
    assert.equal((await admin("/admin/workload", { workload: { path: "gpt", run_id: "bad" } })).ok, false, "경로 이름 검사");
    await admin("/admin/workload", { workload: { path: "chatgpt", run_id: "chatgpt-x", started_at: "2026-10-23T01:00:00Z", finder_cost_usd: "not_applicable" } });
    assert.equal(to(n).length, 0, "작업량 요약을 텔레그램으로 보내지 않음");
    const x = await admin("/admin/export?since=2026-01-01");
    assert.equal(x.workload.length, 2);
    assert.equal(x.workload[0].sent_now, "unknown");
    assert.ok(x.compare.summary && Array.isArray(x.gaps) && Array.isArray(x.usage) && Array.isArray(x.queries) && Array.isArray(x.failing_sources));
  });

  await step("외부 입력 파일: 같은 경로·같은 내용은 한 번만, 내용이 바뀌거나 실패한 파일은 다시, 경로 형식 검사", async () => {
    const p = "external-inbox/chatgpt/2026-10-23-run1.json";
    assert.equal((await admin("/admin/inbox", { op: "claim", path: p, sha: "aaa" })).claimed, true);
    assert.equal((await admin("/admin/inbox", { op: "claim", path: p, sha: "aaa" })).claimed, false, "처리 중 중복");
    await admin("/admin/inbox", { op: "done", path: p, sha: "aaa", report: { enqueued: 1 } });
    assert.equal((await admin("/admin/inbox", { op: "claim", path: p, sha: "aaa" })).claimed, false, "처리 끝난 파일 재실행");
    assert.equal((await admin("/admin/inbox", { op: "claim", path: p, sha: "bbb" })).claimed, true, "내용이 바뀌면 다시");
    await admin("/admin/inbox", { op: "done", path: p, sha: "bbb", failed: true });
    assert.equal((await admin("/admin/inbox", { op: "claim", path: p, sha: "bbb" })).claimed, true, "실패한 파일은 다시");
    for (const bad of ["external-inbox/x/a.json", "external-inbox/chatgpt/../a.json", "a.json"]) {
      assert.equal((await admin("/admin/inbox", { op: "claim", path: bad, sha: "c" })).ok, false, bad);
    }
    assert.equal(sql("SELECT finder FROM inbox_files")[0].finder, "chatgpt");
  });

  await step("보충 발송: 추천 판정이 나중에 '적극 알림'이 된 행사·예전 상한으로 보관만 된 행사를 요약 시각을 기다리지 않고 한 통씩, 다시 실행해도 중복 없음", async () => {
    await admin("/admin/enqueue", { events: [ev("late", { rec_level: undefined, special_reason: undefined, urgency: "weekly" }),
      ev("heldone", { urgency: "weekly", rec_level: "none", special_reason: "" }), ev("past", { start_date: "2020-01-01", urgency: "weekly", rec_level: undefined })] });
    await admin("/admin/recommend-update", { items: [
      { event_key: "late", rec_level: "push", special_reason: "평소 닫힌 시설을 하루만 공개", bot: "family" },
      { event_key: "heldone", rec_level: "push", special_reason: "선착순 무료 체험", bot: "family" },
      { event_key: "past", rec_level: "push", special_reason: "x", bot: "family" }] });
    sql("INSERT INTO outbox (chat_id, bot, event_key, version, kind, status, reason, created_at) VALUES ('" + OWNER + "', 'family', 'heldone', 1, 'digest', 'held', '하루 알림 수 상한', '2026-10-20')");
    const dry = await admin("/admin/catchup", { dry: true });
    assert.deepEqual(dry.events.sort(), ["heldone", "late"], JSON.stringify(dry));
    const r = await admin("/admin/catchup", {});
    assert.equal(r.queued, 2); assert.equal(r.revived_held, 1);
    // 오늘 요약을 이미 보낸 뒤라도(하루 한 번 표시) 보충분은 매시 실행에서 나간다
    for (const tz of ["America/Denver", "Asia/Seoul"]) {
      const d = new Intl.DateTimeFormat("sv-SE", { timeZone: tz }).format(new Date());
      sql("INSERT OR IGNORE INTO notices (key, sent_at) VALUES ('digest:" + OWNER + ":family:" + d + "', '2026-10-23')");
    }
    const n = E.calls.length;
    await admin("/admin/digest", {});
    const got = to(n);
    for (const k of ["late", "heldone"]) assert.equal(got.filter((c) => new RegExp(`행사 ${k}\\b`).test(c.payload.text)).length, 1, k);
    assert.ok(!got.some((c) => /행사 past/.test(c.payload.text)), "끝난 행사는 보내지 않음");
    const again = await admin("/admin/catchup", {});
    assert.equal(again.queued, 0, "이미 보낸 행사는 다시 보충하지 않음");
    // 같은 정보원·같은 원제목의 다른 날짜 회차(시즌 열차 등)는 이미 받았으면 보충하지 않고, 아직 안 받았으면 한 번만
    await admin("/admin/enqueue", { events: [
      ev("train1", { title: "Holiday Train", source_id: "museum", start_date: plus(30), urgency: "weekly", rec_level: undefined }),
      ev("train2", { title: "Holiday Train", source_id: "museum", start_date: plus(31), urgency: "weekly", rec_level: undefined }),
      ev("late2", { title: "Event late", source_id: "test", start_date: plus(32), urgency: "weekly", rec_level: undefined })] });
    await admin("/admin/recommend-update", { items: ["train1", "train2", "late2"].map((k) => ({ event_key: k, rec_level: "push", special_reason: "시즌 한정", bot: "family" })) });
    const s2 = await admin("/admin/catchup", { dry: true });
    assert.deepEqual(s2.events, ["train1"], JSON.stringify(s2));
    assert.equal(s2.skipped["same-series"], 2, "train2 와, 이미 받은 'Event late' 의 다른 날짜(late2)");
  });

  await step("메시지: 주소는 구글 지도 링크, 비용 정보 없으면 '입장료 미확인'(신청 불필요와 구분), 모델이 '무료'라 해도 비용 근거 없으면 무료 표시 안 함", async () => {
    const n = E.calls.length;
    await admin("/admin/enqueue", { events: [ev("hockey2", { title: "Community Skate Day", source_id: "s-hockey", location: "Student Recreation Center, Ice Rink, 1855 Pleasant Street",
      city: "Boulder", cost_text: null, signup: "none", highlights: ["무료"] }), ev("freeday", { title: "Free Day", source_id: "s-free", cost_text: "무료 (시간지정 입장권 필요)", location: "Denver Zoo" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const t = to(n).map((c) => c.payload.text);
    const h = t.find((x) => /행사 hockey2/.test(x));
    assert.match(h, /장소: <a href="https:\/\/www\.google\.com\/maps\/search\/\?api=1&amp;query=Student%20Recreation%20Center%2C%20Ice%20Rink%2C%201855%20Pleasant%20Street%2C%20Boulder%2C%20CO">Student Recreation Center, Ice Rink, 1855 Pleasant Street<\/a>/);
    assert.match(h, /비용: 입장료 미확인/);
    assert.match(h, /신청 불필요/);
    assert.ok(!/🆓/.test(h), "비용 근거 없이 무료 표시");
    const f = t.find((x) => /행사 freeday/.test(x));
    assert.match(f, /비용: 무료/); assert.match(f, /비용: 무료 \(시간지정 입장권 필요\)/);
  });

  await step("같은 회차 묶음(매주 금요일 투어의 다른 날짜)은 사람마다 한 통만 — 즉시·요약 모두, 같은 행사의 변경 알림은 막지 않음", async () => {
    const n = E.calls.length;
    await admin("/admin/enqueue", { events: [ev("fri1", { title: "Friday Highlights Tour", source_id: "railroad", start_date: plus(7) }),
      ev("fri2", { title: "Friday Highlights Tour", source_id: "railroad", start_date: plus(14) })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    await admin("/admin/enqueue", { events: [ev("fri3", { title: "Friday  highlights tour", source_id: "railroad", start_date: plus(21), urgency: "weekly" })] });
    await admin("/admin/digest", { force: true });
    const got = to(n).filter((c) => /행사 fri/.test(c.payload.text));
    assert.equal(got.length, 1, got.map((c) => c.payload.text.split("\n")[0]).join(" / "));
    const m = n;
    await admin("/admin/enqueue", { events: [ev("fri1", { title: "Friday Highlights Tour", source_id: "railroad", start_date: plus(8) })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(to(m).filter((c) => /\[변경\]/.test(c.payload.text) && /행사 fri1/.test(c.payload.text)).length, 1, "같은 행사의 날짜 변경은 알림");
  });

  await step("차단 사유가 기록된 적극 알림은 발송 직전에 다시 막힘(대기열에 이미 들어간 것 포함)", async () => {
    const n = E.calls.length;
    await admin("/admin/enqueue", { events: [ev("sbath", { title: "Sound Bath", source_id: "hh", rec_blockers: ["routine_commercial_program"] }),
      ev("sbath2", { title: "Sound Bath 2", source_id: "hh", push_blocked: "evidence-not-in-source" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    assert.equal(to(n).filter((c) => /행사 sbath/.test(c.payload.text)).length, 0);
    const why = sql("SELECT reason FROM outbox WHERE event_key = 'sbath'")[0];
    assert.ok(!why || /blocked:routine_commercial_program/.test(why.reason), JSON.stringify(why));
  });

  await step("알릴 때: 내년 접수(예: 국립공원 시간대 예약)는 지금 보내지 않고 접수 7일 전에, 날짜가 하나도 없으면 보내지 않음, 먼 행사는 30일 전", async () => {
    const n = E.calls.length;
    await admin("/admin/enqueue", { events: [
      ev("rmnp", { title: "Timed entry reservations", source_id: "nps", start_date: null, registration_opens: plus(190), special_basis: "scarce_quota_or_lottery" }),
      ev("nodate", { title: "Some permit", source_id: "nps2", start_date: null }),
      ev("farfest", { title: "Far festival", source_id: "x9", start_date: plus(200) }),
      ev("soonreg", { title: "Camp signup", source_id: "x10", start_date: plus(60), registration_opens: plus(3) })] });
    const r = await admin("/admin/dispatch", { ignoreQuiet: true });
    const got = to(n).map((c) => c.payload.text);
    assert.ok(got.some((t) => /행사 soonreg/.test(t)), "7일 안 접수는 지금");
    for (const k of ["rmnp", "nodate", "farfest"]) assert.ok(!got.some((t) => new RegExp(`행사 ${k}\\b`).test(t)), k + " 가 지금 감");
    const rows = Object.fromEntries(sql("SELECT event_key, status, reason, next_attempt_at FROM outbox WHERE event_key IN ('rmnp','nodate','farfest')").map((x) => [x.event_key, x]));
    assert.equal(rows.rmnp.status, "pending"); assert.equal(rows.rmnp.next_attempt_at.slice(0, 10), plus(183), "접수 7일 전");
    assert.equal(rows.farfest.next_attempt_at.slice(0, 10), plus(170), "행사 30일 전");
    assert.equal(rows.nodate.status, "skipped"); assert.equal(rows.nodate.reason, "no-date");
    const p = await admin("/admin/outbox-preview");
    assert.ok(p.skipped.scheduled >= 2 && p.skipped["no-date"] === undefined, JSON.stringify(p.skipped));
    assert.ok(r.deferred >= 2);
  });

  await step("경로 간 중복: 다른 경로·정보원이 다른 키로 올린 같은 행사(같은 공식 주소, 또는 같은 날짜+비슷한 제목)는 한 통만", async () => {
    const n = E.calls.length;
    const d = plus(15);
    await admin("/admin/enqueue", { events: [ev("gh1", { title: "Free Night at Wings Over the Rockies Air and Space Museum", source_id: "wings-api", start_date: d, url: "https://wingsmuseum.org/events/free-night/" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    await admin("/admin/enqueue", { events: [
      ev("gpt1", { title: "Denver Arts Week: Free Night at Wings Over the Rockies", source_id: "ext-search", found_by: "chatgpt", start_date: d, url: "https://wingsmuseum.org/events/free-night" }),
      ev("cl1", { title: "Wings Over the Rockies museum free night (Arts Week)", source_id: "ext-search", found_by: "claude", start_date: d, url: "https://www.wingsmuseum.org/other-page/" }),
      ev("diff1", { title: "Pumpkin Splash at Recreation Pool", source_id: "ext-search", found_by: "claude", start_date: d, url: "https://e.org/pumpkin" })] });
    await admin("/admin/dispatch", { ignoreQuiet: true });
    const got = to(n).map((c) => c.payload.text);
    assert.equal(got.filter((t) => /행사 (gh1|gpt1|cl1)\b/.test(t)).length, 1, got.map((t) => t.split("\n")[0]).join(" / "));
    assert.ok(got.some((t) => /행사 diff1/.test(t)), "다른 행사는 그대로");
    const why = Object.fromEntries(sql("SELECT event_key, reason FROM outbox WHERE event_key IN ('gpt1','cl1')").map((x) => [x.event_key, x.reason]));
    assert.equal(why.gpt1, "same-url"); assert.equal(why.cl1, "same-event-other-source");
  });

  await step("사용액 경고는 경고만(멈춘다는 말·예산 승인 요청 없음), 카드·충전 안 함 명시", async () => {
    const n = E.calls.length;
    await admin("/admin/usage", { model: "claude-opus-5-5", usd: 101, calls: 1, budget: 100 });
    const t = to(n).map((c) => c.payload.text).join("\n");
    assert.match(t, /\$100의 100%/);
    assert.match(t, /스스로 멈추지는 않습니다/);
    assert.match(t, /카드 등록·충전·자동 충전은 하지 않습니다/);
    assert.ok(!/예산을 올릴지|monthly_budget/.test(t));
  });
} finally {
  E.stop();
}
if (failed()) process.exit(1);
