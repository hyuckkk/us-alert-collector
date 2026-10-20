// 수집기(GitHub Actions)가 보내는 판정 결과를 받아 사람별로 나눠 보내는 부분.
// D1 은 무료 요금제에서 호출 1회당 쿼리 수가 제한되므로, 여러 건을 json_each 로 한 번에 처리한다.

import { BOTS, isOwner, nowIso, ownerOnly, tg, claimNotice, notifyOwner, kvSet, defaultPerson, hourIn, weekdayIn, dateIn, inQuiet, localDate } from "./common.js";
import { renderEvent, renderDigest, keyboard, compactRow } from "./render.js";

const IMPORTANT = ["start_date", "end_date", "time_text", "location", "cost_text", "signup", "deadline", "status_note"];
const FIELD_KO = { start_date: "시작일", end_date: "종료일", time_text: "시간", location: "장소", cost_text: "비용", signup: "신청 방식", deadline: "마감", status_note: "신분·주민 메모" };
const MAX_EVENTS_PER_CALL = 30;
const SEND_LIMIT = 20;   // 무료 요금제 하위 요청 50개 한도 안에서 여유를 둔다
const DIGEST_LIMIT = 10; // 요약 1통에 담는 최대 건수

async function sha(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}
function importantOf(d) { return Object.fromEntries(IMPORTANT.map((k) => [k, d[k] ?? null])); }
function diffNote(oldD, newD) {
  return IMPORTANT.filter((k) => (oldD[k] ?? null) !== (newD[k] ?? null))
    .map((k) => `${FIELD_KO[k]} ${oldD[k] ?? "미확인"} → ${newD[k] ?? "미확인"}`).join(", ");
}
const all = async (stmt) => (await stmt.all()).results;

// ---------------------------------------------------------------- 이미 본 항목
export async function seen(env, body) {
  const items = (Array.isArray(body.items) ? body.items : []).filter((x) => x && x.key && x.hash).slice(0, 500);
  if (body.op === "mark") {
    const now = nowIso();
    await env.DB.prepare(
      "INSERT INTO seen (key, hash, source_id, first_seen, last_seen) " +
      "SELECT json_extract(value, '$.key'), json_extract(value, '$.hash'), json_extract(value, '$.source_id'), ?1, ?1 FROM json_each(?2) WHERE true " +
      "ON CONFLICT(key) DO UPDATE SET hash = excluded.hash, last_seen = excluded.last_seen",
    ).bind(now, JSON.stringify(items)).run();
    return { ok: true, marked: items.length };
  }
  const rows = await all(env.DB.prepare("SELECT key, hash FROM seen WHERE key IN (SELECT value FROM json_each(?))").bind(JSON.stringify(items.map((x) => x.key))));
  const known = new Map(rows.map((r) => [r.key, r.hash]));
  return {
    ok: true,
    new: items.filter((x) => !known.has(x.key)).map((x) => x.key),
    changed: items.filter((x) => known.has(x.key) && known.get(x.key) !== x.hash).map((x) => x.key),
  };
}

