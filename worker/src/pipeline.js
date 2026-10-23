// 수집기(GitHub Actions)와 사람별 발송 사이의 모든 상태를 D1 에 둔다.
// 원칙: 수집한 정보는 처리 전까지 대기열(pending)에 보존하고, 발송은 선점(claim) 후 보내며 결과를 확실/불명/실패로 나눠 기록한다.
// D1 무료 요금제의 호출당 쿼리 수 제한 때문에 여러 건은 json_each 로 한 번에 처리한다.

import { BOTS, isOwner, nowIso, ownerOnly, tg, sendHtml, claimNotice, notifyOwner, kvGet, kvSet, defaultPerson, ensurePerson, hourIn, weekdayIn, dateIn, inQuiet, localDate, ownerTz } from "./common.js";
import { renderEvent, renderCount, keyboard } from "./render.js";
import { tooFar } from "./geo.js";

// 다시 알려야 하는 '참여 기회에 영향을 주는' 필드 (1차 검증 1-5)
const IMPORTANT = ["start_date", "end_date", "time_text", "location", "cost_text", "signup", "registration_opens", "registration_status",
  "deadline", "eligibility", "age_text", "status_note"];
const FIELD_KO = { start_date: "시작일", end_date: "종료일", time_text: "시간", location: "장소", cost_text: "비용", signup: "신청 방식",
  registration_opens: "접수 시작", registration_status: "접수 상태", deadline: "마감", eligibility: "참가 자격", age_text: "대상 연령", status_note: "신분·주민 메모" };
const MAX_EVENTS_PER_CALL = 30;
const SEND_LIMIT = 20;            // 무료 요금제 하위 요청 50개 한도 안에서 여유
const DIGEST_SINGLES = Infinity;  // 소유자 지시(2026-10-22): 하루 건수 제한 없이 추천된 행사는 모두 한 통씩. 한 번 실행에서 못 다 보낸 것은 다음 매시 실행에서 이어 보냄
const DIGEST_CALLS = 25;          // 한 번 실행에서 보내는 요약 메시지 최대 수(넘으면 다음 매시 실행에서 이어 보냄)
const MAX_SEND_ATTEMPTS = 6;
const BACKOFF_MIN = [5, 30, 120, 360, 720];
const STALE_SENDING_MIN = 10;     // 'sending' 으로 이 시간 넘게 남으면 결과 불명으로 본다
const QUEUE_MAX_ATTEMPTS = 6;
const DENVER = "America/Denver";

