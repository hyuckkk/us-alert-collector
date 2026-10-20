// 공통: 봇 정의, 텔레그램 호출, 시간, D1 작은 도우미

export const BUILD = "us-0.2.0";
export const BOTS = {
  life: { label: "성인 활동", handle: "@ColoradoLife_bot", secret: "BOT_TOKEN_LIFE" },
  family: { label: "가족 활동", handle: "@ColoradoFamily_bot", secret: "BOT_TOKEN_FAMILY" },
  kids: { label: "어린이 활동", handle: "@ColoradoKids_bot", secret: "BOT_TOKEN_KIDS" },
};

export function botToken(env, bot) { return env[BOTS[bot].secret]; }
export function isOwner(env, id) { return !!env.OWNER_CHAT_ID && String(id) === String(env.OWNER_CHAT_ID); }
export function nowIso() { return new Date().toISOString(); }
export function json(body, status = 200) {
  return new Response(JSON.stringify(body, null, 2), { status, headers: { "content-type": "application/json; charset=utf-8" } });
}
// 1차 검증 전에는 소유자에게만 보낸다. wrangler.toml 의 DELIVERY_MODE 를 "all" 로 바꿔 배포해야 지인에게도 간다.
export function ownerOnly(env) { return (env.DELIVERY_MODE || "owner-only") !== "all"; }

// ---------------------------------------------------------------- 시간
function fmt(tz, opts, d = new Date()) {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: tz, hour12: false, ...opts }).format(d);
}
export function ownerTz(env) { return env.OWNER_TZ || "Asia/Seoul"; }
export function localStamp(env, d = new Date()) {
  const tz = ownerTz(env);
  return `${fmt(tz, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }, d)} (${tz})`;
}
export function localDate(env, d = new Date()) {
  return fmt(ownerTz(env), { year: "numeric", month: "2-digit", day: "2-digit" }, d);
}
export function dateIn(tz, d = new Date()) {
  return fmt(tz, { year: "numeric", month: "2-digit", day: "2-digit" }, d);
}
export function hourIn(tz, d = new Date()) {
  return parseInt(fmt(tz, { hour: "2-digit" }, d), 10) % 24;
}
export function weekdayIn(tz, d = new Date()) { // 0=일 … 6=토
  return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short" }).format(d));
}
export function daysBetween(fromYmd, toYmd) {
  return Math.round((Date.parse(toYmd + "T00:00:00Z") - Date.parse(fromYmd + "T00:00:00Z")) / 86400000);
}
// 정지 시간대: start > end 이면 자정을 넘는 구간(예 22~7)
export function inQuiet(hour, start, end) {
  if (start === end) return false;
  return start < end ? hour >= start && hour < end : hour >= start || hour < end;
}
export function validTz(tz) {
  try { new Intl.DateTimeFormat("en-US", { timeZone: tz }); return true; } catch { return false; }
}

// ---------------------------------------------------------------- 텔레그램
export async function tg(env, bot, method, payload) {
  const tok = botToken(env, bot);
  if (!tok) return { ok: false, description: `${BOTS[bot].secret} not set` };
  const base = env.TG_API || "https://api.telegram.org";
  try {
    const r = await fetch(`${base}/bot${tok}/${method}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload || {}),
    });
    try { return await r.json(); } catch { return { ok: false, description: `HTTP ${r.status}` }; }
  } catch (e) {
    return { ok: false, description: String(e && e.message).slice(0, 200) };
  }
}

// ---------------------------------------------------------------- D1 작은 도우미
export async function kvGet(env, k) {
  const r = await env.DB.prepare("SELECT v FROM kv WHERE k = ?").bind(k).first();
  return r ? r.v : null;
}
export async function kvSet(env, k, v) {
  await env.DB.prepare("INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at")
    .bind(k, typeof v === "string" ? v : JSON.stringify(v), nowIso()).run();
}
// 같은 key 로는 한 번만 true. 시스템 알림 중복 방지용.
export async function claimNotice(env, key) {
  const r = await env.DB.prepare("INSERT INTO notices (key, sent_at) VALUES (?, ?) ON CONFLICT(key) DO NOTHING").bind(key, nowIso()).run();
  return r.meta.changes === 1;
}
export async function notifyOwner(env, text, extra = {}) {
  if (!env.OWNER_CHAT_ID) return { ok: false, description: "OWNER_CHAT_ID not set" };
  return tg(env, env.SYSTEM_BOT || "life", "sendMessage", { chat_id: env.OWNER_CHAT_ID, text, disable_web_page_preview: true, ...extra });
}

// ---------------------------------------------------------------- 사람 설정
export function defaultPerson(env, chatId) {
  const owner = isOwner(env, chatId);
  return {
    chat_id: String(chatId),
    role: owner ? "owner" : "friend",
    tz: owner ? ownerTz(env) : "America/Denver",
    available_from: owner ? env.FAMILY_AVAILABLE_FROM || null : null,
    quiet_start: 22, quiet_end: 7, city: null,
  };
}
export async function ensurePerson(env, chatId) {
  const p = defaultPerson(env, chatId);
  await env.DB.prepare("INSERT INTO people (chat_id, role, tz, available_from, quiet_start, quiet_end, city, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id) DO NOTHING")
    .bind(p.chat_id, p.role, p.tz, p.available_from, p.quiet_start, p.quiet_end, p.city, nowIso()).run();
}
export async function getPerson(env, chatId) {
  return (await env.DB.prepare("SELECT * FROM people WHERE chat_id = ?").bind(String(chatId)).first()) || defaultPerson(env, chatId);
}