// ---------------------------------------------------------------- 판정 결과 받기 → 사람별 대기열
export async function enqueue(env, body) {
  const now = nowIso();
  const input = (Array.isArray(body.events) ? body.events : [])
    .filter((e) => e && typeof e.event_key === "string" && e.event_key.length <= 48 && BOTS[e.bot] && /^https?:\/\//.test(e.url || "") && (e.title || e.title_ko))
    .slice(0, MAX_EVENTS_PER_CALL);
  const stats = { received: (body.events || []).length, accepted: input.length, new: 0, changed: 0, same: 0, outbox: 0, skipped: {} };
  if (!input.length) return { ok: true, ...stats };

  const keys = input.map((e) => e.event_key);
  const existing = new Map((await all(env.DB.prepare("SELECT event_key, version, content_hash, data FROM events WHERE event_key IN (SELECT value FROM json_each(?))").bind(JSON.stringify(keys))))
    .map((r) => [r.event_key, r]));

  const rows = [];
  const fresh = []; // 새로 보내야 하는 판
  for (const e of input) {
    const h = await sha(JSON.stringify(importantOf(e)));
    const ex = existing.get(e.event_key);
    let version = 1, note = null;
    if (!ex) stats.new++;
    else if (ex.content_hash !== h) { version = ex.version + 1; note = diffNote(JSON.parse(ex.data), e); stats.changed++; }
    else { version = ex.version; stats.same++; }
    rows.push({
      event_key: e.event_key, bot: e.bot, version, content_hash: h, data: JSON.stringify(e), urgency: e.urgency === "now" ? "now" : "weekly",
      start_date: e.start_date || null, end_date: e.end_date || null, annual: e.annual ? 1 : 0, tags: JSON.stringify(e.tags || []),
      source_id: e.source_id || null, url: e.url, note, tag: (e.tags || [])[0] || null, title: e.title_ko || e.title,
      changed: !ex || ex.content_hash !== h,
    });
    if (!ex || ex.content_hash !== h) fresh.push({ e, version });
  }

  const payload = JSON.stringify(rows);
  const stmts = [
    env.DB.prepare(
      "INSERT INTO events (event_key, bot, version, content_hash, data, urgency, start_date, end_date, annual, tags, source_id, url, first_seen, last_changed, change_note) " +
      "SELECT json_extract(value,'$.event_key'), json_extract(value,'$.bot'), json_extract(value,'$.version'), json_extract(value,'$.content_hash'), json_extract(value,'$.data'), " +
      "json_extract(value,'$.urgency'), json_extract(value,'$.start_date'), json_extract(value,'$.end_date'), json_extract(value,'$.annual'), json_extract(value,'$.tags'), " +
      "json_extract(value,'$.source_id'), json_extract(value,'$.url'), ?1, ?1, json_extract(value,'$.note') FROM json_each(?2) WHERE true " +
      "ON CONFLICT(event_key) DO UPDATE SET data = excluded.data, urgency = excluded.urgency, tags = excluded.tags, url = excluded.url, " +
      "version = excluded.version, content_hash = excluded.content_hash, start_date = excluded.start_date, end_date = excluded.end_date, " +
      "last_changed = CASE WHEN events.content_hash <> excluded.content_hash THEN excluded.last_changed ELSE events.last_changed END, " +
      "change_note = CASE WHEN events.content_hash <> excluded.content_hash THEN excluded.change_note ELSE events.change_note END",
    ).bind(now, payload),
    // 버튼 처리(이 태그 그만)가 쓰는 items 표에도 같은 키로 넣는다
    env.DB.prepare(
      "INSERT INTO items (item_key, bot, title, tag, url, created_at) SELECT json_extract(value,'$.event_key'), json_extract(value,'$.bot'), json_extract(value,'$.title'), " +
      "json_extract(value,'$.tag'), json_extract(value,'$.url'), ?1 FROM json_each(?2) WHERE true " +
      "ON CONFLICT(item_key) DO UPDATE SET title = excluded.title, tag = excluded.tag, url = excluded.url",
    ).bind(now, payload),
  ];
  await env.DB.batch(stmts);

  if (!fresh.length) return { ok: true, ...stats };
  const recips = await all(env.DB.prepare(
    "SELECT r.chat_id, r.bot, p.available_from AS available_from, p.role AS role FROM recipients r LEFT JOIN people p ON p.chat_id = r.chat_id WHERE r.status = 'active'"));
  const states = new Map((await all(env.DB.prepare("SELECT chat_id, item_key, state FROM item_state WHERE item_key IN (SELECT value FROM json_each(?))").bind(JSON.stringify(keys))))
    .map((r) => [`${r.chat_id}|${r.item_key}`, r.state]));
  const mutedTags = new Map();
  for (const r of await all(env.DB.prepare("SELECT chat_id, tag FROM muted_tags"))) {
    if (!mutedTags.has(r.chat_id)) mutedTags.set(r.chat_id, new Set());
    mutedTags.get(r.chat_id).add(r.tag);
  }
  const skip = (why) => { stats.skipped[why] = (stats.skipped[why] || 0) + 1; };
  const out = [];
  for (const { e, version } of fresh) {
    for (const r of recips) {
      if (r.bot !== e.bot) continue;
      if (ownerOnly(env) && !isOwner(env, r.chat_id)) { skip("owner-only"); continue; }
      if (states.get(`${r.chat_id}|${e.event_key}`) === "MUTED") { skip("muted"); continue; }
      const mt = mutedTags.get(String(r.chat_id));
      if (mt && (e.tags || []).some((t) => mt.has(t))) { skip("muted-tag"); continue; }
      const from = r.available_from ?? defaultPerson(env, r.chat_id).available_from;
      const ref = e.end_date || e.start_date;
      let kind = e.urgency === "now" ? "now" : "digest";
      if (from && ref && ref < from) {
        // 참가 가능 시작일 이전 행사: 연례행사면 '내년에도 열릴 가능성' 예고로, 아니면 보내지 않음
        if (!e.annual) { skip("before-available"); continue; }
        kind = "season";
      }
      out.push({ chat_id: String(r.chat_id), bot: e.bot, event_key: e.event_key, version, kind });
    }
  }
  if (out.length) {
    const res = await env.DB.prepare(
      "INSERT INTO outbox (chat_id, bot, event_key, version, kind, created_at) SELECT json_extract(value,'$.chat_id'), json_extract(value,'$.bot'), " +
      "json_extract(value,'$.event_key'), json_extract(value,'$.version'), json_extract(value,'$.kind'), ?1 FROM json_each(?2) WHERE true " +
      "ON CONFLICT(chat_id, bot, event_key, version) DO NOTHING",
    ).bind(now, JSON.stringify(out)).run();
    stats.outbox = res.meta.changes;
  }
  return { ok: true, ...stats };
}

// ---------------------------------------------------------------- 즉시·예고 알림 발송 (정지 시간대는 미룸)
async function finish(env, updates) {
  if (!updates.length) return;
  const now = nowIso();
  await env.DB.batch([
    env.DB.prepare(
      "UPDATE outbox SET status = json_extract(j.value,'$.status'), reason = json_extract(j.value,'$.reason'), message_id = json_extract(j.value,'$.message_id'), " +
      "sent_at = CASE WHEN json_extract(j.value,'$.status') = 'sent' THEN ?1 ELSE sent_at END FROM (SELECT value FROM json_each(?2)) AS j WHERE outbox.id = json_extract(j.value,'$.id')",
    ).bind(now, JSON.stringify(updates)),
    env.DB.prepare(
      "INSERT INTO deliveries (bot, chat_id, item_key, message_id, ok, error, sent_at) SELECT json_extract(value,'$.bot'), json_extract(value,'$.chat_id'), " +
      "json_extract(value,'$.event_key'), json_extract(value,'$.message_id'), json_extract(value,'$.ok'), json_extract(value,'$.error'), ?1 FROM json_each(?2) WHERE json_extract(value,'$.sent') = 1",
    ).bind(now, JSON.stringify(updates)),
  ]);
}

export async function dispatch(env, opts = {}) {
  const limit = Math.min(opts.limit || SEND_LIMIT, SEND_LIMIT);
  const rows = await all(env.DB.prepare(
    "SELECT o.id, o.chat_id, o.bot, o.event_key, o.version, o.kind, e.data, e.change_note, p.tz, p.quiet_start, p.quiet_end, s.state " +
    "FROM outbox o JOIN events e ON e.event_key = o.event_key LEFT JOIN people p ON p.chat_id = o.chat_id " +
    "LEFT JOIN item_state s ON s.chat_id = o.chat_id AND s.item_key = o.event_key " +
    "WHERE o.status = 'pending' AND o.kind IN ('now', 'season') ORDER BY o.id LIMIT 100"));
  const r = { sent: 0, deferred: 0, skipped: 0, failed: 0 };
  const updates = [];
  for (const row of rows) {
    if (ownerOnly(env) && !isOwner(env, row.chat_id)) { updates.push({ id: row.id, status: "skipped", reason: "owner-only" }); r.skipped++; continue; }
    if (row.state === "MUTED") { updates.push({ id: row.id, status: "skipped", reason: "muted" }); r.skipped++; continue; }
    const p = { ...defaultPerson(env, row.chat_id), ...(row.tz ? { tz: row.tz, quiet_start: row.quiet_start, quiet_end: row.quiet_end } : {}) };
    if (!opts.ignoreQuiet && inQuiet(hourIn(p.tz), p.quiet_start, p.quiet_end)) { r.deferred++; continue; }
    if (r.sent + r.failed >= limit) break;
    const text = renderEvent(JSON.parse(row.data), { kind: row.kind, version: row.version, note: row.change_note });
    const d = await tg(env, row.bot, "sendMessage", { chat_id: row.chat_id, text, disable_web_page_preview: true, reply_markup: keyboard(row.event_key, { state: row.state }) });
    const mid = d.ok && d.result ? d.result.message_id : null;
    updates.push({ id: row.id, status: d.ok ? "sent" : "failed", reason: d.ok ? null : String(d.description || "").slice(0, 200), message_id: mid,
      sent: 1, ok: d.ok ? 1 : 0, error: d.ok ? null : String(d.description || "").slice(0, 200), bot: row.bot, chat_id: row.chat_id, event_key: row.event_key });
    d.ok ? r.sent++ : r.failed++;
  }
  await finish(env, updates);
  const left = await env.DB.prepare("SELECT COUNT(*) AS n FROM outbox WHERE status = 'pending' AND kind IN ('now', 'season')").first();
  return { ok: true, ...r, pending: left.n };
}

// ---------------------------------------------------------------- 주간 요약 (각자 현지 토요일 9시 이후, 정지 시간 밖에서 1회)
export async function digest(env, opts = {}) {
  const groups = await all(env.DB.prepare(
    "SELECT o.chat_id, o.bot, COUNT(*) AS n, p.tz, p.quiet_start, p.quiet_end FROM outbox o LEFT JOIN people p ON p.chat_id = o.chat_id " +
    "WHERE o.status = 'pending' AND o.kind = 'digest' GROUP BY o.chat_id, o.bot"));
  const r = { sent: 0, waiting: 0, skipped: 0 };
  const updates = [];
  let calls = 0;
  for (const g of groups) {
    if (ownerOnly(env) && !isOwner(env, g.chat_id)) {
      await env.DB.prepare("UPDATE outbox SET status = 'skipped', reason = 'owner-only' WHERE status = 'pending' AND kind = 'digest' AND chat_id = ?").bind(g.chat_id).run();
      r.skipped += g.n; continue;
    }
    const p = { ...defaultPerson(env, g.chat_id), ...(g.tz ? { tz: g.tz, quiet_start: g.quiet_start, quiet_end: g.quiet_end } : {}) };
    const due = weekdayIn(p.tz) === 6 && hourIn(p.tz) >= 9 && !inQuiet(hourIn(p.tz), p.quiet_start, p.quiet_end);
    if (!opts.force && !due) { r.waiting++; continue; }
    if (!opts.force && !(await claimNotice(env, `digest:${g.chat_id}:${g.bot}:${dateIn(p.tz)}`))) { r.waiting++; continue; }
    if (calls >= 15) break;
    const items = await all(env.DB.prepare(
      "SELECT o.id, o.event_key, e.data, s.state FROM outbox o JOIN events e ON e.event_key = o.event_key LEFT JOIN item_state s ON s.chat_id = o.chat_id AND s.item_key = o.event_key " +
      "WHERE o.status = 'pending' AND o.kind = 'digest' AND o.chat_id = ? AND o.bot = ? ORDER BY COALESCE(e.start_date, '9999') LIMIT ?").bind(g.chat_id, g.bot, DIGEST_LIMIT));
    const live = items.filter((x) => x.state !== "MUTED").map((x) => ({ ...x, data: JSON.parse(x.data) }));
    for (const x of items.filter((x) => x.state === "MUTED")) updates.push({ id: x.id, status: "skipped", reason: "muted" });
    if (!live.length) continue;
    const kb = { inline_keyboard: live.map((x, i) => compactRow(x.event_key, { state: x.state }, String(i + 1))) };
    const d = await tg(env, g.bot, "sendMessage", { chat_id: g.chat_id, text: renderDigest(BOTS[g.bot].label, live), disable_web_page_preview: true, reply_markup: kb });
    calls++;
    const mid = d.ok && d.result ? d.result.message_id : null;
    for (const x of live) {
      updates.push({ id: x.id, status: d.ok ? "sent" : "failed", reason: d.ok ? null : String(d.description || "").slice(0, 200), message_id: mid,
        sent: 1, ok: d.ok ? 1 : 0, error: d.ok ? null : String(d.description || "").slice(0, 200), bot: g.bot, chat_id: g.chat_id, event_key: x.event_key });
    }
    if (d.ok) r.sent++;
  }
  await finish(env, updates);
  return { ok: true, ...r };
}

// ---------------------------------------------------------------- 실행 기록·사용량
export async function runLog(env, body) {
  if (!body.run_id) return { ok: false, error: "run_id required" };
  await env.DB.prepare("INSERT INTO runs (run_id, started_at, finished_at, data) VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET finished_at = excluded.finished_at, data = excluded.data")
    .bind(String(body.run_id), body.started_at || nowIso(), body.finished_at || null, JSON.stringify(body.metrics || {})).run();
  const alerts = (body.alerts || []).filter(Boolean).slice(0, 5);
  let notified = false;
  if (alerts.length && (await claimNotice(env, `run-alert:${body.run_id}`))) {
    await notifyOwner(env, `⚠️ 수집 실행 ${body.run_id}\n` + alerts.map((a) => `- ${String(a).slice(0, 300)}`).join("\n"));
    notified = true;
  }
  return { ok: true, notified };
}

export async function usage(env, method, body, url) {
  if (method === "POST") {
    const m = body.month || nowIso().slice(0, 7);
    await env.DB.prepare(
      "INSERT INTO usage (month, model, input_tokens, output_tokens, usd, calls) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(month, model) DO UPDATE SET " +
      "input_tokens = usage.input_tokens + excluded.input_tokens, output_tokens = usage.output_tokens + excluded.output_tokens, usd = usage.usd + excluded.usd, calls = usage.calls + excluded.calls",
    ).bind(m, String(body.model || "?"), body.input_tokens | 0, body.output_tokens | 0, Number(body.usd) || 0, body.calls | 0 || 1).run();
  }
  const month = (url && url.searchParams.get("month")) || body.month || nowIso().slice(0, 7);
  const rows = await all(env.DB.prepare("SELECT * FROM usage WHERE month = ?").bind(month));
  return { ok: true, month, rows, usd: rows.reduce((a, r) => a + r.usd, 0) };
}

// ---------------------------------------------------------------- 정보원 후보
export async function addCandidates(env, body) {
  const list = (body.candidates || []).filter((c) => c && /^https?:\/\//.test(c.url || "")).slice(0, 30);
  const now = nowIso();
  const rows = [];
  for (const c of list) rows.push({ id: await sha(c.url.replace(/\/+$/, "").toLowerCase()).then((h) => h.slice(0, 12)), url: c.url, name: c.name || null, reason: c.reason || null, bots: JSON.stringify(c.bots || []) });
  if (rows.length) {
    await env.DB.prepare(
      "INSERT INTO candidates (id, url, name, reason, bots, hits, first_seen, last_seen) SELECT json_extract(value,'$.id'), json_extract(value,'$.url'), json_extract(value,'$.name'), " +
      "json_extract(value,'$.reason'), json_extract(value,'$.bots'), 1, ?1, ?1 FROM json_each(?2) WHERE true " +
      "ON CONFLICT(id) DO UPDATE SET hits = candidates.hits + 1, last_seen = excluded.last_seen, reason = excluded.reason",
    ).bind(now, JSON.stringify(rows)).run();
  }
  // 2회 이상 나온 후보는 소유자에게 승격 여부를 한 번 묻는다
  const ready = await all(env.DB.prepare("SELECT * FROM candidates WHERE status = 'pending' AND notified = 0 AND hits >= 2 LIMIT 5"));
  for (const c of ready) {
    await notifyOwner(env, `🔎 정보원 후보 (${c.hits}회 발견)\n${c.name || ""}\n${c.url}\n이유: ${c.reason || "-"}`, {
      reply_markup: { inline_keyboard: [[{ text: "➕ 감시 목록에 추가", callback_data: `cpro:${c.id}` }, { text: "무시", callback_data: `cign:${c.id}` }]] },
    });
    await env.DB.prepare("UPDATE candidates SET notified = 1 WHERE id = ?").bind(c.id).run();
  }
  return { ok: true, upserted: rows.length, asked: ready.length };
}

export async function decideCandidate(env, id, promote) {
  const c = await env.DB.prepare("SELECT * FROM candidates WHERE id = ?").bind(id).first();
  if (!c) return null;
  const now = nowIso();
  const stmts = [env.DB.prepare("UPDATE candidates SET status = ? WHERE id = ?").bind(promote ? "promoted" : "ignored", id)];
  if (promote) {
    stmts.push(env.DB.prepare("INSERT INTO sources_extra (id, url, name, type, bots, added_at) VALUES (?, ?, ?, 'html', ?, ?) ON CONFLICT(id) DO NOTHING")
      .bind(`cand-${id}`, c.url, c.name, c.bots, now));
  }
  await env.DB.batch(stmts);
  return c;
}

export async function sourcesExtra(env) {
  const rows = await all(env.DB.prepare("SELECT id, url, name, type, bots FROM sources_extra ORDER BY added_at"));
  return { ok: true, sources: rows.map((r) => ({ ...r, bots: JSON.parse(r.bots || "[]"), every_days: 1, official: false, promoted: true })) };
}

// ---------------------------------------------------------------- 주간 보고
export async function weeklyReport(env, send = true) {
  const since = new Date(Date.now() - 7 * 86400000).toISOString();
  const runs = await all(env.DB.prepare("SELECT data FROM runs WHERE started_at >= ?").bind(since));
  const sum = {};
  for (const r of runs) for (const [k, v] of Object.entries(JSON.parse(r.data))) if (typeof v === "number") sum[k] = (sum[k] || 0) + v;
  const month = nowIso().slice(0, 7);
  const u = await usage(env, "GET", { month });
  const del = await env.DB.prepare("SELECT COUNT(*) AS n, SUM(ok) AS ok FROM deliveries WHERE sent_at >= ?").bind(since).first();
  const clicks = await all(env.DB.prepare("SELECT action, COUNT(*) AS n FROM clicks WHERE at >= ? GROUP BY action").bind(since));
  const report = { since: since.slice(0, 10), runs: runs.length, totals: sum, month_usd: Number(u.usd.toFixed(4)), deliveries: del, clicks };
  if (send) {
    const t = (k) => sum[k] || 0;
    const lines = [
      `📊 미국 알림 주간 보고 (${report.since}~${localDate(env)})`,
      `실행 ${runs.length}회 · 정보원 조회 ${t("sources_ok")}/${t("sources_total")} 성공 · 검색 ${t("queries")}회`,
      `수집 ${t("entries_fetched")}건 → 신규 ${t("entries_new")}건 (중복 제거 ${t("entries_seen")}건)`,
      `판정 ${t("ai_items")}건 → 행사 ${t("events_out")}건 · 대기열 ${t("outbox")}건`,
      `AI 입력 ${t("input_tokens")} / 출력 ${t("output_tokens")} 토큰 · 이번 달 $${report.month_usd}`,
      `발송 ${del.n || 0}건 (성공 ${del.ok || 0}) · 버튼 ${clicks.map((c) => `${c.action} ${c.n}`).join(", ") || "없음"}`,
    ];
    await notifyOwner(env, lines.join("\n"));
  }
  await kvSet(env, "last_report", report);
  return { ok: true, report };
}