async function sha(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}
function importantOf(d) { return Object.fromEntries(IMPORTANT.map((k) => [k, d[k] ?? null])); }
// 같은 내용을 AI 가 다르게 적은 것(예: "오후 4:30–9시" ↔ "4:30-9 p.m.", "York St." ↔ "York Street")은 바뀐 것이 아니다.
// 글로 된 필드는 숫자·핵심 표시만 비교하고, 날짜·접수 상태 같은 정해진 값은 그대로 비교한다(외부 검색 재입력 시험: 같은 행사가 '변경'으로 잡힘).
const TEXTY = new Set(["time_text", "location", "cost_text", "eligibility", "age_text", "status_note"]);
function canon(k, v) {
  if (v === null || v === undefined || v === "") return null;
  if (!TEXTY.has(k)) return String(v);
  const t = String(v).toLowerCase();
  const nums = (t.match(/\d+(?:[.:]\d+)?/g) || []).map((x) => x.replace(/^0+(?=\d)/, "")).join(" ");
  if (k === "cost_text") return `${/free|무료|\$0\b/.test(t) ? "free " : ""}${nums}`;
  if (k === "location") return t.replace(/[^a-z0-9가-힣]+/g, "").slice(0, 12);
  return nums || t.replace(/[^a-z0-9가-힣]+/g, "").slice(0, 20);
}
function materialDiff(oldD, newD) { return IMPORTANT.filter((k) => canon(k, oldD[k]) !== canon(k, newD[k])); }
function diffNote(oldD, newD) {
  return materialDiff(oldD, newD).map((k) => `${FIELD_KO[k]} ${oldD[k] ?? "미확인"} → ${newD[k] ?? "미확인"}`).join(", ");
}
const all = async (stmt) => (await stmt.all()).results;
const addMin = (iso, min) => new Date(Date.parse(iso) + min * 60000).toISOString();
const plusDays = (ymd, n) => new Date(Date.parse(ymd + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);

// ---------------------------------------------------------------- 이미 본 항목(0단계 호환)
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

// ---------------------------------------------------------------- 처리 대기열 (1차 검증 1-2, 3)
export async function queue(env, body) {
  const now = nowIso();
  const op = body.op;
  if (op === "check" || op === "add") {
    const items = (Array.isArray(body.items) ? body.items : []).filter((x) => x && x.key && x.hash).slice(0, op === "add" ? 40 : 500);
    const known = new Map((await all(env.DB.prepare("SELECT key, hash, status FROM pending WHERE key IN (SELECT value FROM json_each(?))")
      .bind(JSON.stringify(items.map((x) => x.key))))).map((r) => [r.key, r]));
    const fresh = items.filter((x) => !known.has(x.key)).map((x) => x.key);
    const changed = items.filter((x) => known.has(x.key) && known.get(x.key).hash !== x.hash).map((x) => x.key);
    if (op === "check") return { ok: true, new: fresh, changed };
    const write = items.filter((x) => !known.has(x.key) || known.get(x.key).hash !== x.hash)
      .map((x) => ({ ...x, url: x.url || "", body: JSON.stringify(x.body || {}), priority: x.priority | 0 }));
    if (write.length) {
      await env.DB.prepare(
        "INSERT INTO pending (key, hash, source_id, url, title, body, hint, priority, status, attempts, first_seen, updated_at) " +
        "SELECT json_extract(value,'$.key'), json_extract(value,'$.hash'), json_extract(value,'$.source_id'), json_extract(value,'$.url'), " +
        "json_extract(value,'$.title'), json_extract(value,'$.body'), json_extract(value,'$.hint'), json_extract(value,'$.priority'), 'queued', 0, ?1, ?1 " +
        "FROM json_each(?2) WHERE true ON CONFLICT(key) DO UPDATE SET hash = excluded.hash, url = excluded.url, title = excluded.title, body = excluded.body, " +
        "hint = COALESCE(excluded.hint, pending.hint), priority = MAX(pending.priority, excluded.priority), status = 'queued', attempts = 0, last_error = NULL, " +
        "result = NULL, lease_id = NULL, lease_until = NULL, updated_at = excluded.updated_at",
      ).bind(now, JSON.stringify(write)).run();
    }
    return { ok: true, new: fresh, changed, written: write.length };
  }
  // take: 꺼낸 항목에 임대를 건다(1차 재검증 A). 임대 중인 항목은 다른 실행·다음 페이지에서 나오지 않으므로
  // 여러 번 호출해 300건이든 그 이상이든 겹치지 않게 가져갈 수 있고, 동시 실행도 서로 다른 항목을 받는다.
  // 결과 보고(result)가 없으면 lease_minutes 뒤 다시 꺼낼 수 있다(실행이 중간에 죽은 경우).
  if (op === "take") {
    const limit = Math.min(Math.max(body.limit | 0, 1), 60);
    const until = addMin(now, Math.min(Math.max(body.lease_minutes | 0 || 30, 5), 180));
    const rows = await all(env.DB.prepare(
      "UPDATE pending SET lease_id = ?1, lease_until = ?2 WHERE key IN (SELECT key FROM pending WHERE status IN ('queued', 'extracted') " +
      "AND (lease_until IS NULL OR lease_until < ?3) ORDER BY CASE status WHEN 'extracted' THEN 0 ELSE 1 END, priority DESC, first_seen LIMIT ?4) " +
      "RETURNING key, hash, lease_id, source_id, url, title, body, hint, priority, status, attempts, result, first_seen").bind(crypto.randomUUID(), until, now, limit));
    rows.sort((a, b) => (a.status === "extracted" ? 0 : 1) - (b.status === "extracted" ? 0 : 1) || b.priority - a.priority || (a.first_seen < b.first_seen ? -1 : 1));
    return { ok: true, items: rows.map((r) => ({ ...r, body: JSON.parse(r.body || "{}"), result: r.result ? JSON.parse(r.result) : null })), lease_until: until };
  }
  // peek: 임대 없이 보기만(검증 전 정보 안내용)
  if (op === "peek") {
    const rows = await all(env.DB.prepare(
      "SELECT key, source_id, url, title, priority, status, attempts, first_seen FROM pending WHERE status = 'queued' ORDER BY priority DESC, first_seen LIMIT ?")
      .bind(Math.min(Math.max(body.limit | 0, 1), 400)));
    return { ok: true, items: rows };
  }
  // extend: 처리가 길어질 때 자기 임대만 연장(다른 실행이 가져가 AI 를 두 번 부르는 일을 줄임, 3차 검증 2)
  if (op === "extend") {
    const items = (Array.isArray(body.items) ? body.items : []).filter((x) => x && x.key && x.lease_id).slice(0, 400);
    const until = addMin(now, Math.min(Math.max(body.minutes | 0 || 30, 5), 180));
    const rows = items.length ? await all(env.DB.prepare(
      "UPDATE pending SET lease_until = ?1 FROM (SELECT value FROM json_each(?2)) AS j WHERE pending.key = json_extract(j.value,'$.key') " +
      "AND pending.lease_id = json_extract(j.value,'$.lease_id') AND pending.lease_until >= ?3 RETURNING pending.key AS key").bind(until, JSON.stringify(items), now)) : [];
    const ok = new Set(rows.map((r) => r.key));
    return { ok: true, extended: [...ok], lost: [...new Set(items.map((x) => x.key))].filter((k) => !ok.has(k)), lease_until: until };
  }
  // result: 임대 소유자만 보고할 수 있다(3차 검증 2). 꺼낼 때 받은 lease_id·hash 가 지금 값과 같고 임대가 아직 유효해야 반영하고,
  // 아니면(임대 만료 후 다른 실행이 가져감, 처리 중 내용이 바뀌어 다시 들어옴, 임대 없이 보고) 거부해 다른 실행의 상태를 지킨다.
  if (op === "result") {
    const items = (body.items || []).filter((x) => x && x.key && ["done", "extracted", "retry", "release"].includes(x.status)).slice(0, 100)
      .map((x) => ({ key: x.key, status: x.status, lease_id: x.lease_id || null, hash: x.hash || null,
        result: x.result ? JSON.stringify(x.result) : null, error: x.error ? String(x.error).slice(0, 300) : null }));
    if (!items.length) return { ok: true, updated: 0, rejected: [] };
    const rows = await all(env.DB.prepare(
      "UPDATE pending SET updated_at = ?1, " +
      // release: 처리하지 못하고 돌려놓기(상태·결과·시도 횟수 그대로, 임대만 해제)
      "status = CASE json_extract(j.value,'$.status') WHEN 'done' THEN 'done' WHEN 'extracted' THEN 'extracted' WHEN 'release' THEN pending.status " +
      "  ELSE CASE WHEN pending.attempts + 1 >= ?3 THEN 'stuck' ELSE 'queued' END END, " +
      "attempts = CASE json_extract(j.value,'$.status') WHEN 'retry' THEN pending.attempts + 1 ELSE pending.attempts END, " +
      "last_error = COALESCE(json_extract(j.value,'$.error'), pending.last_error), " +
      "result = CASE json_extract(j.value,'$.status') WHEN 'extracted' THEN json_extract(j.value,'$.result') WHEN 'done' THEN NULL ELSE pending.result END, " +
      "body = CASE json_extract(j.value,'$.status') WHEN 'done' THEN '{}' ELSE pending.body END, " + // 끝난 항목은 본문을 비워 공간 절약(키·해시는 남겨 중복 방지)
      // extracted 는 같은 실행이 Worker 등록 후 done 을 보고할 수 있게 임대를 유지한다
      "lease_id = CASE json_extract(j.value,'$.status') WHEN 'extracted' THEN pending.lease_id ELSE NULL END, " +
      "lease_until = CASE json_extract(j.value,'$.status') WHEN 'extracted' THEN pending.lease_until ELSE NULL END " +
      "FROM (SELECT value FROM json_each(?2)) AS j WHERE pending.key = json_extract(j.value,'$.key') " +
      "AND pending.lease_id = json_extract(j.value,'$.lease_id') AND pending.hash = json_extract(j.value,'$.hash') AND pending.lease_until >= ?1 " +
      "RETURNING pending.key AS key",
    ).bind(now, JSON.stringify(items), QUEUE_MAX_ATTEMPTS));
    const ok = new Set(rows.map((r) => r.key));
    return { ok: true, updated: ok.size, rejected: [...new Set(items.map((x) => x.key))].filter((k) => !ok.has(k)) };
  }
  if (op === "stats") return { ok: true, ...(await queueStats(env)) };
  return { ok: false, error: "unknown op" };
}

export async function queueStats(env) {
  const by = await all(env.DB.prepare("SELECT status, COUNT(*) AS n, MIN(first_seen) AS oldest FROM pending GROUP BY status"));
  const m = Object.fromEntries(by.map((r) => [r.status, r.n]));
  const q = by.find((r) => r.status === "queued");
  return { counts: m, queued: m.queued || 0, extracted: m.extracted || 0, stuck: m.stuck || 0,
    oldest_queued_hours: q && q.oldest ? Math.round((Date.now() - Date.parse(q.oldest)) / 3600000) : 0 };
}

// ---------------------------------------------------------------- 행사 목록(수집기의 같은 행사 판별용)
export async function eventIndex(env) {
  const rows = await all(env.DB.prepare(
    "SELECT event_key, bot, identity, url, start_date, end_date, json_extract(data,'$.title') AS title, json_extract(data,'$.title_ko') AS title_ko, " +
    "json_extract(data,'$.org') AS org, json_extract(data,'$.city') AS city, json_extract(data,'$.location') AS location, " +
    "json_extract(data,'$.cost_text') AS cost_text, source_id, first_seen FROM events " +
    "WHERE COALESCE(end_date, start_date, '9999-12-31') >= date('now', '-30 days') ORDER BY last_changed DESC LIMIT 2000"));
  return { ok: true, events: rows };
}

// ---------------------------------------------------------------- 운영 첫날 기준선(3차 검증 5): 쌓인 옛 정보를 한꺼번에 보내지 않기
// baseline_until 까지 들어온 새 행사는 조용히 등록하고(나중에 내용이 바뀌면 그때 정상 알림), 곧 행동이 필요한 것만 일간 요약에 넣는다.
const BASELINE_START_DAYS = 21;   // 3주 안에 열리거나 진행 중인 행사
const BASELINE_REG_DAYS = 14;     // 2주 안에 접수 시작·마감
export function baselineRelevant(e, today) {
  const within = (d, n) => d && d >= today && d <= plusDays(today, n);
  if (e.registration_status === "cancelled") return false;
  if (within(e.registration_opens, BASELINE_REG_DAYS) || within(e.deadline, BASELINE_REG_DAYS)) return true;
  const start = e.start_date, end = e.end_date || e.start_date;
  return !!(start && end && end >= today && start <= plusDays(today, BASELINE_START_DAYS));
}
// 배포 직후(마이그레이션이 'awaiting-start' 로 심어 둠)에는 운영자가 기준선을 시작·해제하기 전까지 기준선으로 본다(4차 검증 2).
// main 병합 직후 첫 자동 수집이 기준선 설정보다 먼저 돌아도 옛 정보·즉시 알림이 나가지 않게 하기 위함.
const BASELINE_AWAITING = "awaiting-start";
export async function baselineActive(env, now = nowIso()) {
  const v = await kvGet(env, "baseline_until");
  return v === BASELINE_AWAITING || !!(v && v > now);
}
export async function baseline(env, body = {}) {
  if (body.hours !== undefined) {
    const h = Math.min(Math.max(Number(body.hours) || 0, 0), 72);
    await kvSet(env, "baseline_until", h ? addMin(nowIso(), h * 60) : "");
  }
  const until = await kvGet(env, "baseline_until");
  const silent = await all(env.DB.prepare("SELECT COUNT(*) AS n FROM events WHERE json_extract(data,'$._baseline') = 'silent'"));
  return { ok: true, baseline_until: until || null, awaiting_start: until === BASELINE_AWAITING, active: await baselineActive(env), silent_events: silent[0].n,
    window: { start_days: BASELINE_START_DAYS, registration_days: BASELINE_REG_DAYS } };
}

// ---------------------------------------------------------------- 판정 결과 받기 → 사람별 대기열
function regState(e, today) {
  const st = e.registration_status;
  if (st === "cancelled") return null;
  if (e.registration_opens && e.registration_opens > today) {
    return Date.parse(e.registration_opens) - Date.parse(today) <= 14 * 86400000 ? ["upcoming-soon", 6] : ["upcoming", 48];
  }
  if (st === "open") return ["open", 24];
  if (st === "closed" || st === "soldout" || st === "waitlist") return ["closed", 72];
  return ["unknown", 72];
}

async function recipientsFor(env) {
  const recips = await all(env.DB.prepare(
    "SELECT r.chat_id, r.bot, p.available_from AS available_from, p.city AS city, p.radius_mi AS radius_mi, p.interests AS interests FROM recipients r LEFT JOIN people p ON p.chat_id = r.chat_id WHERE r.status = 'active'"));
  const mutedTags = new Map();
  for (const r of await all(env.DB.prepare("SELECT chat_id, tag FROM muted_tags"))) {
    if (!mutedTags.has(r.chat_id)) mutedTags.set(r.chat_id, new Set());
    mutedTags.get(r.chat_id).add(r.tag);
  }
  return { recips, mutedTags };
}

// 추천 수준에 따른 발송 여부(사람별 관심 분야 반영). 보내면 null, 아니면 사유
export function parseInterests(x) { try { const v = JSON.parse(x || "[]"); return Array.isArray(v) ? v : []; } catch { return []; } }
export function recLevel(e, r) {
  const lv = e.rec_level;
  // 적극 알림은 '왜 특별한지' 근거가 있어야 보낸다(소유자 기준: 정가·상시 공연·일상 세미나는 보내지 않음)
  if (lv === "push") return String(e.special_reason || "").trim() ? null : "no-special-reason";
  if (!lv) return "not-recommended-yet";
  if (e.category && parseInterests(r.interests).includes(e.category)) return null;
  return lv === "interest" ? "interest-only" : "not-recommended";
}

// 사람별 대상 판정. 반환: kind 또는 null(+사유)
function targetKind(env, r, e, states, mutedTags, opts = {}) {
  if (r.bot !== e.bot) return [null, null];
  if (ownerOnly(env) && !isOwner(env, r.chat_id)) return [null, "owner-only"];
  const st = states.get(`${r.chat_id}|${e.event_key}`);
  if (st === "MUTED") return [null, "muted"];
  if (opts.skipAcked && st === "ACKNOWLEDGED") return [null, "acknowledged"];
  const mt = mutedTags.get(String(r.chat_id));
  if (mt && [e.category, ...(e.tags || [])].some((t) => t && mt.has(t))) return [null, "muted-tag"];
  // 추천 판정: '적극 알림'만 기본 발송. '관심 설정 시'·'일반 제외'는 그 분야를 직접 고른 사람에게만. 판정이 없는 옛 행사는 보내지 않음(저장만)
  const lv = recLevel(e, r);
  if (lv) return [null, lv];
  const far = tooFar(r, e); // /city 기준 도시·반경(허가·추첨·연례 등은 예외)
  if (far) return [null, far];
  const from = r.available_from ?? defaultPerson(env, r.chat_id).available_from;
  const ref = e.end_date || e.start_date;
  let kind = opts.kind || (e.urgency === "now" ? "now" : "digest");
  if (from && ref && ref < from) {
    if (!e.annual) return [null, "before-available"];
    kind = "season"; // 참가 가능 시작일 이전의 연례행사: '내년에도 열릴 가능성' 예고
  }
  return [kind, null];
}

export async function enqueue(env, body) {
  const now = nowIso();
  const today = dateIn(DENVER);
  const input = (Array.isArray(body.events) ? body.events : [])
    .filter((e) => e && typeof e.event_key === "string" && e.event_key.length <= 48 && BOTS[e.bot] && /^https?:\/\//.test(e.url || "") && (e.title || e.title_ko))
    .slice(0, MAX_EVENTS_PER_CALL);
  const stats = { received: (body.events || []).length, accepted: input.length, new: 0, changed: 0, same: 0, recovered: 0, outbox: 0, watch: 0, skipped: {} };
  if (!input.length) return { ok: true, ...stats };
  const base = body.baseline === true || (await baselineActive(env, now));
  if (base) { stats.baseline = true; stats.baseline_silent = 0; stats.baseline_digest = 0; }

  const keys = input.map((e) => e.event_key);
  const existing = new Map((await all(env.DB.prepare("SELECT event_key, version, content_hash, fanout_version, data FROM events WHERE event_key IN (SELECT value FROM json_each(?))")
    .bind(JSON.stringify(keys)))).map((r) => [r.event_key, r]));

  // 받는 사람 판정에 필요한 것은 쓰기 전에 모두 읽어 둔다. 행사·항목·감시·알림 대기열은 아래에서 한 트랜잭션(batch)으로 쓴다(3차 검증 1).
  const { recips, mutedTags } = await recipientsFor(env);
  const states = new Map((await all(env.DB.prepare("SELECT chat_id, item_key, state FROM item_state WHERE item_key IN (SELECT value FROM json_each(?))").bind(JSON.stringify(keys))))
    .map((r) => [`${r.chat_id}|${r.item_key}`, r.state]));
  const rows = [];
  const fanout = [];
  const watches = [];
  for (const e0 of input) {
    const ex = existing.get(e0.event_key);
    // 새 추출에 값이 비어 있으면(그 페이지·입력에 그 정보가 이번엔 안 보였을 뿐) 기존 값을 지우지 않는다 → 정보가 사라진 것을 '변경'으로 알리지 않음
    const e = ex ? { ...e0, ...Object.fromEntries(IMPORTANT.filter((k) => (e0[k] ?? "") === "" && (JSON.parse(ex.data)[k] ?? "") !== "").map((k) => [k, JSON.parse(ex.data)[k]])) } : e0;
    const h = await sha(JSON.stringify(importantOf(e)));
    let version = 1, note = null;
    if (!ex) stats.new++;
    let changed = false;
    if (ex && ex.content_hash !== h) {
      if (materialDiff(JSON.parse(ex.data), e).length) { changed = true; version = ex.version + 1; note = diffNote(JSON.parse(ex.data), e); stats.changed++; }
      else { version = ex.version; stats.same++; stats.reworded = (stats.reworded || 0) + 1; } // 표현만 다름: 내용 갱신, 알림 없음
    } else if (ex) { version = ex.version; stats.same++; }
    rows.push({
      event_key: e.event_key, bot: e.bot, version, content_hash: h, data: JSON.stringify(e), urgency: e.urgency === "now" ? "now" : "weekly",
      start_date: e.start_date || null, end_date: e.end_date || null, annual: e.annual ? 1 : 0, tags: JSON.stringify(e.tags || []),
      source_id: e.source_id || null, url: e.url, note, tag: e.category || (e.tags || [])[0] || null, title: e.title_ko || e.title,
      identity: e.identity || null, registration_opens: e.registration_opens || null, deadline: e.deadline || null, fanout_version: version,
    });
    // 새 행사·바뀐 행사, 그리고 저장은 됐지만 이 판의 알림 생성이 끝나지 않은 행사(이전 실패·이전 판 코드)는 알림 대기열을 만든다.
    // 이미 만든(보낸) 사람 몫은 UNIQUE(chat_id, bot, event_key, version) 로 건너뛰므로 다시 보내지 않는다.
    if (base && !ex) {
      // 기준선 기간의 새 행사: 곧 행동할 것만 요약으로, 나머지는 조용히 등록(데이터에 표시만)
      if (baselineRelevant(e, today)) { fanout.push({ e, version, kind: "digest" }); stats.baseline_digest++; }
      else { rows[rows.length - 1].data = JSON.stringify({ ...e, _baseline: "silent" }); stats.baseline_silent++; }
    } else if (!ex || changed) fanout.push({ e, version });
    else if (ex.fanout_version == null || ex.fanout_version < version) { fanout.push({ e, version }); stats.recovered++; }
    // 상세 페이지 재검사 등록(뉴스 기사·검색 결과는 제외)
    const rs = regState(e, today);
    if (rs && !String(e.source_id || "").startsWith("gnews")) {
      watches.push({ url: e.url, event_keys: JSON.stringify([e.event_key]), source_id: e.source_id || null, reg_state: rs[0], interval_h: rs[1],
        next_check: addMin(now, rs[1] * 60), until_date: e.end_date || e.start_date || plusDays(today, 60) });
    }
  }

  const payload = JSON.stringify(rows);
  const stmts = [
    env.DB.prepare(
      "INSERT INTO events (event_key, bot, version, content_hash, data, urgency, start_date, end_date, annual, tags, source_id, url, first_seen, last_changed, change_note, identity, registration_opens, deadline, fanout_version) " +
      "SELECT json_extract(value,'$.event_key'), json_extract(value,'$.bot'), json_extract(value,'$.version'), json_extract(value,'$.content_hash'), json_extract(value,'$.data'), " +
      "json_extract(value,'$.urgency'), json_extract(value,'$.start_date'), json_extract(value,'$.end_date'), json_extract(value,'$.annual'), json_extract(value,'$.tags'), " +
      "json_extract(value,'$.source_id'), json_extract(value,'$.url'), ?1, ?1, json_extract(value,'$.note'), json_extract(value,'$.identity'), " +
      "json_extract(value,'$.registration_opens'), json_extract(value,'$.deadline'), json_extract(value,'$.fanout_version') FROM json_each(?2) WHERE true " +
      "ON CONFLICT(event_key) DO UPDATE SET data = excluded.data, urgency = excluded.urgency, tags = excluded.tags, url = excluded.url, " +
      "version = excluded.version, content_hash = excluded.content_hash, start_date = excluded.start_date, end_date = excluded.end_date, " +
      "identity = COALESCE(excluded.identity, events.identity), registration_opens = excluded.registration_opens, deadline = excluded.deadline, fanout_version = excluded.fanout_version, " +
      "last_changed = CASE WHEN events.content_hash <> excluded.content_hash THEN excluded.last_changed ELSE events.last_changed END, " +
      "change_note = CASE WHEN events.content_hash <> excluded.content_hash THEN excluded.change_note ELSE events.change_note END",
    ).bind(now, payload),
    env.DB.prepare(
      "INSERT INTO items (item_key, bot, title, tag, url, created_at) SELECT json_extract(value,'$.event_key'), json_extract(value,'$.bot'), json_extract(value,'$.title'), " +
      "json_extract(value,'$.tag'), json_extract(value,'$.url'), ?1 FROM json_each(?2) WHERE true " +
      "ON CONFLICT(item_key) DO UPDATE SET title = excluded.title, tag = excluded.tag, url = excluded.url",
    ).bind(now, payload),
  ];
  if (watches.length) {
    stmts.push(env.DB.prepare(
      "INSERT INTO watch (url, event_keys, source_id, reg_state, interval_h, next_check, until_date) SELECT json_extract(value,'$.url'), json_extract(value,'$.event_keys'), " +
      "json_extract(value,'$.source_id'), json_extract(value,'$.reg_state'), json_extract(value,'$.interval_h'), json_extract(value,'$.next_check'), json_extract(value,'$.until_date') " +
      "FROM json_each(?1) WHERE true ON CONFLICT(url) DO UPDATE SET " +
      // 같은 페이지의 다른 행사는 목록에 더한다(덮어쓰지 않음, 1차 재검증 F)
      "event_keys = (SELECT json_group_array(k) FROM (SELECT value AS k FROM json_each(watch.event_keys) UNION SELECT value FROM json_each(excluded.event_keys))), " +
      "reg_state = CASE WHEN excluded.interval_h < watch.interval_h THEN excluded.reg_state ELSE watch.reg_state END, interval_h = MIN(watch.interval_h, excluded.interval_h), " +
      "until_date = MAX(COALESCE(watch.until_date, ''), COALESCE(excluded.until_date, '')), next_check = MIN(watch.next_check, excluded.next_check)",
    ).bind(JSON.stringify(watches)));
    stats.watch = watches.length;
  }
  // 경로별 발견 기록(이슈 #20). 같은 행사를 다른 경로가 다시 보내도 경로마다 최초 발견 시각은 그대로 둔다.
  const finds = input.map((e) => ({ event_key: e.event_key, finder: finderOf(e), found_at: validIso(e.found_at), url: e.url,
    verified: e.verified === false ? 0 : 1, rec_level: e.rec_level || null, ext_level: e.ext_level || null, run_id: e.run_id || body.run_id || null }));
  stmts.push(env.DB.prepare(
    "INSERT INTO event_finds (event_key, finder, first_seen, found_at, last_seen, url, verified, rec_level, ext_level, run_id) " +
    "SELECT json_extract(value,'$.event_key'), json_extract(value,'$.finder'), ?1, json_extract(value,'$.found_at'), ?1, json_extract(value,'$.url'), " +
    "json_extract(value,'$.verified'), json_extract(value,'$.rec_level'), json_extract(value,'$.ext_level'), json_extract(value,'$.run_id') FROM json_each(?2) WHERE true " +
    "ON CONFLICT(event_key, finder) DO UPDATE SET last_seen = excluded.last_seen, rec_level = COALESCE(excluded.rec_level, event_finds.rec_level), " +
    "found_at = COALESCE(event_finds.found_at, excluded.found_at), verified = MAX(COALESCE(event_finds.verified, 0), excluded.verified), " +
    "ext_level = COALESCE(excluded.ext_level, event_finds.ext_level)",
  ).bind(now, JSON.stringify(finds)));
  const out = [];
  for (const { e, version, kind: force } of fanout) {
    for (const r of recips) {
      let [kind, why] = targetKind(env, r, e, states, mutedTags);
      if (kind && force && kind !== "season") kind = force;
      if (why) stats.skipped[why] = (stats.skipped[why] || 0) + 1;
      if (kind) out.push({ chat_id: String(r.chat_id), bot: e.bot, event_key: e.event_key, version, kind });
    }
  }
  if (out.length) stmts.push(outboxInsert(env, out));
  // D1 batch 는 한 트랜잭션: 하나라도 실패하면 행사 저장까지 모두 되돌아가 '행사만 있고 알림은 없는' 상태가 생기지 않는다.
  const res = await env.DB.batch(stmts);
  if (out.length) stats.outbox = res[res.length - 1].meta.changes;
  return { ok: true, ...stats };
}

export const FINDERS = ["github_collector", "claude", "chatgpt"];
// 발견 경로: 외부 입력은 수집기가 found_by 를 붙여 보낸다. 표시가 없는 예전 외부 입력은 chatgpt(이 경로만 있었음), 나머지는 GitHub 수집기.
function finderOf(e) {
  if (FINDERS.includes(e.found_by)) return e.found_by;
  return e.source_id === "ext-search" ? "chatgpt" : "github_collector";
}
function validIso(v) {
  return typeof v === "string" && !Number.isNaN(Date.parse(v)) ? new Date(Date.parse(v)).toISOString() : null;
}

function outboxInsert(env, out) {
  return env.DB.prepare(
    "INSERT INTO outbox (chat_id, bot, event_key, version, kind, created_at) SELECT json_extract(value,'$.chat_id'), json_extract(value,'$.bot'), " +
    "json_extract(value,'$.event_key'), json_extract(value,'$.version'), json_extract(value,'$.kind'), ?1 FROM json_each(?2) WHERE true " +
    "ON CONFLICT(chat_id, bot, event_key, version) DO NOTHING",
  ).bind(nowIso(), JSON.stringify(out));
}

// ---------------------------------------------------------------- 시간 알림: 오늘 접수 시작 / 내일 마감 (1차 검증 1-5)
// 같은 날 수십~수백 건이 몰려도 전부 처리한다(1차 재검증 C): 아직 기록이 없는 행사만 페이지 단위로 꺼내고,
// 판 올리기·알림 대기열·처리 기록을 한 트랜잭션(batch)으로 쓴다. 중간에 실패하면 셋 다 없던 일이 되어 다음 실행에서 다시 처리된다.
const TRIGGER_PAGE = 40;
const TRIGGER_PAGES_PER_CALL = 5;
async function timeTriggers(env) {
  const today = dateIn(DENVER);
  const tomorrow = plusDays(today, 1);
  let fired = 0, ctx = null;
  // 기준선 중에는 시간 알림도 즉시 보내지 않고 일간 요약에 넣는다(4차 검증 2). 처리 기록은 남겨 기준선이 끝난 뒤 같은 날 중복으로 나가지 않게 한다.
  const base = await baselineActive(env);
  for (let page = 0; page < TRIGGER_PAGES_PER_CALL; page++) {
    const evs = await all(env.DB.prepare(
      "SELECT e.event_key, e.bot, e.version, e.data, e.registration_opens, e.deadline FROM events e " +
      "WHERE (e.registration_opens = ?1 OR e.deadline = ?2) AND NOT EXISTS (SELECT 1 FROM trigger_log t WHERE t.event_key = e.event_key AND t.day = ?1) " +
      "AND COALESCE(json_extract(e.data, '$.date_uncertain'), 0) = 0 " + // 연도를 추정한 날짜로는 시간 알림을 걸지 않는다
      "ORDER BY e.event_key LIMIT ?3").bind(today, tomorrow, TRIGGER_PAGE));
    if (!evs.length) break;
    if (!ctx) ctx = await recipientsFor(env);
    const states = new Map((await all(env.DB.prepare("SELECT chat_id, item_key, state FROM item_state WHERE item_key IN (SELECT value FROM json_each(?))")
      .bind(JSON.stringify(evs.map((e) => e.event_key))))).map((r) => [`${r.chat_id}|${r.item_key}`, r.state]));
    const bumps = [], out = [], logs = [];
    for (const ev of evs) {
      const type = ev.registration_opens === today ? "open" : "deadline";
      const e = { ...JSON.parse(ev.data), event_key: ev.event_key, bot: ev.bot, urgency: "now" };
      const version = ev.version + 1;
      bumps.push({ event_key: ev.event_key, version, note: type === "open" ? "⏰ 오늘 접수 시작" : "⏰ 내일 신청 마감" });
      logs.push({ event_key: ev.event_key, type });
      for (const r of ctx.recips) {
        // 접수 시작은 확인한 사람에게도(신청 시점), 마감 알림은 확인하지 않은 사람에게만
        const [kind] = targetKind(env, r, e, states, ctx.mutedTags, { kind: "now", skipAcked: type === "deadline" });
        if (kind) out.push({ chat_id: String(r.chat_id), bot: e.bot, event_key: e.event_key, version, kind: kind === "season" ? "season" : base ? "digest" : "now" });
      }
    }
    const now = nowIso();
    const stmts = [
      env.DB.prepare("UPDATE events SET version = json_extract(j.value,'$.version'), fanout_version = json_extract(j.value,'$.version'), change_note = json_extract(j.value,'$.note'), last_changed = ?1 " +
        "FROM (SELECT value FROM json_each(?2)) AS j WHERE events.event_key = json_extract(j.value,'$.event_key')").bind(now, JSON.stringify(bumps)),
      env.DB.prepare("INSERT INTO trigger_log (event_key, day, type, at) SELECT json_extract(value,'$.event_key'), ?1, json_extract(value,'$.type'), ?2 " +
        "FROM json_each(?3) WHERE true ON CONFLICT(event_key, day) DO NOTHING").bind(today, now, JSON.stringify(logs)),
    ];
    if (out.length) stmts.push(outboxInsert(env, out));
    await env.DB.batch(stmts);
    fired += evs.length;
    if (evs.length < TRIGGER_PAGE) break;
  }
  return { fired, baseline: base };
}

// ---------------------------------------------------------------- 발송 결과 분류 (1차 검증 1-4)
function classify(d, row, now) {
  if (d.ok) return { status: "sent", message_id: d.result ? d.result.message_id : null, reason: null };
  const err = String(d.description || "").slice(0, 200);
  if (d.network) return { status: "uncertain", resend: 1, next_attempt_at: addMin(now, 10), reason: `불명: ${err}` };
  const code = d.error_code | 0;
  if (code === 429) return { status: "failed", next_attempt_at: addMin(now, Math.ceil(((d.parameters && d.parameters.retry_after) || 30) / 60) + 1), reason: err };
  if (code === 403) return { status: "dead", reason: err, blocked: true };
  if (code === 400) return { status: "dead", reason: err };
  if (row.attempts >= MAX_SEND_ATTEMPTS) return { status: "dead", reason: `재시도 한도: ${err}` };
  return { status: "failed", next_attempt_at: addMin(now, BACKOFF_MIN[Math.min(row.attempts - 1, BACKOFF_MIN.length - 1)] || 5), reason: err };
}

async function finish(env, updates) {
  if (!updates.length) return;
  const now = nowIso();
  const stmts = [
    env.DB.prepare(
      "UPDATE outbox SET status = json_extract(j.value,'$.status'), reason = json_extract(j.value,'$.reason'), " +
      "message_id = COALESCE(json_extract(j.value,'$.message_id'), outbox.message_id), next_attempt_at = json_extract(j.value,'$.next_attempt_at'), " +
      "resend = COALESCE(json_extract(j.value,'$.resend'), outbox.resend), claim = NULL, " +
      "sent_at = CASE WHEN json_extract(j.value,'$.status') = 'sent' THEN ?1 ELSE sent_at END FROM (SELECT value FROM json_each(?2)) AS j WHERE outbox.id = json_extract(j.value,'$.id')",
    ).bind(now, JSON.stringify(updates)),
    env.DB.prepare(
      "INSERT INTO deliveries (bot, chat_id, item_key, message_id, ok, error, sent_at) SELECT json_extract(value,'$.bot'), json_extract(value,'$.chat_id'), " +
      "json_extract(value,'$.event_key'), json_extract(value,'$.message_id'), json_extract(value,'$.status') = 'sent', json_extract(value,'$.reason'), ?1 " +
      "FROM json_each(?2) WHERE json_extract(value,'$.attempted') = 1",
    ).bind(now, JSON.stringify(updates)),
  ];
  const blocked = updates.filter((u) => u.blocked);
  if (blocked.length) {
    stmts.push(env.DB.prepare("UPDATE recipients SET status = 'blocked', updated_at = ?1 FROM (SELECT value FROM json_each(?2)) AS j " +
      "WHERE recipients.chat_id = json_extract(j.value,'$.chat_id') AND recipients.bot = json_extract(j.value,'$.bot')").bind(now, JSON.stringify(blocked)));
  }
  // D1 batch 는 한 트랜잭션이다. 텔레그램 발송 뒤 기록이 실패하면 한 번 더 시도하고, 그래도 실패하면
  // 행이 'sending' 으로 남아 10분 뒤 '성공 여부 불명'으로 바뀌고 표시를 붙여 1회 재전송된다(유실보다 표시된 중복을 택함).
  try { await env.DB.batch(stmts); } catch (e) { await env.DB.batch(stmts); }
  const dead = updates.filter((u) => u.status === "dead");
  if (dead.length) await notifyOwner(env, `⚠️ 발송 실패(재시도 중단) ${dead.length}건\n` + dead.slice(0, 5).map((u) => `- ${u.bot} ${u.event_key}: ${u.reason}`).join("\n"));
}

async function recoverStale(env) {
  const r = await env.DB.prepare("UPDATE outbox SET status = 'uncertain', resend = 1, next_attempt_at = ?1, claim = NULL, reason = '선점 후 결과 기록 없음' " +
    "WHERE status = 'sending' AND claimed_at < ?2").bind(nowIso(), addMin(nowIso(), -STALE_SENDING_MIN)).run();
  return r.meta.changes;
}

// 같은 행을 두 실행이 동시에 보내지 못하게, 아직 보낼 수 있는 상태인 행만 'sending' 으로 바꾸고 바뀐 행만 보낸다
// held: 하루 상한으로 보관한 행사(/more 로 사용자가 요청할 때만 꺼냄)
async function claimRows(env, ids, held = false) {
  if (!ids.length) return new Set();
  const now = nowIso();
  const claim = crypto.randomUUID();
  const rows = await all(env.DB.prepare(
    "UPDATE outbox SET status = 'sending', claim = ?1, claimed_at = ?2, attempts = attempts + 1 WHERE id IN (SELECT value FROM json_each(?3)) " +
    "AND (status = 'pending' OR (?4 = 1 AND status = 'held') OR (status IN ('failed', 'uncertain') AND (next_attempt_at IS NULL OR next_attempt_at <= ?2))) RETURNING id")
    .bind(claim, now, JSON.stringify(ids), held ? 1 : 0));
  return new Set(rows.map((r) => r.id));
}

function isExpired(data, tz) {
  const ref = data.end_date || data.start_date;
  return !!ref && ref < dateIn(tz || DENVER);
}

// ---------------------------------------------------------------- 즉시·예고 알림 발송 (정지 시간대는 미룸)
// 소유자는 /start 를 누르지 않아도 봇 3개의 수신자로 등록한다(운영 첫날: /start 에 응답이 없던 때라 수신자 0명 → 알림 0건).
// 처음 등록되는 봇에는 이미 저장된 행사 중 곧 행동할 것(3주 안 행사·2주 안 접수)을 일간 요약으로 한 번 넣는다. 나머지는 쏟아내지 않는다.
export async function ensureOwnerRecipients(env) {
  if (!env.OWNER_CHAT_ID) return { added: 0, queued: 0 };
  const owner = String(env.OWNER_CHAT_ID);
  const at = nowIso();
  const today = dateIn(DENVER);
  let added = 0, queued = 0;
  for (const bot of Object.keys(BOTS)) {
    const r = await env.DB.prepare("INSERT INTO recipients (chat_id, bot, status, joined_at, updated_at) VALUES (?, ?, 'active', ?, ?) ON CONFLICT(chat_id, bot) DO NOTHING")
      .bind(owner, bot, at, at).run();
    if (r.meta.changes !== 1) continue;
    added++;
    await ensurePerson(env, owner);
    const person = await env.DB.prepare("SELECT available_from FROM people WHERE chat_id = ?").bind(owner).first();
    const recip = { chat_id: owner, bot, available_from: person ? person.available_from : null };
    const evs = await all(env.DB.prepare("SELECT event_key, version, data FROM events WHERE bot = ?").bind(bot));
    const out = [];
    for (const ev of evs) {
      const e = { ...JSON.parse(ev.data), event_key: ev.event_key, bot };
      if (e.registration_status === "cancelled" || !baselineRelevant(e, today)) continue;
      const [kind] = targetKind(env, recip, e, new Map(), new Map(), { kind: "digest" });
      if (kind) out.push({ chat_id: owner, bot, event_key: ev.event_key, version: ev.version, kind: kind === "season" ? "season" : "digest" });
    }
    for (let i = 0; i < out.length; i += 200) await outboxInsert(env, out.slice(i, i + 200)).run();
    queued += out.length;
  }
  return { added, queued };
}

// 새로 승인되었거나 /stop 뒤 다시 시작한 지인에게: 쌓인 행사 수백 건을 보내지 않고, 2주 안에 접수가 열리거나 마감되는 것만(최대 15건) 요약으로 한 번.
// 그보다 먼 접수 시작일은 그날 ⏰ 알림이 따로 간다(시간 알림은 그 시점의 수신자에게 나감).
export async function welcomeQueue(env, chatId, bot) {
  const today = dateIn(DENVER);
  const until = plusDays(today, 14);
  const p = await env.DB.prepare("SELECT available_from, city, radius_mi FROM people WHERE chat_id = ?").bind(String(chatId)).first();
  const recip = { chat_id: String(chatId), bot, available_from: p ? p.available_from : null, city: p ? p.city : null, radius_mi: p ? p.radius_mi : null };
  const evs = await all(env.DB.prepare(
    "SELECT event_key, version, data FROM events WHERE bot = ?1 AND ((registration_opens >= ?2 AND registration_opens <= ?3) OR (deadline >= ?2 AND deadline <= ?3)) " +
    "ORDER BY MIN(COALESCE(registration_opens, '9999'), COALESCE(deadline, '9999')) LIMIT 40").bind(bot, today, until));
  const out = [];
  for (const ev of evs) {
    const e = { ...JSON.parse(ev.data), event_key: ev.event_key, bot };
    if (e.registration_status === "cancelled" || e.registration_status === "closed" || e.registration_status === "soldout") continue;
    const [kind] = targetKind(env, recip, e, new Map(), new Map(), { kind: "digest" });
    if (kind) out.push({ chat_id: String(chatId), bot, event_key: ev.event_key, version: ev.version, kind: "digest" });
    if (out.length >= 15) break;
  }
  if (out.length) await outboxInsert(env, out).run();
  return { queued: out.length };
}

export async function dispatch(env, opts = {}) {
  const limit = Math.min(opts.limit || SEND_LIMIT, SEND_LIMIT);
  const owner = await ensureOwnerRecipients(env);
  const trig = await timeTriggers(env);
  const stale = await recoverStale(env);
  const now = nowIso();
  const rows = await all(env.DB.prepare(
    "SELECT o.id, o.chat_id, o.bot, o.event_key, o.version, o.kind, o.attempts, o.resend, e.data, e.change_note, e.version AS cur_version, " +
    "p.tz, p.quiet_start, p.quiet_end, p.interests, s.state FROM outbox o JOIN events e ON e.event_key = o.event_key LEFT JOIN people p ON p.chat_id = o.chat_id " +
    "LEFT JOIN item_state s ON s.chat_id = o.chat_id AND s.item_key = o.event_key " +
    "WHERE o.kind IN ('now', 'season') AND (o.status = 'pending' OR (o.status IN ('failed', 'uncertain') AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?1))) " +
    "ORDER BY o.id LIMIT 100").bind(now));
  const r = { sent: 0, deferred: 0, skipped: 0, failed: 0, uncertain: 0, expired: 0, superseded: 0, triggers: trig.fired, recovered: stale };
  const done = [];
  const latest = new Map();
  for (const row of rows) latest.set(`${row.chat_id}|${row.event_key}`, Math.max(latest.get(`${row.chat_id}|${row.event_key}`) || 0, row.version));
  const sendable = [];
  for (const row of rows) {
    const data = JSON.parse(row.data);
    const p = { ...defaultPerson(env, row.chat_id), ...(row.tz ? { tz: row.tz, quiet_start: row.quiet_start, quiet_end: row.quiet_end } : {}) };
    if (ownerOnly(env) && !isOwner(env, row.chat_id)) { done.push({ id: row.id, status: "skipped", reason: "owner-only" }); r.skipped++; continue; }
    if (row.state === "MUTED") { done.push({ id: row.id, status: "skipped", reason: "muted" }); r.skipped++; continue; }
    // 보내기 직전 추천 판정 재확인: 대기열에 들어간 뒤 재평가로 '일반 제외'가 됐거나 판정 전에 쌓인 행사는 보내지 않는다(⏰·변경 알림 포함)
    { const why = recLevel(data, { interests: row.interests }); if (why) { done.push({ id: row.id, status: "skipped", reason: why }); r.skipped++; continue; } }
    if (row.version < latest.get(`${row.chat_id}|${row.event_key}`)) { done.push({ id: row.id, status: "superseded", reason: "새 판이 대신 감" }); r.superseded++; continue; }
    if (isExpired(data, p.tz)) { done.push({ id: row.id, status: "expired", reason: "이미 끝난 행사" }); r.expired++; continue; }
    if (!opts.ignoreQuiet && inQuiet(hourIn(p.tz), p.quiet_start, p.quiet_end)) { r.deferred++; continue; }
    sendable.push({ ...row, data });
  }
  const chosen = sendable.slice(0, limit);
  const claimed = await claimRows(env, chosen.map((x) => x.id));
  const now2 = nowIso();
  for (const row of chosen) {
    if (!claimed.has(row.id)) continue; // 다른 실행이 이미 가져감
    const note = row.version === row.cur_version ? row.change_note : null;
    let text = renderEvent(row.data, { kind: row.kind, version: row.version, note });
    if (row.resend) text = "🔁 재전송 (이전 전송의 성공 여부를 확인하지 못했습니다)\n" + text;
    const d = await sendHtml(env, row.bot, { chat_id: row.chat_id, text, reply_markup: keyboard(row.event_key, { state: row.state }, row.data.url) });
    const c = classify(d, { attempts: row.attempts + 1 }, now2);
    done.push({ id: row.id, ...c, attempted: 1, bot: row.bot, chat_id: row.chat_id, event_key: row.event_key });
    if (c.status === "sent") r.sent++; else if (c.status === "uncertain") r.uncertain++; else r.failed++;
  }
  await finish(env, done);
  const left = await env.DB.prepare("SELECT COUNT(*) AS n FROM outbox WHERE status IN ('pending', 'failed', 'uncertain', 'sending') AND kind IN ('now', 'season')").first();
  return { ok: true, ...r, owner_recipients: owner, pending: left.n };
}

function firstDate(d) {
  return [d.registration_opens, d.deadline, d.start_date].filter(Boolean).sort()[0] || "9999";
}
// 0: 2주 안 접수 시작·마감, 1: 1주 안에 열리는 행사, 2: 나머지
export function digestRank(d, today) {
  const within = (x, n) => x && x >= today && x <= plusDays(today, n);
  if (within(d.registration_opens, 14) || within(d.deadline, 14)) return 0;
  if (d.start_date && d.start_date <= plusDays(today, 7) && (d.end_date || d.start_date) >= today) return 1;
  return 2;
}

// ---------------------------------------------------------------- 요약 (기본: 매일 각자 현지 digest_hour 이후 1회, 여러 통으로 나눠 누락 없음)
export async function digest(env, opts = {}) {
  const now = nowIso();
  const groups = await all(env.DB.prepare(
    "SELECT o.chat_id, o.bot, p.tz, p.quiet_start, p.quiet_end, p.digest_mode, p.digest_hour, p.interests, " +
    "SUM(o.status = 'pending') AS fresh, SUM(o.status IN ('failed', 'uncertain') AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?1)) AS retry, " +
    "SUM(o.status = 'pending' AND o.reason = 'catchup') AS catchup " +
    "FROM outbox o LEFT JOIN people p ON p.chat_id = o.chat_id WHERE o.kind = 'digest' AND o.status IN ('pending', 'failed', 'uncertain') GROUP BY o.chat_id, o.bot").bind(now));
  const r = { messages: 0, items: 0, waiting: 0, skipped: 0, expired: 0, failed: 0 };
  let calls = 0;
  // 한 번 실행의 발송 수를 사람·봇마다 나눠 쓴다(한 사람의 많은 요약이 다른 사람 몫을 다 쓰지 않게). 남은 것은 다음 매시 실행에서
  const perGroup = Math.max(3, Math.floor(DIGEST_CALLS / Math.max(groups.length, 1)));
  for (const g of groups) {
    const groupStart = calls;
    const p = { ...defaultPerson(env, g.chat_id), ...(g.tz ? { tz: g.tz, quiet_start: g.quiet_start, quiet_end: g.quiet_end, digest_mode: g.digest_mode, digest_hour: g.digest_hour } : {}) };
    if ((ownerOnly(env) && !isOwner(env, g.chat_id)) || p.digest_mode === "off") {
      await env.DB.prepare("UPDATE outbox SET status = 'skipped', reason = ? WHERE kind = 'digest' AND status IN ('pending', 'failed', 'uncertain') AND chat_id = ? AND bot = ?")
        .bind(p.digest_mode === "off" ? "digest-off" : "owner-only", g.chat_id, g.bot).run();
      r.skipped++; continue;
    }
    if (!opts.force && inQuiet(hourIn(p.tz), p.quiet_start, p.quiet_end)) { r.waiting++; continue; }
    const dayOk = p.digest_mode === "weekly" ? weekdayIn(p.tz) === 6 : true;
    const timeOk = dayOk && hourIn(p.tz) >= (p.digest_hour ?? 8);
    let take = g.retry > 0 || g.catchup > 0; // 실패분 재시도·보충 발송은 요약 시각을 기다리지 않고 매시 실행에서
    if (opts.force || (g.fresh > 0 && timeOk && (await claimNotice(env, `digest:${g.chat_id}:${g.bot}:${dateIn(p.tz)}`)))) take = true;
    if (!take) { r.waiting++; continue; }
    const rows = await all(env.DB.prepare(
      "SELECT o.id, o.event_key, o.version, o.attempts, e.data, e.version AS cur_version, s.state FROM outbox o JOIN events e ON e.event_key = o.event_key " +
      "LEFT JOIN item_state s ON s.chat_id = o.chat_id AND s.item_key = o.event_key WHERE o.kind = 'digest' AND o.chat_id = ? AND o.bot = ? " +
      "AND (o.status = 'pending' OR (o.status IN ('failed', 'uncertain') AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?))) " +
      "ORDER BY COALESCE(e.start_date, '9999'), o.id LIMIT 300").bind(g.chat_id, g.bot, now));
    const done = [];
    const latest = new Map();
    for (const x of rows) latest.set(x.event_key, Math.max(latest.get(x.event_key) || 0, x.version));
    const live = [];
    const seenKey = new Set();
    for (const x of rows) {
      const data = JSON.parse(x.data);
      if (x.state === "MUTED") { done.push({ id: x.id, status: "skipped", reason: "muted" }); continue; }
      { const why = recLevel(data, { interests: g.interests }); if (why) { done.push({ id: x.id, status: "skipped", reason: why }); continue; } }
      if (x.version < latest.get(x.event_key) || seenKey.has(x.event_key)) { done.push({ id: x.id, status: "superseded", reason: "같은 행사 중복" }); continue; }
      if (isExpired(data, p.tz)) { done.push({ id: x.id, status: "expired", reason: "이미 끝난 행사" }); r.expired++; continue; }
      seenKey.add(x.event_key);
      live.push({ ...x, data });
    }
    // 중요한 것부터: 2주 안 접수 시작·마감 → 1주 안 행사 → 추천 점수 순으로 한 통씩(실행당 발송 수를 넘으면 다음 실행에서 이어서)
    const today = dateIn(DENVER);
    live.sort((a, b) => digestRank(a.data, today) - digestRank(b.data, today) || (Number(b.data.rec_score) || 0) - (Number(a.data.rec_score) || 0)
      || String(firstDate(a.data)).localeCompare(String(firstDate(b.data))));
    const singles = live.slice(0, DIGEST_SINGLES), rest = live.slice(DIGEST_SINGLES);
    let sentNow = 0;
    for (const x of singles) {
      if (calls >= DIGEST_CALLS || calls - groupStart >= perGroup) break;
      const claimed = await claimRows(env, [x.id]);
      if (!claimed.has(x.id)) continue;
      const d = await sendHtml(env, g.bot, { chat_id: g.chat_id, text: renderEvent(x.data, { kind: "digest" }), reply_markup: keyboard(x.event_key, { state: x.state }, x.data.url) });
      calls++;
      const c = classify(d, { attempts: x.attempts + 1 }, nowIso());
      done.push({ id: x.id, ...c, attempted: 1, bot: g.bot, chat_id: g.chat_id, event_key: x.event_key });
      if (c.status === "sent") { r.messages++; r.items++; sentNow++; } else r.failed++;
    }
    // 나머지는 보내지 않고 보관. 하루 한 번 짧은 개수 안내(버튼·주소 없음)만
    for (const x of rest) done.push({ id: x.id, status: "held", reason: "하루 알림 수 상한 — /more 로 조회" });
    r.held = (r.held || 0) + rest.length;
    if (rest.length && calls < DIGEST_CALLS && (await claimNotice(env, `count:${g.chat_id}:${g.bot}:${dateIn(p.tz)}`))) {
      await sendHtml(env, g.bot, { chat_id: g.chat_id, text: renderCount(BOTS[g.bot].label, { total: live.length, sent: sentNow, held: rest.length }) });
      calls++;
      r.messages++;
    }
    // 아직 보내지 못한 묶음은 다음 실행에서 시간 조건 없이 보내도록 retry 상태로 둔다
    const sentIds = new Set(done.map((u) => u.id));
    for (const x of live) if (!sentIds.has(x.id)) done.push({ id: x.id, status: "failed", reason: "다음 실행에서 이어 보냄", next_attempt_at: nowIso() });
    await finish(env, done);
  }
  return { ok: true, ...r };
}

// ---------------------------------------------------------------- 보충 발송(이슈 #20)
// 아직 소유자가 받지 못한 '지금도 유효한 적극 알림'(특별 근거 있음)을 한 통씩 보낸다. 같은 행사는 다시 보내지 않는다.
// 1) 예전 하루 5건 상한으로 보관만 된 것(held), 2) 추천 판정이 나중에 붙어 알림 대기열이 없던 것.
// 보충분은 요약 시각을 기다리지 않고 매시 실행(실행당 발송 수 안에서)으로 나간다. 조용한 시간·관심없음·끝난 행사는 그대로 지킨다.
export async function catchup(env, body = {}) {
  const today = dateIn(DENVER);
  const { recips, mutedTags } = await recipientsFor(env);
  const owners = recips.filter((r) => isOwner(env, r.chat_id));
  const owner = String(env.OWNER_CHAT_ID || "");
  const cand = await all(env.DB.prepare(
    "SELECT e.event_key, e.bot, e.version, e.data FROM events e WHERE json_extract(e.data, '$.rec_level') = 'push' " +
    "AND TRIM(COALESCE(json_extract(e.data, '$.special_reason'), '')) <> '' " +
    "AND MAX(COALESCE(e.end_date, ''), COALESCE(e.start_date, ''), COALESCE(e.registration_opens, ''), COALESCE(e.deadline, '')) >= ?1 " +
    "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.event_key = e.event_key AND o.chat_id = ?2 AND (o.status IN ('sent', 'sending', 'pending', 'uncertain') OR o.reason = 'muted')) " +
    "ORDER BY COALESCE(e.start_date, '9999') LIMIT 500").bind(today, owner));
  const states = new Map((await all(env.DB.prepare("SELECT chat_id, item_key, state FROM item_state WHERE chat_id = ?").bind(owner)))
    .map((r) => [`${r.chat_id}|${r.item_key}`, r.state]));
  const out = [], why = {};
  const heldIds = [];
  for (const ev of cand) {
    const e = { ...JSON.parse(ev.data), event_key: ev.event_key, bot: ev.bot };
    if (isExpired(e, "America/Denver")) { why.expired = (why.expired || 0) + 1; continue; }
    for (const r of owners) {
      const [kind, w] = targetKind(env, r, e, states, mutedTags, { skipAcked: true, kind: "digest" });
      if (w) why[w] = (why[w] || 0) + 1;
      if (kind) out.push({ chat_id: String(r.chat_id), bot: e.bot, event_key: e.event_key, version: ev.version, kind: "digest" });
    }
  }
  if (body.dry) return { ok: true, dry: true, candidates: cand.length, would_queue: out.length, skipped: why, events: out.map((x) => x.event_key) };
  // 같은 판의 행이 이미 있으면(예전 상한으로 보관 held, 추천 판정 전이라 건너뜀 skipped 등) 그 행을 보충분으로 되살린다.
  // 이 행사를 이미 보냈거나 보내는 중이면 위 후보에서 빠졌으므로 중복 발송은 없다.
  const keys = new Set(out.map((x) => `${x.event_key}|${x.version}`));
  const held = await all(env.DB.prepare("SELECT o.id, o.event_key, o.version FROM outbox o WHERE o.chat_id = ? AND o.status IN ('held', 'skipped', 'superseded', 'failed')")
    .bind(owner));
  for (const h of held) if (keys.has(`${h.event_key}|${h.version}`)) heldIds.push(h.id);
  const stmts = [];
  if (heldIds.length) stmts.push(env.DB.prepare("UPDATE outbox SET status = 'pending', reason = 'catchup', kind = 'digest', next_attempt_at = NULL WHERE id IN (SELECT value FROM json_each(?))").bind(JSON.stringify(heldIds)));
  if (out.length) {
    stmts.push(outboxInsert(env, out));
    stmts.push(env.DB.prepare("UPDATE outbox SET reason = 'catchup' WHERE status = 'pending' AND reason IS NULL AND kind = 'digest' AND chat_id = ? " +
      "AND event_key IN (SELECT value FROM json_each(?))").bind(owner, JSON.stringify(out.map((x) => x.event_key))));
  }
  if (stmts.length) await env.DB.batch(stmts);
  return { ok: true, candidates: cand.length, queued: out.length, revived_held: heldIds.length, skipped: why };
}

// ---------------------------------------------------------------- 사용자가 원할 때 더 보기(/more)
// 1) 하루 상한으로 보관된 행사(held)를 중요한 순서로, 2) 없으면 저장만 된 행사 중 이 사람에게 아직 안 보낸 것(추천 점수 순).
// 한 번에 5건까지, 각각 한 통씩. 이미 받은 행사·관심없음·끝난 행사는 빼고, 같은 행사는 다시 보내지 않는다.
export async function more(env, chatId, bot, n = 5) {
  const chat = String(chatId);
  const today = dateIn(DENVER);
  const held = await all(env.DB.prepare(
    "SELECT o.id, o.event_key, o.version, o.attempts, e.data, s.state FROM outbox o JOIN events e ON e.event_key = o.event_key " +
    "LEFT JOIN item_state s ON s.chat_id = o.chat_id AND s.item_key = o.event_key WHERE o.chat_id = ? AND o.bot = ? AND o.status = 'held' LIMIT 200").bind(chat, bot));
  let pick = held.map((x) => ({ ...x, data: JSON.parse(x.data) })).filter((x) => x.state !== "MUTED" && !isExpired(x.data, "America/Denver"));
  pick.sort((a, b) => digestRank(a.data, today) - digestRank(b.data, today) || (Number(b.data.rec_score) || 0) - (Number(a.data.rec_score) || 0));
  pick = pick.slice(0, n);
  let from = "held";
  if (!pick.length) {
    from = "stored";
    const evs = await all(env.DB.prepare(
      "SELECT e.event_key, e.version, e.data FROM events e WHERE e.bot = ?1 AND COALESCE(e.end_date, e.start_date, e.registration_opens, '9999-12-31') >= ?2 " +
      "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.chat_id = ?3 AND o.event_key = e.event_key AND o.status IN ('sent', 'sending', 'pending', 'held')) " +
      "AND NOT EXISTS (SELECT 1 FROM item_state s WHERE s.chat_id = ?3 AND s.item_key = e.event_key AND s.state = 'MUTED') " +
      "ORDER BY CAST(COALESCE(json_extract(e.data, '$.rec_score'), 0) AS INTEGER) DESC, COALESCE(e.start_date, '9999') LIMIT ?4").bind(bot, today, chat, n));
    if (evs.length) await outboxInsert(env, evs.map((e) => ({ chat_id: chat, bot, event_key: e.event_key, version: e.version, kind: "more" }))).run();
    const ids = await all(env.DB.prepare("SELECT o.id, o.event_key, o.version, o.attempts, e.data FROM outbox o JOIN events e ON e.event_key = o.event_key " +
      "WHERE o.chat_id = ? AND o.bot = ? AND o.kind = 'more' AND o.status = 'pending'").bind(chat, bot));
    pick = ids.map((x) => ({ ...x, data: JSON.parse(x.data) }));
  }
  if (!pick.length) return { sent: 0, from };
  const claimed = await claimRows(env, pick.map((x) => x.id), true);
  const done = [];
  let sent = 0;
  for (const x of pick) {
    if (!claimed.has(x.id)) continue;
    const d = await sendHtml(env, bot, { chat_id: chat, text: renderEvent(x.data, { kind: "more" }), reply_markup: keyboard(x.event_key, { state: x.state }, x.data.url) });
    const c = classify(d, { attempts: (x.attempts || 0) + 1 }, nowIso());
    done.push({ id: x.id, ...c, attempted: 1, bot, chat_id: chat, event_key: x.event_key });
    if (c.status === "sent") sent++;
  }
  await finish(env, done);
  return { sent, from };
}

// ---------------------------------------------------------------- 소유자 시험 발송: 실제 DB 의 대표 행사(추천 '적극 알림', 아직 안 끝난 것)를 실제 형식으로
// keys 를 주면 그 행사만(외부 연결 시험 등). 소유자에게만 보낸다
export async function preview(env, bot, n = 4, keys = null) {
  const owner = String(env.OWNER_CHAT_ID || "");
  if (!owner || !BOTS[bot]) return { ok: false, error: "owner or bot missing" };
  const today = dateIn(DENVER);
  const evs = Array.isArray(keys) && keys.length
    ? await all(env.DB.prepare("SELECT event_key, version, data FROM events WHERE event_key IN (SELECT value FROM json_each(?1)) LIMIT ?2").bind(JSON.stringify(keys.slice(0, 8)), n))
    : await all(env.DB.prepare(
      "SELECT event_key, version, data FROM events WHERE bot = ?1 AND json_extract(data, '$.rec_level') = 'push' " +
      "AND COALESCE(end_date, start_date, registration_opens, '9999-12-31') >= ?2 ORDER BY CAST(json_extract(data, '$.rec_score') AS INTEGER) DESC LIMIT ?3").bind(bot, today, n));
  const out = [];
  for (const ev of evs) {
    const data = JSON.parse(ev.data);
    const d = await sendHtml(env, BOTS[data.bot] ? data.bot : bot, { chat_id: owner, text: renderEvent(data, { kind: "digest" }), reply_markup: keyboard(ev.event_key, {}, data.url) });
    out.push({ event_key: ev.event_key, ok: !!d.ok, title: data.title_ko || data.title, error: d.ok ? undefined : d.description });
  }
  return { ok: out.every((x) => x.ok), sent: out.filter((x) => x.ok).length, items: out };
}

// ---------------------------------------------------------------- 추천 판정 결과만 갱신(재평가). 판·변경 알림·대기열은 건드리지 않는다
const REC_FIELDS = ["rec_level", "category", "rec_score", "summary_ko", "highlights", "rec_reason", "title_ko", "special_reason"];
export async function recommendUpdate(env, body) {
  const items = (body.items || []).filter((x) => x && x.event_key && ["push", "interest", "none"].includes(x.rec_level)).slice(0, 300);
  if (!items.length) return { ok: true, updated: 0 };
  const stmts = [];
  for (const x of items) {
    const patch = Object.fromEntries(REC_FIELDS.filter((k) => x[k] !== undefined).map((k) => [k, x[k]]));
    stmts.push(env.DB.prepare("UPDATE events SET data = json_patch(data, ?1), bot = CASE WHEN ?2 IN ('life','family','kids') THEN ?2 ELSE bot END WHERE event_key = ?3")
      .bind(JSON.stringify(patch), x.bot || "", x.event_key));
    stmts.push(env.DB.prepare("UPDATE items SET title = COALESCE(?1, title), tag = COALESCE(?2, tag), bot = CASE WHEN ?3 IN ('life','family','kids') THEN ?3 ELSE bot END WHERE item_key = ?4")
      .bind(x.title_ko || null, x.category || null, x.bot || "", x.event_key));
  }
  for (let i = 0; i < stmts.length; i += 100) await env.DB.batch(stmts.slice(i, i + 100));
  return { ok: true, updated: items.length };
}

// 재평가용: 행사 목록(행사 정보만). bot·sent(소유자에게 실제로 보낸 것만) 조건
export async function eventsDump(env, url) {
  const bot = url.searchParams.get("bot");
  const sent = url.searchParams.get("sent") === "1";
  const offset = Math.max(parseInt(url.searchParams.get("offset") || "0", 10) || 0, 0);
  const q = sent
    ? "SELECT DISTINCT e.event_key, e.source_id, e.url, e.data, e.bot FROM outbox o JOIN events e ON e.event_key = o.event_key WHERE o.status = 'sent' AND (?1 = '' OR o.bot = ?1) ORDER BY e.event_key LIMIT 300 OFFSET ?2"
    : "SELECT event_key, source_id, url, data, bot FROM events WHERE (?1 = '' OR bot = ?1) ORDER BY event_key LIMIT 300 OFFSET ?2";
  const rows = await all(env.DB.prepare(q).bind(bot || "", offset));
  return { ok: true, events: rows.map((r) => ({ ...JSON.parse(r.data), event_key: r.event_key, source_id: r.source_id, url: r.url, bot: r.bot })) };
}

// ---------------------------------------------------------------- 상세 페이지 재검사 목록 (1차 검증 1-3)
export async function watch(env, body) {
  const now = nowIso();
  if (body.op === "due") {
    await env.DB.prepare("DELETE FROM watch WHERE until_date IS NOT NULL AND until_date <> '' AND until_date < ?").bind(plusDays(dateIn(DENVER), -1)).run();
    const rows = await all(env.DB.prepare("SELECT url, event_keys, source_id, fingerprint, reg_state, interval_h, failures FROM watch WHERE next_check <= ? ORDER BY next_check LIMIT ?")
      .bind(now, Math.min(body.limit | 0 || 60, 150)));
    return { ok: true, items: rows.map((r) => ({ ...r, event_keys: JSON.parse(r.event_keys || "[]") })) };
  }
  if (body.op === "update") {
    const items = (body.items || []).filter((x) => x && x.url).slice(0, 150);
    if (!items.length) return { ok: true, updated: 0 };
    // 다음 검사 주기는 그 페이지 행사들의 현재 접수 상태 중 가장 짧은 것으로 다시 계산
    const rows = await all(env.DB.prepare("SELECT url, event_keys FROM watch WHERE url IN (SELECT value FROM json_each(?))").bind(JSON.stringify(items.map((x) => x.url))));
    const keys = [...new Set(rows.flatMap((r) => JSON.parse(r.event_keys || "[]")))];
    const evs = new Map((await all(env.DB.prepare("SELECT event_key, data FROM events WHERE event_key IN (SELECT value FROM json_each(?))").bind(JSON.stringify(keys))))
      .map((e) => [e.event_key, JSON.parse(e.data)]));
    const today = dateIn(DENVER);
    const interval = new Map(rows.map((r) => {
      const st = JSON.parse(r.event_keys || "[]").map((k) => evs.get(k)).filter(Boolean).map((e) => regState(e, today)).filter(Boolean);
      return [r.url, st.length ? st.reduce((a, b) => (b[1] < a[1] ? b : a)) : ["unknown", 72]];
    }));
    const payload = items.map((x) => ({ ...x, ok: x.ok ? 1 : 0, reg_state: (interval.get(x.url) || ["unknown", 72])[0], interval_h: (interval.get(x.url) || ["unknown", 72])[1] }));
    await env.DB.prepare(
      "UPDATE watch SET last_checked = ?1, reg_state = json_extract(j.value,'$.reg_state'), interval_h = json_extract(j.value,'$.interval_h'), " +
      "fingerprint = CASE WHEN json_extract(j.value,'$.ok') THEN json_extract(j.value,'$.fingerprint') ELSE watch.fingerprint END, " +
      "failures = CASE WHEN json_extract(j.value,'$.ok') THEN 0 ELSE watch.failures + 1 END, " +
      "next_check = CASE WHEN json_extract(j.value,'$.ok') THEN strftime('%Y-%m-%dT%H:%M:%fZ', ?1, '+' || json_extract(j.value,'$.interval_h') || ' hours') " +
      "  ELSE strftime('%Y-%m-%dT%H:%M:%fZ', ?1, '+' || MIN(json_extract(j.value,'$.interval_h') * (1 << MIN(watch.failures + 1, 4)), 168) || ' hours') END " +
      "FROM (SELECT value FROM json_each(?2)) AS j WHERE watch.url = json_extract(j.value,'$.url')",
    ).bind(now, JSON.stringify(payload)).run();
    return { ok: true, updated: items.length };
  }
  if (body.op === "stats") {
    return { ok: true, rows: await all(env.DB.prepare("SELECT reg_state, COUNT(*) AS n, SUM(failures > 0) AS failing FROM watch GROUP BY reg_state")) };
  }
  return { ok: false, error: "unknown op" };
}

// ---------------------------------------------------------------- 실행 기록, AI 상태, 정보원 상태, 실행 감시
export async function runLog(env, body) {
  if (!body.run_id) return { ok: false, error: "run_id required" };
  const now = nowIso();
  await env.DB.prepare("INSERT INTO runs (run_id, started_at, finished_at, data) VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET finished_at = excluded.finished_at, data = excluded.data")
    .bind(String(body.run_id), body.started_at || now, body.finished_at || null, JSON.stringify({ ...(body.metrics || {}), stages: body.stages || {} })).run();
  await kvSet(env, "last_run_at", now);
  if (body.completed) await kvSet(env, "last_collect_ok_at", now);
  const alerts = (body.alerts || []).filter(Boolean).slice(0, 8);

  // AI 상태: 성공 시 복구 알림, 실패 시 중단 시작 시각 유지
  if (body.ai && typeof body.ai.ok === "boolean") {
    const prev = JSON.parse((await kvGet(env, "ai_health")) || "{}");
    if (body.ai.ok) {
      if (prev.failing_since) {
        const h = Math.round((Date.now() - Date.parse(prev.failing_since)) / 3600000);
        alerts.push(`AI 분석 복구 (중단 약 ${h}시간, 실패 ${prev.consecutive || 0}회). 보존된 대기열부터 처리합니다.`);
      }
      await kvSet(env, "ai_health", { last_ok_at: now, failing_since: null, consecutive: 0 });
    } else {
      await kvSet(env, "ai_health", { ...prev, failing_since: prev.failing_since || now, consecutive: (prev.consecutive || 0) + 1, last_error: String(body.ai.error || "").slice(0, 200) });
    }
  }
  // 정보원 상태
  const src = (body.sources || []).filter((s) => s && s.id).slice(0, 200);
  if (src.length) {
    await env.DB.prepare(
      "INSERT INTO source_health (id, last_attempt_at, last_ok_at, last_status, consecutive_failures, last_items, events_total, details_ok, details_total) " +
      "SELECT json_extract(value,'$.id'), ?1, CASE WHEN json_extract(value,'$.ok') THEN ?1 END, json_extract(value,'$.status'), " +
      "CASE WHEN json_extract(value,'$.ok') THEN 0 ELSE 1 END, COALESCE(json_extract(value,'$.items'), 0), COALESCE(json_extract(value,'$.events'), 0), " +
      "COALESCE(json_extract(value,'$.details_ok'), 0), COALESCE(json_extract(value,'$.details_total'), 0) FROM json_each(?2) WHERE true " +
      "ON CONFLICT(id) DO UPDATE SET last_attempt_at = excluded.last_attempt_at, last_ok_at = COALESCE(excluded.last_ok_at, source_health.last_ok_at), " +
      "last_status = excluded.last_status, consecutive_failures = CASE WHEN excluded.last_ok_at IS NOT NULL THEN 0 ELSE source_health.consecutive_failures + 1 END, " +
      "last_items = excluded.last_items, events_total = source_health.events_total + excluded.events_total, " +
      "details_ok = source_health.details_ok + excluded.details_ok, details_total = source_health.details_total + excluded.details_total",
    ).bind(now, JSON.stringify(src.map((s) => ({ ...s, status: s.ok ? String(s.status || "ok") : String(s.error || s.status || "error").slice(0, 120) })))).run();
  }
  // 재시도 한도를 넘긴 대기열 항목
  const qs = await queueStats(env);
  if (qs.stuck && (await claimNotice(env, `stuck:${qs.stuck}:${localDate(env)}`))) alerts.push(`처리하지 못한 항목 ${qs.stuck}건이 재시도 한도를 넘어 보류 중입니다(삭제하지 않음).`);
  // 같은 경고(예: 접속이 계속 안 되는 핵심 정보원)는 하루 한 번만 알린다. 문구에서 실행 번호·숫자는 빼고 비교
  const fresh = [];
  for (const a of alerts) {
    const sig = String(a).replace(/\d+/g, "#").slice(0, 200);
    if (await claimNotice(env, `alert:${localDate(env)}:${sig}`)) fresh.push(a);
  }
  alerts.length = 0;
  alerts.push(...fresh);
  let notified = false;
  if (alerts.length && (await claimNotice(env, `run-alert:${body.run_id}`))) {
    await notifyOwner(env, `⚠️ 수집 실행 ${body.run_id}\n` + alerts.map((a) => `- ${String(a).slice(0, 300)}`).join("\n"));
    notified = true;
  }
  return { ok: true, notified, queue: qs };
}

export async function aiHealth(env) {
  return { ok: true, ...JSON.parse((await kvGet(env, "ai_health")) || "{}") };
}

// 수집 실행 감시 (1차 검증 5). GitHub Actions 매시 실행과, 소유자가 봇을 쓸 때(웹훅) 함께 확인한다.
export async function watchdog(env) {
  const last = await kvGet(env, "last_collect_ok_at");
  const lastRun = await kvGet(env, "last_run_at");
  const limit = Number(env.COLLECT_STALE_HOURS || 26);
  const hours = last ? (Date.now() - Date.parse(last)) / 3600000 : null;
  const stale = last !== null && hours > limit;
  let notified = false;
  if (stale && (await claimNotice(env, `watchdog:${localDate(env)}:${Math.floor(hourIn(ownerTz(env)) / 12)}`))) {
    await notifyOwner(env, `⚠️ 매일 수집이 ${Math.floor(hours)}시간째 성공 기록이 없습니다 (기준 ${limit}시간).\n마지막 실행 시작: ${lastRun || "없음"}\nGitHub us-alert-collector 저장소 Actions 탭의 collect 실행을 확인해 주세요. 매시 감시가 살아 있으면 자동으로 다시 실행합니다.`);
    notified = true;
  }
  return { ok: true, last_collect_ok_at: last, last_run_at: lastRun, hours_since: hours === null ? null : Math.round(hours * 10) / 10, stale, limit_hours: limit, notified };
}

// AI 가 오래 막혔을 때: 공식 정보원 새 글 제목·링크를 '검증 전 정보'로 소유자에게만 (1차 검증 4)
export async function unverified(env, body) {
  const items = (body.items || []).filter((x) => x && /^https?:\/\//.test(x.url || "")).slice(0, 60);
  if (!items.length) return { ok: true, sent: 0 };
  if (!(await claimNotice(env, `unverified:${localDate(env)}`))) return { ok: true, sent: 0, reason: "already today" };
  let sent = 0;
  for (let i = 0; i < items.length; i += 20) {
    const part = items.slice(i, i + 20);
    const head = `⚠️ 검증 전 정보 (AI 분석 ${body.outage_hours || "?"}시간째 중단, 대기 ${body.pending || items.length}건)\n날짜·대상·비용·자격은 확인되지 않았습니다. 원문 링크로 직접 확인해 주세요. 지인에게는 보내지 않습니다.`;
    const d = await notifyOwner(env, [head, "", ...part.map((x, j) => `${i + j + 1}. ${String(x.title || "").slice(0, 90)}\n   ${x.url}`)].join("\n"));
    if (d.ok) sent++;
  }
  return { ok: true, sent };
}

// ---------------------------------------------------------------- 사용량·예산
export async function usage(env, method, body, url) {
  const month = (url && url.searchParams.get("month")) || body.month || nowIso().slice(0, 7);
  if (method === "POST") {
    await env.DB.prepare(
      "INSERT INTO usage (month, model, input_tokens, output_tokens, usd, calls, cache_write_tokens, cache_read_tokens, searches, errors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) " +
      "ON CONFLICT(month, model) DO UPDATE SET input_tokens = usage.input_tokens + excluded.input_tokens, output_tokens = usage.output_tokens + excluded.output_tokens, " +
      "usd = usage.usd + excluded.usd, calls = usage.calls + excluded.calls, cache_write_tokens = usage.cache_write_tokens + excluded.cache_write_tokens, " +
      "cache_read_tokens = usage.cache_read_tokens + excluded.cache_read_tokens, searches = usage.searches + excluded.searches, errors = usage.errors + excluded.errors",
    ).bind(month, String(body.model || "?"), body.input_tokens | 0, body.output_tokens | 0, Number(body.usd) || 0, body.calls | 0,
      body.cache_write_tokens | 0, body.cache_read_tokens | 0, body.searches | 0, body.errors | 0).run();
  }
  const rows = await all(env.DB.prepare("SELECT * FROM usage WHERE month = ?").bind(month));
  const usd = rows.reduce((a, r) => a + r.usd, 0);
  // 사용액 경고(참고 크레딧의 50·80·100%). 경고만 하고 멈추지 않는다(이슈 #20: 내부 상한 제거).
  // 실제 정지는 Anthropic 이 크레딧 부족으로 호출을 거절할 때뿐이며, 그때도 AI 단계만 멈추고 수집·발송은 계속된다.
  const ref = Number(body.budget) || 0;
  if (method === "POST" && ref > 0) {
    for (const pct of [50, 80, 100]) {
      if (usd >= (ref * pct) / 100 && (await claimNotice(env, `budget:${month}:${pct}`))) {
        await notifyOwner(env, `💰 이번 달 미국 알림 API 사용액 $${usd.toFixed(2)} — 보유 크레딧 기준 $${ref}의 ${pct}%.\n` +
          "시스템이 스스로 멈추지는 않습니다. 크레딧이 실제로 바닥나면 AI 분석만 멈추고 수집·발송은 계속합니다. 카드 등록·충전·자동 충전은 하지 않습니다.");
      }
    }
  }
  return { ok: true, month, rows, usd };
}

// ---------------------------------------------------------------- 정보원 후보 (2회 이상 발견 + 자동 검증 통과 후 질문)
export async function candidates(env, body, url) {
  const now = nowIso();
  if (body.op === "unverified" || (url && url.searchParams.get("op") === "unverified")) {
    return { ok: true, items: await all(env.DB.prepare("SELECT id, url, name, bots, hits FROM candidates WHERE status = 'pending' AND hits >= 2 AND verified IS NULL LIMIT 10")) };
  }
  if (body.op === "verify") {
    const res = (body.results || []).filter((x) => x && x.id).slice(0, 10);
    let asked = 0;
    for (const v of res) {
      await env.DB.prepare("UPDATE candidates SET verified = ? WHERE id = ?").bind(JSON.stringify({ ...v.verified, checked_at: now }), v.id).run();
      const c = await env.DB.prepare("SELECT * FROM candidates WHERE id = ?").bind(v.id).first();
      if (!c || c.notified) continue;
      const vv = v.verified || {};
      const lines = [`🔎 정보원 후보 (${c.hits}회 발견)`, c.name || "", c.url, `이유: ${c.reason || "-"}`,
        `자동 검증: 조회 ${vv.fetchable ? "가능" : "불가"} · 행사 링크 ${vv.eventish ?? "?"}개 · 앞으로의 날짜 ${vv.future_dates ?? "?"}개 · 공식 도메인 ${vv.official ? "예" : "아니오"}${vv.duplicate_of ? ` · 기존 정보원과 같은 사이트(${vv.duplicate_of})` : ""}`];
      if (!vv.fetchable) lines.push("자동 조회가 막혀 있습니다. 추가하면 '대체 수집 방법 필요' 목록에 올립니다.");
      await notifyOwner(env, lines.join("\n"), {
        reply_markup: { inline_keyboard: [[{ text: "➕ 감시 목록에 추가", callback_data: `cpro:${c.id}` }, { text: "무시", callback_data: `cign:${c.id}` }]] },
      });
      await env.DB.prepare("UPDATE candidates SET notified = 1 WHERE id = ?").bind(c.id).run();
      asked++;
    }
    return { ok: true, asked };
  }
  const list = (body.candidates || []).filter((c) => c && /^https?:\/\//.test(c.url || "")).slice(0, 30);
  const rows = [];
  for (const c of list) rows.push({ id: (await sha(c.url.replace(/\/+$/, "").toLowerCase())).slice(0, 12), url: c.url, name: c.name || null, reason: c.reason || null, bots: JSON.stringify(c.bots || []) });
  if (rows.length) {
    await env.DB.prepare(
      "INSERT INTO candidates (id, url, name, reason, bots, hits, first_seen, last_seen) SELECT json_extract(value,'$.id'), json_extract(value,'$.url'), json_extract(value,'$.name'), " +
      "json_extract(value,'$.reason'), json_extract(value,'$.bots'), 1, ?1, ?1 FROM json_each(?2) WHERE true " +
      "ON CONFLICT(id) DO UPDATE SET hits = candidates.hits + 1, last_seen = excluded.last_seen, reason = excluded.reason",
    ).bind(now, JSON.stringify(rows)).run();
  }
  const waiting = await env.DB.prepare("SELECT COUNT(*) AS n FROM candidates WHERE status = 'pending' AND hits >= 2 AND verified IS NULL").first();
  return { ok: true, upserted: rows.length, awaiting_verification: waiting.n };
}

export async function decideCandidate(env, id, promote) {
  const c = await env.DB.prepare("SELECT * FROM candidates WHERE id = ?").bind(id).first();
  if (!c) return null;
  const now = nowIso();
  const v = JSON.parse(c.verified || "{}");
  const stmts = [env.DB.prepare("UPDATE candidates SET status = ? WHERE id = ?").bind(promote ? "promoted" : "ignored", id)];
  if (promote) {
    stmts.push(env.DB.prepare("INSERT INTO sources_extra (id, url, name, type, bots, added_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING")
      .bind(`cand-${id}`, c.url, c.name, v.type || "html", c.bots, now));
    if (v.fetchable === false) {
      stmts.push(env.DB.prepare("INSERT INTO source_health (id, alt) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET alt = excluded.alt").bind(`cand-${id}`, "자동 조회 불가 — 대체 수집 방법 필요"));
    }
  }
  await env.DB.batch(stmts);
  return c;
}

export async function sourcesExtra(env) {
  const rows = await all(env.DB.prepare("SELECT id, url, name, type, bots, mode, reason FROM sources_extra ORDER BY added_at"));
  return { ok: true, sources: rows.map((r) => ({ id: r.id, url: r.url, name: r.name, type: r.type, bots: JSON.parse(r.bots || "[]"), every_days: 1,
    official: /\.(gov|edu|us|org)(\/|$)/i.test(new URL(r.url).hostname + "/"), promoted: true, ...(r.mode ? { mode: r.mode } : {}) })) };
}

// ---------------------------------------------------------------- 놓친 검색 보완(외부 검색 대조)
// 외부 검색이 찾은 행사를 우리 수집이 놓쳤으면 원인을 기록하고, 그 행사를 다음부터 스스로 찾도록 정보원·검색어를 자동으로 더한다.
export async function gaps(env, method, body) {
  if (method === "POST") {
    const now = nowIso();
    const g = (body.gaps || []).filter((x) => x && x.event_key && x.url).slice(0, 30);
    const stmts = g.map((x) => env.DB.prepare("INSERT INTO ext_gaps (event_key, url, title, host, found, cause, action, at, finder, cause_code, rediscovered) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) " +
      "ON CONFLICT(event_key) DO UPDATE SET found = excluded.found, cause = excluded.cause, action = excluded.action, at = excluded.at, finder = excluded.finder, " +
      "cause_code = excluded.cause_code, rediscovered = excluded.rediscovered")
      .bind(x.event_key, x.url, x.title || null, x.host || null, x.found, x.cause || null, x.action || null, now, FINDERS.includes(x.finder) ? x.finder : null,
        CAUSE_CODES.includes(x.cause_code) ? x.cause_code : null, x.rediscovered === true ? 1 : x.rediscovered === false ? 0 : null));
    for (const src of (body.sources || []).filter((x) => x && /^https:\/\//.test(x.url || "")).slice(0, 10)) {
      stmts.push(env.DB.prepare("INSERT INTO sources_extra (id, url, name, type, bots, added_at, mode, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING")
        .bind(`gap-${(await sha(src.url.toLowerCase())).slice(0, 10)}`, src.url, src.name || null, ["html", "rss", "ical", "json"].includes(src.type) ? src.type : "html",
          JSON.stringify(src.bots || ["family", "kids", "life"]), now, src.mode === "page-change" ? "page-change" : null, String(src.reason || "").slice(0, 200)));
    }
    for (const q of (body.queries || []).filter((x) => x && x.query && x.query.length <= 120).slice(0, 10)) {
      stmts.push(env.DB.prepare("INSERT INTO queries_extra (query, bot, reason, added_at) VALUES (?, ?, ?, ?) ON CONFLICT(query) DO NOTHING")
        .bind(q.query.trim(), ["family", "kids", "life"].includes(q.bot) ? q.bot : "any", String(q.reason || "").slice(0, 200), now));
    }
    if (stmts.length) await env.DB.batch(stmts);
    return { ok: true, gaps: g.length };
  }
  const rows = await all(env.DB.prepare("SELECT * FROM ext_gaps ORDER BY at DESC LIMIT 200"));
  const by = {};
  for (const r of rows) by[r.found] = (by[r.found] || 0) + 1;
  return { ok: true, summary: by, gaps: rows };
}
export const CAUSE_CODES = ["missing_source", "query_gap", "scrape_or_js_failure", "freshness_or_schedule", "extraction_error",
  "eligibility_or_recommendation_error", "identity_duplicate_error", "delivery_error", "outside_scope", "uncertain"];

// ---------------------------------------------------------------- 3경로 대조·작업량·외부 입력 파일 (이슈 #20)
// 실행(경로)별 작업량 저장. 텔레그램으로 요약을 보내지 않고 조용히 쌓는다.
export async function workload(env, method, body, url) {
  if (method === "POST") {
    const w = body.workload || {};
    if (!w.run_id || !FINDERS.includes(w.path)) return { ok: false, error: "run_id and path required" };
    await env.DB.prepare("INSERT INTO workload (run_id, path, started_at, data) VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET data = excluded.data")
      .bind(String(w.run_id).slice(0, 80), w.path, validIso(w.started_at) || nowIso(), JSON.stringify(w).slice(0, 200000)).run();
    return { ok: true };
  }
  const since = (url && url.searchParams.get("since")) || "2026-10-01";
  const rows = await all(env.DB.prepare("SELECT run_id, path, started_at, data FROM workload WHERE started_at >= ? ORDER BY started_at").bind(since));
  return { ok: true, rows: rows.map((r) => ({ ...JSON.parse(r.data), run_id: r.run_id, path: r.path, started_at: r.started_at })) };
}

// 외부 입력 파일 선점: 같은 경로·같은 내용(sha)이 이미 처리됐거나 처리 중이면 거절 → 재실행·중복 push 에도 한 번만 처리
export async function inbox(env, body) {
  const path = String(body.path || "");
  if (!/^external-inbox\/(chatgpt|claude)\/[A-Za-z0-9._-]{1,100}\.json$/.test(path)) return { ok: false, error: "bad path" };
  const finder = path.split("/")[1];
  const now = nowIso();
  if (body.op === "claim") {
    const prev = await env.DB.prepare("SELECT sha, status, at FROM inbox_files WHERE path = ?").bind(path).first();
    // 처리 중으로 30분 넘게 남은 것(실행이 끊김)은 다시 처리할 수 있다
    if (prev && prev.sha === body.sha && (prev.status === "done" || (prev.status === "processing" && Date.now() - Date.parse(prev.at) < 30 * 60000))) {
      return { ok: true, claimed: false, status: prev.status };
    }
    await env.DB.prepare("INSERT INTO inbox_files (path, sha, finder, status, at) VALUES (?, ?, ?, 'processing', ?) ON CONFLICT(path) DO UPDATE SET sha = excluded.sha, status = 'processing', at = excluded.at")
      .bind(path, String(body.sha || "").slice(0, 80), finder, now).run();
    return { ok: true, claimed: true, finder };
  }
  if (body.op === "done") {
    await env.DB.prepare("UPDATE inbox_files SET status = ?, at = ?, report = ? WHERE path = ?")
      .bind(body.failed ? "failed" : "done", now, JSON.stringify(body.report || {}).slice(0, 20000), path).run();
    return { ok: true };
  }
  return { ok: false, error: "unknown op" };
}

// 3경로 대조: 행사마다 경로별 최초 발견·검증·추천과 실제 발송(소유자)을 모은다
export async function compare(env, url) {
  const since = (url && url.searchParams.get("since")) || "2026-10-01";
  const rows = await all(env.DB.prepare(
    "SELECT f.event_key, f.finder, f.first_seen, f.found_at, f.url, f.verified, f.rec_level, f.ext_level, e.bot, json_extract(e.data, '$.title_ko') AS title, " +
    "json_extract(e.data, '$.rec_level') AS final_level, json_extract(e.data, '$.special_reason') AS special_reason, " +
    "(SELECT MIN(o.sent_at) FROM outbox o WHERE o.event_key = f.event_key AND o.status = 'sent' AND o.chat_id = ?2) AS sent_at, " +
    "g.found AS gap_found, g.cause_code, g.cause, g.action, g.rediscovered " +
    "FROM event_finds f LEFT JOIN events e ON e.event_key = f.event_key LEFT JOIN ext_gaps g ON g.event_key = f.event_key " +
    "WHERE f.event_key IN (SELECT event_key FROM event_finds WHERE first_seen >= ?1) ORDER BY f.event_key, f.first_seen")
    .bind(since, String(env.OWNER_CHAT_ID || "")));
  const events = new Map();
  for (const r of rows) {
    const x = events.get(r.event_key) || { event_key: r.event_key, title: r.title, bot: r.bot, final_level: r.final_level, special_reason: r.special_reason,
      sent_at: r.sent_at, gap: r.gap_found ? { found: r.gap_found, cause_code: r.cause_code, cause: r.cause, action: r.action, rediscovered: r.rediscovered } : null, finds: {} };
    x.finds[r.finder] = { first_seen: r.first_seen, found_at: r.found_at, url: r.url, verified: r.verified, rec_level: r.rec_level, ext_level: r.ext_level };
    events.set(r.event_key, x);
  }
  const list = [...events.values()];
  for (const x of list) {
    const order = Object.entries(x.finds).sort((a, b) => (a[1].found_at || a[1].first_seen).localeCompare(b[1].found_at || b[1].first_seen));
    x.first_finder = order[0][0];
    // 발송을 일으킨 경로: 발송 시각 이전에 찾은 경로 중 가장 먼저 찾은 경로
    x.sent_via = x.sent_at ? (order.find(([, f]) => f.first_seen <= x.sent_at) || order[0])[0] : null;
  }
  const s = { events: list.length, by_finder: {}, only: {}, overlap: {}, first: {}, sent_via: {}, push_by_finder: {}, causes: {} };
  for (const x of list) {
    const fs = Object.keys(x.finds).sort();
    for (const f of fs) {
      s.by_finder[f] = (s.by_finder[f] || 0) + 1;
      if (x.final_level === "push") s.push_by_finder[f] = (s.push_by_finder[f] || 0) + 1;
    }
    if (fs.length === 1) s.only[fs[0]] = (s.only[fs[0]] || 0) + 1;
    else s.overlap[fs.join("+")] = (s.overlap[fs.join("+")] || 0) + 1;
    s.first[x.first_finder] = (s.first[x.first_finder] || 0) + 1;
    if (x.sent_via) s.sent_via[x.sent_via] = (s.sent_via[x.sent_via] || 0) + 1;
    if (x.gap && x.gap.cause_code) s.causes[x.gap.cause_code] = (s.causes[x.gap.cause_code] || 0) + 1;
  }
  return { ok: true, since, summary: s, events: list };
}

// 누적 내보내기(JSON). CSV·Markdown 은 scripts/export_eval.py 가 이 결과로 만든다.
export async function exportAll(env, url) {
  const [c, w, g, u] = await Promise.all([compare(env, url), workload(env, "GET", {}, url),
    all(env.DB.prepare("SELECT * FROM ext_gaps ORDER BY at")), all(env.DB.prepare("SELECT * FROM usage ORDER BY month, model"))]);
  const extra = { sources: await all(env.DB.prepare("SELECT id, url, name, type, mode, reason, added_at FROM sources_extra ORDER BY added_at")),
    queries: await all(env.DB.prepare("SELECT query, bot, reason, added_at FROM queries_extra ORDER BY added_at")),
    failing_sources: await all(env.DB.prepare("SELECT id, consecutive_failures, last_status, last_ok_at FROM source_health WHERE consecutive_failures >= 3 ORDER BY consecutive_failures DESC")),
    inbox: await all(env.DB.prepare("SELECT path, finder, status, at FROM inbox_files ORDER BY at")) };
  return { ok: true, generated_at: nowIso(), compare: c, workload: w.rows, gaps: g, usage: u, ...extra };
}

export async function queriesExtra(env) {
  return { ok: true, queries: await all(env.DB.prepare("SELECT query, bot FROM queries_extra ORDER BY added_at DESC LIMIT 80")) };
}
// 수집 대기열에 그 주소가 이미 있는지(수집은 했지만 아직 AI 처리 전인지)
export async function queueFind(env, urls) {
  const list = (urls || []).filter((u) => typeof u === "string").slice(0, 30);
  if (!list.length) return { ok: true, items: [] };
  return { ok: true, items: await all(env.DB.prepare("SELECT url, status, source_id FROM pending WHERE url IN (SELECT value FROM json_each(?))").bind(JSON.stringify(list))) };
}

export async function sourceHealth(env) {
  return { ok: true, sources: await all(env.DB.prepare("SELECT * FROM source_health ORDER BY consecutive_failures DESC, id")) };
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
  const failing = await all(env.DB.prepare("SELECT id, consecutive_failures, last_status FROM source_health WHERE consecutive_failures >= 3 ORDER BY consecutive_failures DESC LIMIT 8"));
  const qs = await queueStats(env);
  const ob = await all(env.DB.prepare("SELECT status, COUNT(*) AS n FROM outbox WHERE created_at >= ? GROUP BY status").bind(since));
  const gapRows = await all(env.DB.prepare("SELECT found, COUNT(*) AS n FROM ext_gaps WHERE at >= ? GROUP BY found").bind(since));
  const report = { since: since.slice(0, 10), runs: runs.length, totals: sum, month_usd: Number(u.usd.toFixed(4)), deliveries: del, clicks, failing, queue: qs, outbox: ob,
    external_gaps: Object.fromEntries(gapRows.map((x) => [x.found, x.n])) };
  if (send) {
    const t = (k) => sum[k] || 0;
    const lines = [
      `📊 미국 알림 주간 보고 (${report.since}~${localDate(env)})`,
      `실행 ${runs.length}회 · 정보원 조회 ${t("sources_ok")}/${t("sources_total")} 성공 · 검색 ${t("queries")}회 · AI 발굴 ${t("discover_calls")}회`,
      `수집 ${t("entries_fetched")}건 → 신규 ${t("entries_new")}건 · 상세 재검사 ${t("watch_checked")}건(변경 ${t("watch_changed")})`,
      `AI 처리 ${t("ai_items")}건 → 행사 ${t("events_out")}건 · 누락 재시도 ${t("ai_missing")}건`,
      `대기열 ${qs.queued}건(가장 오래 ${qs.oldest_queued_hours}시간) · 보류 ${qs.stuck}건`,
      `AI 입력 ${t("input_tokens")} / 출력 ${t("output_tokens")} 토큰 · 검색 ${t("searches")}회 · 이번 달 $${report.month_usd}`,
      `발송 ${del.n || 0}건 (성공 ${del.ok || 0}) · 버튼 ${clicks.map((c) => `${c.action} ${c.n}`).join(", ") || "없음"}`,
      failing.length ? `연속 실패 정보원: ${failing.map((f) => `${f.id}(${f.consecutive_failures})`).join(", ")}` : "연속 실패 정보원 없음",
      Object.keys(report.external_gaps).length
        ? `외부 검색 대조: ${Object.entries(report.external_gaps).map(([k, n]) => `${{ found: "우리도 찾음", queued: "수집 후 처리 대기", "registered-missed": "등록 정보원인데 놓침", unregistered: "정보원 없음" }[k] || k} ${n}`).join(" · ")} (놓친 것은 정보원·검색어 자동 보완)`
        : "외부 검색 대조: 이번 주 없음",
    ];
    await notifyOwner(env, lines.join("\n"));
  }
  await kvSet(env, "last_report", report);
  return { ok: true, report };
}
