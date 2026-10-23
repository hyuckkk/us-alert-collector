/**
 * us-alert-worker — 미국 체류 알림 시스템의 발송·버튼·DB 담당 (Cloudflare Worker + D1 us-alert-db).
 *
 * 한국 시스템(telegram-relay Worker, alert-collector, telegram-relay 저장소)과는 코드 구조만 참고했고
 * 봇·비밀값·저장소·DB 를 하나도 공유하지 않는다.
 *
 * 비밀값 (GitHub 비밀값에서 deploy-worker 워크플로가 Worker 비밀값으로 복사한다):
 *   BOT_TOKEN_LIFE / BOT_TOKEN_FAMILY / BOT_TOKEN_KIDS, OWNER_CHAT_ID,
 *   ADMIN_KEY (Cloudflare 토큰에서 자동 파생. 웹훅 비밀값도 여기서 파생)
 * 일반 변수(wrangler.toml [vars]): TG_API, SYSTEM_BOT, OWNER_TZ, FAMILY_AVAILABLE_FROM, DELIVERY_MODE
 *
 * 경로 (관리 경로는 Authorization: Bearer ADMIN_KEY)
 *   POST /hook/<bot>            텔레그램 웹훅
 *   POST /admin/setup           웹훅·명령 메뉴 등록
 *   POST /admin/test-send       소유자 DM 테스트 발송
 *   GET  /admin/clicks|status   점검
 *   POST /admin/expiry|key-check|run-cron   만료일·키 점검·매일 점검
 *   POST /admin/seen            {op: check|mark, items:[{key,hash,source_id}]}  수집기의 '이미 본 항목'
 *   POST /admin/enqueue         {events:[...]}  판정 결과 → 사람별 대기열
 *   POST /admin/dispatch        즉시·예고 알림 발송(정지 시간대는 미룸)
 *   POST /admin/digest          주간 요약(각자 토요일 9시 이후)
 *   POST /admin/run-log         실행 기록(측정 항목)
 *   GET|POST /admin/workload    경로별 작업량·비용(조용히 누적)
 *   POST /admin/inbox           외부 입력 파일 선점·완료 기록(재실행 중복 방지)
 *   POST /admin/catchup         아직 못 받은 유효한 적극 알림 보충 발송(dry: 미리 보기)
 *   GET  /admin/compare         3경로(GitHub 수집기·Claude·ChatGPT) 발견 대조
 *   GET  /admin/export          누적 기록 내보내기(JSON)
 *   GET|POST /admin/usage       월별 AI 사용량
 *   POST /admin/candidates      정보원 후보(op 없음: 추가, unverified: 검증할 후보, verify: 검증 결과 → 소유자 질문)
 *   GET  /admin/sources-extra   승격된 정보원
 *   POST /admin/queue           수집기 처리 대기열 {op: check|add|take|result|stats}
 *   GET  /admin/event-index     같은 행사 판별용 최근 행사 목록
 *   POST /admin/watch           상세 페이지 재검사 {op: due|update|stats}
 *   GET  /admin/ai-health       AI 중단 여부 · POST /admin/unverified  AI 장기 중단 시 검증 전 정보(소유자만)
 *   GET  /admin/watchdog        수집 실행 감시 · GET /admin/source-health  정보원별 상태
 *   POST /admin/report          주간 보고를 소유자 DM 으로
 *   GET|POST /admin/baseline    운영 첫날 기준선 {hours}: 그동안 새 행사는 조용히 등록, 곧 행동할 것만 요약
 *   GET  /health
 * 1차 검증 전에는 DELIVERY_MODE=owner-only 로 소유자 외에는 아무에게도 보내지 않는다.
 */

import { BUILD, BOTS, botToken, isOwner, nowIso, json, tg, kvGet, kvSet, claimNotice, notifyOwner, localStamp, localDate, daysBetween, ensurePerson, getPerson, validTz, ownerOnly } from "./common.js";
import { keyboard, rebuildKeyboard } from "./render.js";
import * as pipeline from "./pipeline.js";
import { sportsEvents, sportsPage, sportsWeekly } from "./sports.js";
import { Heartbeat, ensureHeartbeat, beatNow } from "./heartbeat.js";
import { normCity, CITIES } from "./geo.js";

export { Heartbeat };

// 만료 알림 시점: 남은 일수가 이 값 이하로 처음 내려갈 때 1회씩 (하루 실행을 놓쳐도 다음 날 보냄)
const EXPIRY_STEPS = [14, 7, 3, 1, 0];
const ACTIONS = {
  ack: { state: "ACKNOWLEDGED", tip: "✅ 확인 처리했습니다." },
  mute: { state: "MUTED", tip: "🚫 관심없음 처리했습니다." },
  tag: { tip: "🔕 이 태그를 더 보내지 않습니다." },
  unack: { state: null, tip: "↩️ 확인을 취소했습니다." },
  unmute: { state: null, tip: "↩️ 관심없음을 취소했습니다." },
  untag: { tip: "↩️ 태그 차단을 풀었습니다." },
};

async function hookSecret(env) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`us-hook:${env.ADMIN_KEY}`));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let x = 0;
  for (let i = 0; i < a.length; i++) x |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return x === 0;
}
function isAdmin(env, request) {
  const h = request.headers.get("authorization") || "";
  return !!env.ADMIN_KEY && timingSafeEqual(h, `Bearer ${env.ADMIN_KEY}`);
}

async function itemStateFor(env, chatId, itemKey) {
  const s = await env.DB.prepare("SELECT state FROM item_state WHERE chat_id = ? AND item_key = ?").bind(String(chatId), itemKey).first();
  const item = await env.DB.prepare("SELECT tag FROM items WHERE item_key = ?").bind(itemKey).first();
  let tagMuted = false;
  if (item && item.tag) {
    tagMuted = !!(await env.DB.prepare("SELECT 1 AS x FROM muted_tags WHERE chat_id = ? AND tag = ?").bind(String(chatId), item.tag).first());
  }
  return { state: s ? s.state : null, tag: item ? item.tag : null, tagMuted };
}

