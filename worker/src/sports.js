import { PAGE } from './sports-page.js';
import { tg, claimNotice, isOwner, ownerOnly } from "./common.js";
import { venueTravel } from "./sports-travel.js";

// Read-only public school/club sports calendar. Do NOT export people, chat ids, or admin data.
const ZONE = "America/Denver";
const SPORTS = /\b(hockey|soccer|football|baseball|basketball|volleyball|softball|tennis|wrestling|lacrosse|rugby|gymnastics|swimming|water polo|field hockey|cross.country|track.and.field|futsal|badminton)\b|농구|배구|야구|축구|아이스하키|하키|풋볼|미식축구|레슬링|테니스|수영|체조|육상/i;
const VS = /\b(?:vs\.?|v\.|versus|against)\b|(?:\s+@\s+)|\s+대\s+|대항전/i;
const ATHLETIC_SOURCE = /athlet|sport.?club|club.?sports|soccer|hockey|high.?school.?sport|school.?athlet|varsity|^cu-.*club/i;
const UNIVERSITY = /cu-|boulder|university.of.denver|du-athlet|csu-|colorado.state|mines-|air.force|afa-|university|college|sport.?club/i;
const GRADE = {
  "elementary": /elementary|초등|grade.?school|little.league|u[6-9]\b/i,
  "middle": /middle.?school|junior.?high|중학/i,
  "high": /high.?school|prep.?school|고등|varsity|jv\b/i
};
const SCHOOL_LABEL = { elementary: "초등", middle: "중등", high: "고등", university: "대학", unknown: "학교급 미확인" };
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
const CORS = { "Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=600", "Content-Type": "application/json; charset=utf-8", "X-Content-Type-Options": "nosniff" };
function monthShift(m, by) {
  const [y, n] = m.split("-").map(Number);
  const d = new Date(Date.UTC(y, n - 1 + by, 1));
  return d.toISOString().slice(0, 7);
}
function denverToday(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: ZONE, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now);
  const get = (type) => parts.find((x) => x.type === type).value;
  return get("year") + "-" + get("month") + "-" + get("day");
}
function schoolGrade(event, source) {
  const hint = [event.school_level, event.grade_level, event.school_type, event.org, source, event.title].filter(Boolean).join(" ");
  if (/^(elementary|middle|high|university)$/.test(String(event.school_level || ""))) return event.school_level;
  for (const [level, re] of Object.entries(GRADE)) if (re.test(hint)) return level;
  if (UNIVERSITY.test(hint)) return "university";
  return "unknown";
}
function sportName(e) {
  const tags = Array.isArray(e.tags) ? e.tags.join(" ") : "";
  const hint = [e.sport, e.title, e.title_ko, tags].filter(Boolean).join(" ");
  const m = hint.match(SPORTS);
  const word = m ? m[0].toLowerCase().replace(/[\s.-]+/g, " ") : "";
  const names = {
    hockey: "아이스하키", soccer: "축구", football: "미식축구", baseball: "야구",
    basketball: "농구", volleyball: "배구", softball: "소프트볼",
    tennis: "테니스", wrestling: "레슬링", lacrosse: "라크로스",
    rugby: "럭비", gymnastics: "체조", swimming: "수영",
    "water polo": "수구", "field hockey": "필드하키",
    "cross country": "크로스컨트리", "track and field": "육상",
    futsal: "풋살", badminton: "배드민턴"
  };
  return names[word] || (m ? m[0] : "종목 미확인");
}
function costClass(raw) {
  const s = String(raw || "").trim();
  if (!s || /별도 확인|미정|not (?:listed|available)|unknown|tbd/i.test(s)) return "unknown";
  if (/(^|[\s,(])(?:free|complimentary|no (?:admission|charge|fee)|무료)(?=[\s.,)]|$)/i.test(s) || /(^|\s)\$0(?:\.00)?(\s|$)/.test(s)) {
    if (!/(유료|paid|admission\s*\$\d+|\$[1-9]\d*)/i.test(s)) return "free";
  }
  if (/(유료|admission\s*\$[1-9]|\$\s*[1-9]\d*|ticket.{0,15}\$[1-9]|paid admission)/i.test(s)) return "paid";
  return "unknown";
}
function matchup(e) {
  if (["meet", "tournament"].includes(e.event_type)) return { home: "", away: "" };
  if (e.home_team && e.away_team) return { home: e.home_team, away: e.away_team };
  const title = String(e.title || e.title_ko || "");
  const m = title.match(/^(.*?)\s+(?:vs\.?|v\.|versus|against)\s+(.*)$/i);
  if (m) return { home: m[1].trim(), away: m[2].trim() };
  const a = title.match(/^(.*?)\s+@\s+(.*?)$/i);
  if (a) return { home: a[2].trim(), away: a[1].trim() };
  return { home: e.home_team || "", away: e.away_team || "" };
}
export function isCalendarMatch(e, sourceOverride = "") {
  const src = String(sourceOverride || e.source_id || "");
  const title = [e.title, e.title_ko].filter(Boolean).join(" ");
  const tags = Array.isArray(e.tags) ? e.tags.join(" ") : "";
  const school = schoolGrade(e, src);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(e.start_date || "") || !Number.isFinite(Date.parse(e.start_date + "T12:00:00Z")) || new Date(e.start_date + "T12:00:00Z").toISOString().slice(0, 10) !== e.start_date) return false;
  if (e.public_access === false || e.spectator_access === "private" || /^(class|camp|registration|practice|tryout)$/.test(e.event_type || "")) return false;
  if (/\b(camp|clinic|lesson|practice|tryout|season tickets?|registration)\b|강습|레슨|등록 모집/i.test(title) && !["match", "meet", "tournament"].includes(e.event_type)) return false;
  const structured = ["match", "meet", "tournament"].includes(e.event_type);
  const competition = /\b(meet|invitational|triangular|quad|championship|tournament)\b|대회/i.test(title);
  return !!(e.start_date && /^\d{4}-\d{2}-\d{2}$/.test(e.start_date)
    && (ATHLETIC_SOURCE.test(src) || school !== "unknown")
    && (SPORTS.test([e.sport, title, tags].join(" ")) || ATHLETIC_SOURCE.test(src))
    && (structured || VS.test(title) || e.home_team && e.away_team || competition || /(?:match|game|대항|경기)/i.test(title) && ATHLETIC_SOURCE.test(src)));
}
export function isRoutineSportsMatch(e) {
  if (!isCalendarMatch(e)) return false;
  // A championship name alone is not proof of a special visit/experience.
  return !e.sports_special_verified;
}
export function rowAsCalendar(e, r) {
  const match = matchup(e);
  const src = r.source_id || e.source_id || "";
  return {
    id: r.event_key,
    date: e.start_date,
    end_date: e.end_date || e.start_date,
    grade: schoolGrade(e, src),
    level_detail: ["school_team", "club", "youth_league"].includes(e.level_detail) ? e.level_detail : null,
    event_type: e.event_type || "match",
    participants: Array.isArray(e.participants) ? [...new Set(e.participants.filter(x => typeof x === "string"))].slice(0, 24) : [],
    sport: sportName(e),
    title: e.title || e.title_ko || "경기",
    home: match.home || "",
    away: match.away || "",
    time: /^([01]\d|2[0-3]):[0-5]\d$/.test(e.start_time || "") ? e.start_time : e.time_text || "시간 미확인",
    location: e.venue || e.location || "장소 미확인",
    city: e.city || "",
    cost: /등록비|참가비|registration|entry fee/i.test(e.cost_text || "") ? "관중 입장료 미확인 (참가 등록비와 별도)" : e.cost_text || "입장료 미확인",
    price: /등록비|참가비|registration|entry fee/i.test(e.cost_text || "") ? "unknown" : ["free", "paid", "unknown"].includes(e.cost_class) ? e.cost_class : costClass(e.cost_text),
    ...venueTravel(e),
    url: /^https:\/\//.test(String(r.url || "")) ? r.url : "",
    org: e.org || "",
    source: src,
    verification: e.verified === false || e.year_basis === "assumed" ? "추가 확인 필요" : "수집된 공식 정보",
    special: !!e.sports_special_verified
  };
}
export async function sportsEvents(env, url) {
  const now = denverToday();
  const start = url.searchParams.get("month") || now.slice(0, 7);
  if (!MONTH_RE.test(start) || Number(start.slice(0, 4)) < 2026 || Number(start.slice(0, 4)) > 2030) {
    return new Response(JSON.stringify({ ok: false, error: "month must be YYYY-MM (2026–2030)" }), { status: 400, headers: CORS });
  }
  const end = monthShift(start, 2) + "-01";
  const rows = (await env.DB.prepare(
    "SELECT event_key, data, source_id, url, start_date, last_changed FROM events WHERE start_date >= ? AND start_date < ? ORDER BY start_date, event_key LIMIT 3500"
  ).bind(start + "-01", end).all()).results || [];
  const events = [], seen = new Map();
  for (const r of rows) {
    let e;
    try { e = JSON.parse(r.data || "{}"); } catch { continue; }
    if (!isCalendarMatch(e, r.source_id)) continue;
    if (e.registration_status === "cancelled" || e.registration_status === "postponed") continue;
    const rec = rowAsCalendar(e, r);
    const teams = rec.participants.length ? rec.participants : rec.home && rec.away ? [rec.home, rec.away] : [];
    const identity = teams.length ? teams.map(x => x.trim().toLowerCase()).sort().join("~") : rec.title.toLowerCase();
    const key = [rec.date, rec.sport, identity, rec.location.toLowerCase()].join("|");
    if (seen.has(key)) {
      const prior = seen.get(key);
      if (prior.time !== rec.time) prior.verification = "출처 간 경기 시각 불일치 · 공식 일정 재확인 필요";
      continue;
    }
    seen.set(key, rec);
    events.push(rec);
  }
  const counts = key => events.reduce((acc, e) => { acc[e[key]] = (acc[e[key]] || 0) + 1; return acc; }, {});
  const health = (await env.DB.prepare("SELECT id, last_attempt_at, last_ok_at, consecutive_failures FROM source_health").all()).results || [];
  const sportsHealth = health.filter(s => ATHLETIC_SOURCE.test(s.id));
  const sourceHealth = { tracked: sportsHealth.length, failing: sportsHealth.filter(s => s.consecutive_failures > 0).length, last_attempt_at: sportsHealth.map(s => s.last_attempt_at).filter(Boolean).sort().pop() || null, last_ok_at: sportsHealth.map(s => s.last_ok_at).filter(Boolean).sort().pop() || null };
  return new Response(JSON.stringify({ ok: true, month: start, through: monthShift(start, 1), generated_at: new Date().toISOString(), events, source_health: sourceHealth, counts: { grade: counts("grade"), sport: counts("sport"), travel: counts("drive_bucket") }, inspected: rows.length, latest_record_changed: rows.map((x) => x.last_changed).filter(Boolean).sort().pop() || null,
    limit_reached: rows.length >= 3500, coverage: "현재 수집된 공식 일정 중 날짜·학교·종목·경기로 식별되는 항목만 표시됩니다. 전체 초·중·고 경기 수집을 보장하지 않습니다." }), { headers: CORS });
}
export function sportsPage() {
  return new Response(PAGE, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" } });
}
export async function sportsWeekly(env, origin, now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: ZONE, weekday: "long", hour: "2-digit", hour12: false }).formatToParts(now);
  const day = parts.find((x) => x.type === "weekday").value, hour = Number(parts.find((x) => x.type === "hour").value);
  if (day !== "Saturday" || hour < 9) return { checked: true, eligible_now: false };
  const month = denverToday(now).slice(0, 7);
  const page = new URL(origin + "/sports/events?month=" + month);
  const result = await sportsEvents(env, page), { events } = await result.json();
  if (!events || !events.length) return { checked: true, eligible_now: true, sent: 0, reason: "no scheduled verified matches" };
  const all = (await env.DB.prepare("SELECT r.chat_id, p.available_from FROM recipients r LEFT JOIN people p ON p.chat_id = r.chat_id WHERE r.bot = 'family' AND r.status = 'active'").all()).results || [];
  let sent = 0, failed = 0, skipped = 0;
  const stamp = denverToday(now);
  for (const p of all) {
    if (ownerOnly(env) && !isOwner(env, p.chat_id)) { skipped++; continue; }
    if (p.available_from && !events.some(e => e.date >= p.available_from)) { skipped++; continue; }
    if (!(await claimNotice(env, "sports-calendar-weekly:" + stamp + ":" + p.chat_id))) { skipped++; continue; }
    const text = "🏒⚽ 학교·대학 스포츠 정기경기 달력\n\n이번 달·다음 달 경기 " + events.length + "건이 등록돼 있습니다.\n초·중·고·대학, 종목, 무료·유료를 골라 확인하세요.\n\n📅 경기 달력: " + origin + "/sports\n\n일반 정기경기는 개별 알림으로 보내지 않습니다.";
    const response = await tg(env, "family", "sendMessage", { chat_id: p.chat_id, text, disable_web_page_preview: true });
    if (response.ok) sent++; else failed++;
  }
  return { checked: true, eligible_now: true, sent, failed, skipped, events: events.length };
}