// ---------------------------------------------------------------- 테스트 발송
async function testSend(env, bots) {
  if (!env.OWNER_CHAT_ID) return { ok: false, error: "OWNER_CHAT_ID not set" };
  const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 13);
  const out = [];
  for (const bot of bots) {
    const itemKey = `test:${bot}:${stamp}`;
    const text = [
      `🧪 [0단계 테스트] ${BOTS[bot].label} 봇`,
      "제목: 테스트 행사 (실제 행사 아님)",
      "아래 버튼을 눌러 보세요. 누를 때마다 D1(us-alert-db)에 기록되고, 기록 번호가 화면 위에 잠깐 표시됩니다.",
      "같은 버튼을 다시 누르면 취소됩니다.",
      `발송: ${localStamp(env)}`,
    ].join("\n");
    await env.DB.prepare("INSERT INTO items (item_key, bot, title, tag, url, created_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(item_key) DO NOTHING")
      .bind(itemKey, bot, "테스트 행사", "test", null, nowIso()).run();
    const st = await itemStateFor(env, env.OWNER_CHAT_ID, itemKey);
    const d = await tg(env, bot, "sendMessage", { chat_id: env.OWNER_CHAT_ID, text, disable_web_page_preview: true, reply_markup: keyboard(itemKey, st) });
    const msgId = d.ok && d.result ? d.result.message_id : null;
    await env.DB.prepare("INSERT INTO deliveries (bot, chat_id, item_key, message_id, ok, error, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
      .bind(bot, String(env.OWNER_CHAT_ID), itemKey, msgId, d.ok ? 1 : 0, d.ok ? null : String(d.description || "").slice(0, 300), nowIso()).run();
    out.push({ bot, ok: !!d.ok, message_id: msgId, error: d.ok ? undefined : d.description });
  }
  return { ok: out.every((r) => r.ok), results: out };
}

// ---------------------------------------------------------------- 텔레그램 웹훅
async function isActiveRecipient(env, bot, id) {
  if (isOwner(env, id)) return true;
  const r = await env.DB.prepare("SELECT status FROM recipients WHERE chat_id = ? AND bot = ?").bind(String(id), bot).first();
  return !!r && r.status === "active";
}

async function handleCandidateCallback(env, bot, cq, action, id) {
  const from = String((cq.from && cq.from.id) || "");
  if (!isOwner(env, from)) { await tg(env, bot, "answerCallbackQuery", { callback_query_id: cq.id, text: "권한이 없습니다." }); return; }
  const c = await pipeline.decideCandidate(env, id, action === "cpro");
  await tg(env, bot, "answerCallbackQuery", { callback_query_id: cq.id, text: !c ? "후보를 찾지 못했습니다." : action === "cpro" ? "감시 목록에 추가했습니다. 다음 수집부터 확인합니다." : "무시했습니다." });
  const msg = cq.message || {};
  if (c && msg.message_id) {
    await tg(env, bot, "editMessageReplyMarkup", { chat_id: msg.chat.id, message_id: msg.message_id, reply_markup: { inline_keyboard: [[{ text: action === "cpro" ? "➕ 추가됨" : "무시함", callback_data: "noop:x" }]] } });
  }
}

async function handleCallback(env, bot, cq, updateId) {
  const from = String((cq.from && cq.from.id) || "");
  const msg = cq.message || {};
  const chat = msg.chat ? String(msg.chat.id) : from;
  const data = cq.data || "";
  const sep = data.indexOf(":");
  const action = sep > 0 ? data.slice(0, sep) : "";
  const itemKey = sep > 0 ? data.slice(sep + 1) : "";
  if (action === "cpro" || action === "cign") return handleCandidateCallback(env, bot, cq, action, itemKey);
  if (!(await isActiveRecipient(env, bot, from))) {
    await tg(env, bot, "answerCallbackQuery", { callback_query_id: cq.id, text: "권한이 없습니다." });
    return;
  }
  if (!ACTIONS[action] || !itemKey) {
    await tg(env, bot, "answerCallbackQuery", { callback_query_id: cq.id });
    return;
  }
  const at = nowIso();
  const ins = await env.DB.prepare(
    "INSERT INTO clicks (bot, update_id, chat_id, from_id, message_id, item_key, action, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(bot, update_id) DO NOTHING RETURNING id",
  ).bind(bot, updateId, chat, from, msg.message_id || null, itemKey, action, at).first();
  if (!ins) { // 텔레그램 재전송: 이미 처리함
    await tg(env, bot, "answerCallbackQuery", { callback_query_id: cq.id });
    return;
  }
  const stmts = [];
  if (action === "ack" || action === "mute") {
    stmts.push(env.DB.prepare("INSERT INTO item_state (chat_id, item_key, state, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(chat_id, item_key) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at")
      .bind(from, itemKey, ACTIONS[action].state, at));
  } else if (action === "unack" || action === "unmute") {
    stmts.push(env.DB.prepare("DELETE FROM item_state WHERE chat_id = ? AND item_key = ?").bind(from, itemKey));
  } else {
    const item = await env.DB.prepare("SELECT tag FROM items WHERE item_key = ?").bind(itemKey).first();
    if (item && item.tag) {
      stmts.push(action === "tag"
        ? env.DB.prepare("INSERT INTO muted_tags (chat_id, tag, at) VALUES (?, ?, ?) ON CONFLICT(chat_id, tag) DO NOTHING").bind(from, item.tag, at)
        : env.DB.prepare("DELETE FROM muted_tags WHERE chat_id = ? AND tag = ?").bind(from, item.tag));
    }
  }
  if (stmts.length) await env.DB.batch(stmts);
  await tg(env, bot, "answerCallbackQuery", { callback_query_id: cq.id, text: `${ACTIONS[action].tip} (D1 기록 #${ins.id})` });
  if (msg.message_id) {
    const st = await itemStateFor(env, from, itemKey);
    await tg(env, bot, "editMessageReplyMarkup", { chat_id: chat, message_id: msg.message_id, reply_markup: rebuildKeyboard(msg.reply_markup, itemKey, st) });
  }
}

const HELP_ALL = [
  "내 설정 명령 (나에게만 적용)",
  "/settings — 내 설정 보기",
  "/quiet 22 7 — 알림 정지 시간(현지 22시~7시)",
  "/tz America/Denver — 내 시간대",
  "/city Westminster 40 — 이 도시에서 40마일(약 65km) 넘는 일반 행사는 받지 않기. 국립공원 허가·추첨·예약·연례 행사는 거리와 상관없이 받음 (/city off 로 해제, 주소는 넣지 마세요)",
  "/from 2027-07-01 — 이 날짜 이후 행사만 받기 (/from off 로 해제)",
  "/digest daily|weekly|off — 요약 방식 · /digest 8 — 요약 받을 시각(현지)",
  "/myid — 내 텔레그램 ID",
  "/sports — 초·중·고·대학 정기경기 달력 링크",
  "/more — 알림으로 보내지 않고 보관한 행사 5건 더 보기",
  "/interests — 기본 알림에서 빠지는 분야(영유아·시니어·회의 등)를 골라 받기",
  "/stop — 이 봇 알림 그만 받기 (다시 받으려면 /start)",
].join("\n");
const HELP_OWNER = [
  "소유자 명령",
  "/test — 이 봇으로 테스트 알림 1건",
  "/clicks — 최근 버튼 기록 10건 (D1)",
  "/status — 요약 상태",
  "/report — 주간 보고 지금 받기",
  "/candidates — 정보원 후보",
  "/expiry — 만료일 목록 · /expiry anthropic 2027-04-20 — 등록 · /expiry del anthropic — 삭제",
  "/pending — 승인 대기 · /approve <ID> — 이 봇 수신 승인",
  "",
  HELP_ALL,
].join("\n");

// 기본 알림에서 빠지는 분야를 사람마다 골라 받을 수 있다(추천 판정의 category)
const INTERESTS = { toddler: "영유아 프로그램", senior: "시니어 프로그램", "library-regular": "도서관 정기 모임", "support-group": "상담·지원 모임",
  civic: "시의회·위원회 회의", "public-service": "주민 서비스·민원 행사", other: "기타" };
async function interestsCommand(env, fromId, args) {
  await ensurePerson(env, fromId);
  const p = await env.DB.prepare("SELECT interests FROM people WHERE chat_id = ?").bind(String(fromId)).first();
  let cur = pipeline.parseInterests(p && p.interests);
  const [op, ...names] = args;
  if (op === "add" || op === "del") {
    const bad = names.filter((n) => !INTERESTS[n]);
    if (!names.length || bad.length) return `분야 이름을 확인해 주세요: ${bad.join(", ") || "(없음)"}\n${Object.entries(INTERESTS).map(([k, v]) => `${k} — ${v}`).join("\n")}`;
    cur = op === "add" ? [...new Set([...cur, ...names])] : cur.filter((x) => !names.includes(x));
    await env.DB.prepare("UPDATE people SET interests = ?, updated_at = ? WHERE chat_id = ?").bind(JSON.stringify(cur), nowIso(), String(fromId)).run();
  }
  return [
    "기본 알림은 많은 사람이 참여할 가치가 있는 행사만 보냅니다. 아래 분야는 직접 고른 경우에만 받습니다.",
    ...Object.entries(INTERESTS).map(([k, v]) => `${cur.includes(k) ? "✅" : "▫️"} ${k} — ${v}`),
    "",
    "받기: /interests add toddler · 그만: /interests del toddler",
  ].join("\n");
}

async function personCommand(env, fromId, cmd, args) {
  await ensurePerson(env, fromId);
  const p = await getPerson(env, fromId);
  const set = async (col, val) => env.DB.prepare(`UPDATE people SET ${col} = ?, updated_at = ? WHERE chat_id = ?`).bind(val, nowIso(), String(fromId)).run();
  if (cmd === "/quiet") {
    const [a, b] = args.map((x) => parseInt(x, 10));
    if (!(a >= 0 && a <= 23 && b >= 0 && b <= 23)) return "형식: /quiet 22 7 (정지 시작 시, 끝 시. 같은 값이면 정지 없음)";
    await env.DB.prepare("UPDATE people SET quiet_start = ?, quiet_end = ?, updated_at = ? WHERE chat_id = ?").bind(a, b, nowIso(), String(fromId)).run();
    return `알림 정지: ${a}시~${b}시 (${p.tz})`;
  }
  if (cmd === "/tz") {
    if (!args[0] || !validTz(args[0])) return "형식: /tz America/Denver 또는 /tz Asia/Seoul";
    await set("tz", args[0]);
    return `시간대: ${args[0]}`;
  }
  if (cmd === "/city") {
    if ((args[0] || "").toLowerCase() === "off") {
      await env.DB.prepare("UPDATE people SET city = NULL, radius_mi = NULL, updated_at = ? WHERE chat_id = ?").bind(nowIso(), String(fromId)).run();
      return "기준 도시 해제: 거리와 상관없이 모든 행사를 받습니다.";
    }
    const last = args[args.length - 1];
    const radius = /^\d{1,3}$/.test(last || "") ? parseInt(last, 10) : 40;
    const name = (/^\d{1,3}$/.test(last || "") ? args.slice(0, -1) : args).join(" ").slice(0, 40);
    if (!name || /\d{3,}/.test(name)) return "도시 이름만 넣어 주세요(번지·우편번호 없이). 예) /city Westminster 40";
    const c = normCity(name);
    if (!c) {
      return `'${name}' 은(는) 목록에 없는 도시라 거리 판정을 할 수 없습니다. 가까운 도시로 넣어 주세요.\n예) ${Object.keys(CITIES).slice(0, 24).map((k) => k.replace(/\b\w/g, (x) => x.toUpperCase())).join(", ")} …`;
    }
    if (radius < 5 || radius > 400) return "거리는 5~400마일 사이로 넣어 주세요. 예) /city Westminster 40";
    await env.DB.prepare("UPDATE people SET city = ?, radius_mi = ?, updated_at = ? WHERE chat_id = ?").bind(c, radius, nowIso(), String(fromId)).run();
    return `기준 도시: ${c.replace(/\b\w/g, (x) => x.toUpperCase())}, ${radius}마일(약 ${Math.round(radius * 1.609)}km) 안의 일반 행사만 받습니다.\n국립공원 허가·추첨·예약·연례 행사는 거리와 상관없이 보냅니다. 행사 위치를 알 수 없으면 거르지 않고 보냅니다.`;
  }
  if (cmd === "/digest") {
    const a = (args[0] || "").toLowerCase();
    if (["daily", "weekly", "off"].includes(a)) { await set("digest_mode", a); return `요약 방식: ${a === "daily" ? "매일" : a === "weekly" ? "매주 토요일" : "받지 않음"}`; }
    const h = parseInt(a, 10);
    if (h >= 0 && h <= 23) { await set("digest_hour", h); return `요약 시각: 현지 ${h}시 이후`; }
    return "형식: /digest daily | weekly | off, 또는 /digest 8 (시각)";
  }
  if (cmd === "/from") {
    if (args[0] === "off") { await set("available_from", null); return "참가 가능 시작일 해제: 지금 열리는 행사부터 받습니다."; }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(args[0] || "")) return "형식: /from 2027-07-01 (해제: /from off)";
    await set("available_from", args[0]);
    return `참가 가능 시작일: ${args[0]} (그 이전 행사는 연례행사 예고로만 받습니다)`;
  }
  return [
    "내 설정",
    `시간대 ${p.tz} · 알림 정지 ${p.quiet_start}시~${p.quiet_end}시`,
    `참가 가능 시작일 ${p.available_from || "지금"} · 기준 도시 ${p.city ? `${p.city} ${p.radius_mi || "?"}마일 안` : "미설정(거리로 거르지 않음)"}`,
    `요약 ${p.digest_mode === "weekly" ? "매주 토요일" : p.digest_mode === "off" ? "받지 않음" : "매일"} ${p.digest_hour ?? 8}시 이후`,
  ].join("\n");
}

async function handleMessage(env, bot, m) {
  const from = m.from || {};
  const fromId = String(from.id || "");
  const chat = String((m.chat && m.chat.id) || fromId);
  const text = (m.text || "").trim();
  if (!text || (m.chat && m.chat.type !== "private")) return;
  const reply = (t) => tg(env, bot, "sendMessage", { chat_id: chat, text: t, disable_web_page_preview: true });
  const [cmdRaw, ...args] = text.split(/\s+/);
  const cmd = cmdRaw.split("@")[0].toLowerCase();

  if (cmd === "/start" || cmd === "/myid") {
    const owner = isOwner(env, fromId);
    const at = nowIso();
    const prev = await env.DB.prepare("SELECT status FROM recipients WHERE chat_id = ? AND bot = ?").bind(fromId, bot).first();
    if (cmd === "/start" && prev && prev.status === "stopped") {
      // 본인이 /stop 했던 승인된 지인: 다시 승인받지 않고 바로 재개, 쌓인 행사 대신 2주 안 접수 정보만 한 번
      await env.DB.prepare("UPDATE recipients SET status = 'active', updated_at = ? WHERE chat_id = ? AND bot = ?").bind(at, fromId, bot).run();
      const w = await pipeline.welcomeQueue(env, fromId, bot);
      await reply(`${BOTS[bot].label} 알림을 다시 받습니다.${w.queued ? ` 2주 안에 접수가 열리거나 마감되는 ${w.queued}건을 다음 요약으로 보내 드립니다.` : ""}`);
      await notifyOwner(env, `[${BOTS[bot].label}] 수신 재개: ${[from.first_name, from.last_name].filter(Boolean).join(" ")} (ID ${fromId})`);
      return;
    }
    if (cmd === "/start") {
      await env.DB.prepare(
        "INSERT INTO recipients (chat_id, bot, name, username, status, joined_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) " +
        "ON CONFLICT(chat_id, bot) DO UPDATE SET name = excluded.name, username = excluded.username, updated_at = excluded.updated_at, " +
        "status = CASE WHEN recipients.status = 'blocked' THEN 'blocked' WHEN excluded.status = 'active' THEN 'active' ELSE recipients.status END",
      ).bind(fromId, bot, [from.first_name, from.last_name].filter(Boolean).join(" ") || null, from.username || null, owner ? "active" : "pending", at, at).run();
      await ensurePerson(env, fromId);
    }
    if (owner) { await reply(`${BOTS[bot].label} 봇 소유자로 확인되었습니다.\n\n${HELP_OWNER}`); return; }
    if (!env.OWNER_CHAT_ID) {
      await reply(`내 텔레그램 ID: ${fromId}\n\n아직 소유자가 설정되지 않았습니다. 이 숫자를 GitHub 비밀값 OWNER_CHAT_ID 로 넣고 deploy-worker 를 다시 실행하면 이 계정이 소유자가 됩니다.`);
      return;
    }
    await reply(`${BOTS[bot].label} 알림 신청이 접수되었습니다. 소유자 승인 후 알림이 옵니다.\n내 텔레그램 ID: ${fromId}\n\n` +
      "승인된 뒤 /city Westminster 40 처럼 기준 도시와 거리(마일)를 보내면 먼 곳의 일반 행사는 빼고 받습니다(/city off 로 해제). " +
      "이 설정은 받는 범위만 줄이며 검색 지역을 넓히지는 않습니다.");
    if (cmd === "/start" && (await claimNotice(env, `join:${bot}:${fromId}`))) {
      await notifyOwner(env, `[${BOTS[bot].label}] 새 수신 신청: ${[from.first_name, from.last_name].filter(Boolean).join(" ")} ${from.username ? "@" + from.username : ""}\nID ${fromId}\n승인: ${BOTS[bot].handle} 에서 /approve ${fromId}${ownerOnly(env) ? "\n(현재 소유자 전용 모드라 승인해도 1차 검증 전에는 발송되지 않습니다)" : ""}`);
    }
    return;
  }
  if (cmd === "/stop") {
    const r = await env.DB.prepare("UPDATE recipients SET status = 'stopped', updated_at = ? WHERE chat_id = ? AND bot = ? AND status IN ('active', 'pending')").bind(nowIso(), fromId, bot).run();
    if (r.meta.changes) {
      await env.DB.prepare("UPDATE outbox SET status = 'skipped', reason = 'stopped' WHERE chat_id = ? AND bot = ? AND status IN ('pending', 'failed', 'uncertain')").bind(fromId, bot).run();
      await reply(`${BOTS[bot].label} 알림을 더 보내지 않습니다. 다시 받으려면 /start 를 보내 주세요.`);
      if (!isOwner(env, fromId)) await notifyOwner(env, `[${BOTS[bot].label}] 수신 중단: ${[from.first_name, from.last_name].filter(Boolean).join(" ")} (ID ${fromId})`);
    } else await reply("이 봇 알림을 받고 있지 않습니다. 받으려면 /start 를 보내 주세요.");
    return;
  }
  const active = await isActiveRecipient(env, bot, fromId);
  if (!active) return;
  if (isOwner(env, fromId)) await pipeline.watchdog(env); // GitHub Actions 가 통째로 멈춰도 소유자가 봇을 쓰면 감지
  if (["/settings", "/quiet", "/tz", "/city", "/from", "/digest"].includes(cmd)) { await reply(await personCommand(env, fromId, cmd, args)); return; }
  if (cmd === "/sports") {
    const origin = await kvGet(env, "public_worker_origin");
    await reply(origin ? `🏟️ 학교·대학 스포츠 경기 달력\n\n이번 달·다음 달 / 학교급·종목·요금별 검색\n${origin}/sports` : "달력 사이트 주소 등록을 확인하는 중입니다.");
    return;
  }
  if (cmd === "/interests") { await reply(await interestsCommand(env, fromId, args)); return; }
  if (cmd === "/more") {
    if (ownerOnly(env) && !isOwner(env, fromId)) { await reply("지금은 소유자 시험 기간이라 보내 드릴 수 없습니다."); return; }
    const r = await pipeline.more(env, fromId, bot, 5);
    if (!r.sent) await reply("지금 더 보여 드릴 새 행사가 없습니다.");
    return;
  }
  if (!isOwner(env, fromId)) {
    await reply("알림의 [✅ 확인] [🚫 관심없음] [🔕 이 태그 그만] 버튼은 본인에게만 적용됩니다. 다시 누르면 취소됩니다.\n\n" + HELP_ALL);
    return;
  }

  if (cmd === "/help") { await reply(HELP_OWNER); return; }
  if (cmd === "/test") {
    const r = await testSend(env, [bot]);
    if (!r.ok) await reply(`테스트 발송 실패: ${JSON.stringify(r.results || r.error)}`);
    return;
  }
  if (cmd === "/clicks") {
    const rows = (await env.DB.prepare("SELECT id, bot, action, item_key, at FROM clicks ORDER BY id DESC LIMIT 10").all()).results;
    await reply(rows.length ? "최근 버튼 기록 (D1 clicks)\n" + rows.map((r) => `#${r.id} ${r.at.slice(0, 19).replace("T", " ")}Z ${r.bot} ${r.action} ${r.item_key}`).join("\n") : "아직 버튼 기록이 없습니다.");
    return;
  }
  if (cmd === "/status") { await reply(formatStatus(await status(env))); return; }
  if (cmd === "/report") { await pipeline.weeklyReport(env, true); return; }
  if (cmd === "/candidates") {
    const rows = (await env.DB.prepare("SELECT name, url, hits, status FROM candidates ORDER BY last_seen DESC LIMIT 10").all()).results;
    await reply(rows.length ? "정보원 후보 (최근 10)\n" + rows.map((r) => `${r.status} ${r.hits}회 ${r.name || ""}\n  ${r.url}`).join("\n") : "아직 후보가 없습니다.");
    return;
  }
  if (cmd === "/expiry") {
    if (args[0] === "del" && args[1]) {
      await env.DB.prepare("DELETE FROM key_expiry WHERE name = ?").bind(args[1]).run();
      await reply(`만료일 삭제: ${args[1]}`);
      return;
    }
    if (args.length >= 2) {
      const r = await setExpiry(env, args[0], args[1], args.slice(2).join(" ") || null);
      await reply(r.ok ? `만료일 등록: ${r.name} ${r.expires_on} (오늘 기준 ${r.days_left}일 남음)\n만료 ${EXPIRY_STEPS.join("·")}일 전에 이 봇 DM 으로 알립니다.` : `형식 오류: ${r.error}\n예) /expiry anthropic 2027-04-20`);
      return;
    }
    await reply(await formatExpiries(env));
    return;
  }
  if (cmd === "/pending") {
    const rows = (await env.DB.prepare("SELECT chat_id, bot, name, username, joined_at FROM recipients WHERE status = 'pending' ORDER BY joined_at").all()).results;
    await reply(rows.length ? "승인 대기\n" + rows.map((r) => `${r.bot} ${r.chat_id} ${r.name || ""} ${r.username ? "@" + r.username : ""}`).join("\n") : "승인 대기 없음");
    return;
  }
  if (cmd === "/approve" && args[0]) {
    const r = await env.DB.prepare("UPDATE recipients SET status = 'active', updated_at = ? WHERE chat_id = ? AND bot = ? AND status <> 'active'").bind(nowIso(), args[0], bot).run();
    if (!r.meta.changes) { await reply(`이 봇에 /start 한 기록이 없거나 이미 승인된 사람입니다: ${args[0]}`); return; }
    const w = await pipeline.welcomeQueue(env, args[0], bot);
    await reply(`승인: ${args[0]} (${BOTS[bot].label}) · 첫 요약 ${w.queued}건(2주 안 접수 시작·마감만)${ownerOnly(env) ? "\n현재 소유자 전용 모드라 지인 발송을 켜기 전까지는 발송되지 않습니다." : ""}`);
    if (!ownerOnly(env)) await tg(env, bot, "sendMessage", { chat_id: args[0], text: `${BOTS[bot].label} 알림 신청이 승인되었습니다.\n\n${HELP_ALL}`, disable_web_page_preview: true });
    return;
  }
  await reply(HELP_OWNER);
}

// ---------------------------------------------------------------- 만료일
async function setExpiry(env, name, ymd, note) {
  if (!/^[A-Za-z0-9_.-]{1,40}$/.test(name || "")) return { ok: false, error: "이름은 영문·숫자·_.- 40자 이내" };
  if (!/^\d{4}-\d{2}-\d{2}$/.test(ymd || "") || Number.isNaN(Date.parse(ymd + "T00:00:00Z"))) return { ok: false, error: "날짜는 YYYY-MM-DD" };
  await env.DB.prepare("INSERT INTO key_expiry (name, expires_on, note, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET expires_on = excluded.expires_on, note = excluded.note, updated_at = excluded.updated_at")
    .bind(name, ymd, note, nowIso()).run();
  return { ok: true, name, expires_on: ymd, days_left: daysBetween(localDate(env), ymd) };
}

async function formatExpiries(env) {
  const rows = (await env.DB.prepare("SELECT name, expires_on, note FROM key_expiry ORDER BY expires_on").all()).results;
  if (!rows.length) return "등록된 만료일이 없습니다.\n예) /expiry anthropic 2027-04-20";
  const today = localDate(env);
  return "만료일 목록\n" + rows.map((r) => `${r.name}: ${r.expires_on} (D-${daysBetween(today, r.expires_on)})${r.note ? " " + r.note : ""}`).join("\n");
}

async function checkExpiries(env) {
  const rows = (await env.DB.prepare("SELECT name, expires_on, note FROM key_expiry").all()).results;
  const today = localDate(env);
  const sent = [];
  for (const r of rows) {
    const left = daysBetween(today, r.expires_on);
    let key;
    if (left < 0) key = `expiry:${r.name}:${r.expires_on}:expired:${today}`; // 만료 후에는 매일
    else {
      const step = [...EXPIRY_STEPS].reverse().find((s) => left <= s); // 남은 일수가 내려간 가장 작은 단계
      if (step === undefined) continue;
      key = `expiry:${r.name}:${r.expires_on}:d${step}`;
    }
    if (!(await claimNotice(env, key))) continue;
    const head = left < 0 ? `⛔ ${r.name} 이(가) ${-left}일 전에 만료되었습니다 (${r.expires_on}).`
      : left === 0 ? `⛔ ${r.name} 이(가) 오늘 만료됩니다 (${r.expires_on}).`
        : `⏰ ${r.name} 만료 ${left}일 전입니다 (${r.expires_on}).`;
    const body = [head];
    if (r.note) body.push(r.note);
    body.push("새 키를 발급해 GitHub 비밀값(docs/SETUP-0.md)에 바꿔 넣은 뒤, 이 봇에 /expiry " + r.name + " <새 만료일> 로 날짜를 갱신해 주세요.");
    const d = await notifyOwner(env, body.join("\n"));
    sent.push({ name: r.name, left, ok: !!d.ok });
  }
  return sent;
}

// ---------------------------------------------------------------- API 키 점검 결과 (GitHub Actions 에서 보고)
async function keyCheckReport(env, body) {
  const name = String(body.name || "anthropic").slice(0, 40);
  const ok = !!body.ok;
  const rec = { name, ok, status: String(body.status || ""), detail: String(body.detail || "").slice(0, 300), at: nowIso() };
  await kvSet(env, `keycheck:${name}`, rec);
  let alerted = false;
  if (!ok && ["invalid", "missing", "permission"].includes(rec.status)) {
    if (await claimNotice(env, `keycheck:${name}:${rec.status}:${localDate(env)}`)) {
      const why = { invalid: "키가 거부되었습니다(만료·폐기 가능성)", missing: "GitHub 비밀값이 비어 있습니다", permission: "키 권한이 부족합니다" }[rec.status];
      await notifyOwner(env, `⛔ ${name} API 키 점검 실패: ${why}.\n${rec.detail}\n교체 방법: GitHub 비밀값 ANTHROPIC_API_KEY 를 새 키로 바꾸기 (docs/SETUP-0.md)`);
      alerted = true;
    }
  }
  return { ok: true, recorded: rec, alerted };
}

// ---------------------------------------------------------------- 웹훅 등록
async function setupWebhooks(env, origin) {
  if (!env.ADMIN_KEY) return { ok: false, error: "ADMIN_KEY not set" };
  const secret = await hookSecret(env);
  await kvSet(env, "public_worker_origin", origin); // /sports bot command uses the live webhook origin, never a guessed subdomain
  const out = {};
  for (const bot of Object.keys(BOTS)) {
    if (!botToken(env, bot)) { out[bot] = { ok: false, error: `${BOTS[bot].secret} not set` }; continue; }
    const url = `${origin}/hook/${bot}`;
    const w = await tg(env, bot, "setWebhook", { url, secret_token: secret, allowed_updates: ["message", "callback_query"], drop_pending_updates: false });
    const c = await tg(env, bot, "setMyCommands", {
      commands: [
        { command: "start", description: "알림 받기 시작" },
        { command: "settings", description: "내 설정 보기" },
        { command: "sports", description: "학교·대학 스포츠 달력" },
        { command: "more", description: "보관한 행사 더 보기" },
        { command: "interests", description: "관심 분야 고르기" },
        { command: "help", description: "도움말" },
        { command: "stop", description: "이 봇 알림 그만 받기" },
      ],
    });
    const me = await tg(env, bot, "getMe");
    out[bot] = { ok: !!w.ok, webhook: w.ok ? url : w.description, commands: !!c.ok, username: me.ok ? "@" + me.result.username : me.description };
  }
  await kvSet(env, "origin", origin);
  return { ok: Object.values(out).every((x) => x.ok), bots: out };
}

async function ensureWebhooks(env) {
  const origin = await kvGet(env, "origin");
  if (!origin || !env.ADMIN_KEY) return { skipped: true };
  const want = (bot) => `${origin}/hook/${bot}`;
  const out = {};
  for (const bot of Object.keys(BOTS)) {
    if (!botToken(env, bot)) continue;
    const info = await tg(env, bot, "getWebhookInfo");
    const cur = info.ok && info.result ? info.result.url : "";
    if (cur === want(bot)) { out[bot] = "ok"; continue; }
    const w = await tg(env, bot, "setWebhook", { url: want(bot), secret_token: await hookSecret(env), allowed_updates: ["message", "callback_query"] });
    out[bot] = w.ok ? "reset" : w.description;
  }
  return out;
}

// ---------------------------------------------------------------- 상태
async function status(env) {
  const one = async (sql) => (await env.DB.prepare(sql).first()) || {};
  const recips = (await env.DB.prepare("SELECT bot, status, COUNT(*) AS n FROM recipients GROUP BY bot, status").all()).results;
  const kc = await kvGet(env, "keycheck:anthropic");
  return {
    build: BUILD,
    delivery_mode: ownerOnly(env) ? "owner-only" : "all",
    secrets: Object.fromEntries([...Object.values(BOTS).map((b) => b.secret), "OWNER_CHAT_ID", "ADMIN_KEY"].map((k) => [k, !!env[k]])),
    origin_set: !!(await kvGet(env, "origin")),
    recipients: recips,
    deliveries: await one("SELECT COUNT(*) AS n, SUM(ok) AS ok FROM deliveries"),
    clicks: await one("SELECT COUNT(*) AS n, MAX(at) AS last FROM clicks"),
    events: await one("SELECT COUNT(*) AS n, MAX(last_changed) AS last FROM events"),
    outbox: (await env.DB.prepare("SELECT kind, status, COUNT(*) AS n FROM outbox GROUP BY kind, status").all()).results,
    expiries: (await env.DB.prepare("SELECT name, expires_on FROM key_expiry ORDER BY expires_on").all()).results,
    key_check: kc ? JSON.parse(kc) : null,
    last_run: await one("SELECT run_id, started_at, finished_at FROM runs ORDER BY started_at DESC LIMIT 1"),
    queue: await pipeline.queueStats(env),
    ai: (await pipeline.aiHealth(env)),
    watchdog: await pipeline.watchdog(env),
    last_cron: await kvGet(env, "last_cron"),
    last_heartbeat: await kvGet(env, "last_heartbeat"),
  };
}
function formatStatus(s) {
  const lines = [`상태 (${s.build}, 발송 모드 ${s.delivery_mode})`];
  lines.push("비밀값: " + Object.entries(s.secrets).map(([k, v]) => `${k} ${v ? "○" : "✕"}`).join(", "));
  lines.push(`발송 ${s.deliveries.n || 0}건 (성공 ${s.deliveries.ok || 0}), 버튼 기록 ${s.clicks.n || 0}건, 행사 ${s.events.n || 0}건`);
  lines.push("대기열: " + (s.outbox.map((o) => `${o.kind}/${o.status} ${o.n}`).join(", ") || "없음"));
  lines.push("수신자: " + (s.recipients.map((r) => `${r.bot}/${r.status} ${r.n}`).join(", ") || "없음"));
  lines.push("만료일: " + (s.expiries.map((e) => `${e.name} ${e.expires_on}`).join(", ") || "등록 없음 → /expiry anthropic YYYY-MM-DD"));
  if (s.key_check) lines.push(`API 키 점검: ${s.key_check.ok ? "정상" : "실패 " + s.key_check.status} (${s.key_check.at.slice(0, 16)}Z)`);
  if (s.last_run && s.last_run.run_id) lines.push(`마지막 수집: ${s.last_run.run_id} (${String(s.last_run.started_at).slice(0, 16)}Z)`);
  lines.push(`처리 대기 ${s.queue.queued}건(가장 오래 ${s.queue.oldest_queued_hours}시간) · 보류 ${s.queue.stuck}건`);
  if (s.ai && s.ai.failing_since) lines.push(`AI 분석 중단 중 (${String(s.ai.failing_since).slice(0, 16)}Z부터)`);
  if (s.watchdog && s.watchdog.stale) lines.push(`⚠️ 매일 수집 ${Math.floor(s.watchdog.hours_since)}시간째 성공 기록 없음`);
  return lines.join("\n");
}

async function dailyCheck(env) {
  const r = { at: nowIso() };
  try { r.expiry = await checkExpiries(env); } catch (e) { r.expiry_error = String(e && e.message); }
  try { r.webhooks = await ensureWebhooks(env); } catch (e) { r.webhook_error = String(e && e.message); }
  await kvSet(env, "last_cron", r);
  return r;
}

// ---------------------------------------------------------------- 진입점
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dailyCheck(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    const p = url.pathname;

    if (p === "/health") return json({ ok: true, build: BUILD });
    if (p === "/sports" && request.method === "GET") return sportsPage();
    if (p === "/sports/events" && request.method === "GET") return sportsEvents(env, url);

    const hook = p.match(/^\/hook\/(life|family|kids)$/);
    if (hook && request.method === "POST") {
      if (!env.ADMIN_KEY || !timingSafeEqual(request.headers.get("x-telegram-bot-api-secret-token") || "", await hookSecret(env))) {
        return new Response("forbidden", { status: 403 });
      }
      let upd;
      try { upd = await request.json(); } catch { return new Response("bad json", { status: 400 }); }
      try {
        if (upd.callback_query) await handleCallback(env, hook[1], upd.callback_query, upd.update_id);
        else if (upd.message) await handleMessage(env, hook[1], upd.message);
      } catch (e) {
        console.log("handler error", String((e && e.stack) || e));
      }
      return new Response("ok"); // 텔레그램이 같은 update 를 무한 재전송하지 않도록 항상 200
    }

    if (p.startsWith("/admin/")) {
      if (!isAdmin(env, request)) return json({ ok: false, error: "unauthorized" }, 401);
      let body = {};
      if (request.method === "POST") { try { body = await request.json(); } catch { body = {}; } }
      const POST = request.method === "POST";
      try {
        if (p === "/admin/setup" && POST) return json({ ...(await setupWebhooks(env, url.origin)), heartbeat: await ensureHeartbeat(env) });
        if (p === "/admin/heartbeat" && POST) return json(await beatNow(env));
        if (p === "/admin/test-send" && POST) {
          const bots = Array.isArray(body.bots) && body.bots.length ? body.bots.filter((b) => BOTS[b]) : Object.keys(BOTS);
          return json(await testSend(env, bots));
        }
        if (p === "/admin/clicks") {
          const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") || "20", 10) || 20, 1), 200);
          return json({ ok: true, clicks: (await env.DB.prepare("SELECT * FROM clicks ORDER BY id DESC LIMIT ?").bind(limit).all()).results });
        }
        if (p === "/admin/status") return json({ ok: true, ...(await status(env)) });
        if (p === "/admin/expiry" && POST) return json(await setExpiry(env, body.name, body.expires_on, body.note || null));
        if (p === "/admin/key-check" && POST) return json(await keyCheckReport(env, body));
        if (p === "/admin/run-cron" && POST) return json({ ok: true, ...(await dailyCheck(env)) });
        if (p === "/admin/seen" && POST) return json(await pipeline.seen(env, body));
        if (p === "/admin/enqueue" && POST) return json(await pipeline.enqueue(env, body));
        if (p === "/admin/dispatch" && POST) {
          try { await ensureHeartbeat(env); } catch (e) { console.log("heartbeat ensure", String(e)); }
          return json(await pipeline.dispatch(env, body));
        }
        if (p === "/admin/digest" && POST) {
          const ordinary = await pipeline.digest(env, body);
          let sports;
          try { sports = await sportsWeekly(env, url.origin); } catch (e) { sports = { ok: false, error: String(e.message || e) }; console.log("sports weekly failed", sports.error); }
          return json({ ...ordinary, sports });
        }
        if (p === "/admin/run-log" && POST) return json(await pipeline.runLog(env, body));
        if (p === "/admin/usage") return json(await pipeline.usage(env, request.method, body, url));
        if (p === "/admin/candidates") return json(await pipeline.candidates(env, body, url));
        if (p === "/admin/sources-extra") return json(await pipeline.sourcesExtra(env));
        if (p === "/admin/queue" && POST) return json(await pipeline.queue(env, body));
        if (p === "/admin/event-index") return json(await pipeline.eventIndex(env));
        if (p === "/admin/preview" && POST) return json(await pipeline.preview(env, body.bot, Math.min(Math.max(body.n | 0 || 4, 1), 8), body.keys));
        if (p === "/admin/recommend-update" && POST) return json(await pipeline.recommendUpdate(env, body));
        if (p === "/admin/outbox-skip" && POST) {
          // 운영 정정: 잘못 만들어진 알림 대기열(아직 안 보낸 것)을 보내지 않게 표시. 행사 키와 판을 지정해야 한다
          if (!body.event_key) return json({ ok: false, error: "event_key required" }, 400);
          const r = await env.DB.prepare("UPDATE outbox SET status = 'skipped', reason = ? WHERE event_key = ? AND (? IS NULL OR version = ?) AND status IN ('pending', 'failed', 'uncertain', 'held')")
            .bind(String(body.reason || "운영 정정").slice(0, 100), body.event_key, body.version ?? null, body.version ?? null).run();
          return json({ ok: true, skipped: r.meta.changes });
        }
        if (p === "/admin/events-dump") return json(await pipeline.eventsDump(env, url));
        if (p === "/admin/gaps") return json(await pipeline.gaps(env, request.method, body));
        if (p === "/admin/workload") return json(await pipeline.workload(env, request.method, body, url));
        if (p === "/admin/inbox" && POST) return json(await pipeline.inbox(env, body));
        if (p === "/admin/compare") return json(await pipeline.compare(env, url));
        if (p === "/admin/event") return json(await pipeline.eventLookup(env, url));
        if (p === "/admin/outbox-preview") return json(await pipeline.outboxPreview(env));
        if (p === "/admin/miss-report" && POST) return json(await pipeline.missReport(env, body));
        if (p === "/admin/catchup" && POST) {
          try { return json(await pipeline.catchup(env, body)); } catch (e) { return json({ ok: false, error: String(e && e.stack || e).slice(0, 1500) }, 500); }
        }
        if (p === "/admin/export") return json(await pipeline.exportAll(env, url));
        if (p === "/admin/queries-extra") return json(await pipeline.queriesExtra(env));
        if (p === "/admin/queue-find" && POST) return json(await pipeline.queueFind(env, body.urls));
        if (p === "/admin/events-sample") {
          // 원문 대조 점검용 무작위 표본(행사 정보만, 수신자 정보 없음). 아직 끝나지 않은 행사·접수 안내 중에서
          const n = Math.min(Math.max(parseInt(url.searchParams.get("n") || "40", 10) || 40, 1), 100);
          const rows = (await env.DB.prepare("SELECT event_key, source_id, url, data FROM events WHERE COALESCE(end_date, start_date, registration_opens, '9999-12-31') >= date('now') ORDER BY random() LIMIT ?").bind(n).all()).results;
          const today = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Denver" }).format(new Date());
          return json({ ok: true, events: rows.map((r) => ({ ...JSON.parse(r.data), event_key: r.event_key, source_id: r.source_id, url: r.url, today })) });
        }
        if (p === "/admin/watch" && POST) return json(await pipeline.watch(env, body));
        if (p === "/admin/ai-health") return json(await pipeline.aiHealth(env));
        if (p === "/admin/unverified" && POST) return json(await pipeline.unverified(env, body));
        if (p === "/admin/watchdog") return json(await pipeline.watchdog(env));
        if (p === "/admin/baseline") return json(await pipeline.baseline(env, POST ? body : {}));
        if (p === "/admin/source-health") return json(await pipeline.sourceHealth(env));
        if (p === "/admin/report" && POST) return json(await pipeline.weeklyReport(env, body.send !== false));
      } catch (e) {
        console.log("admin error", p, String((e && e.stack) || e));
        return json({ ok: false, error: String((e && e.message) || e).slice(0, 300) }, 500);
      }
      return json({ ok: false, error: "not found" }, 404);
    }
    return new Response("not found", { status: 404 });
  },
};
