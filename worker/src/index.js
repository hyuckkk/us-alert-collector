/**
 * us-alert-worker — 미국 체류 알림 시스템의 발송·버튼·DB 담당 (Cloudflare Worker + D1 us-alert-db).
 *
 * 한국 시스템(telegram-relay Worker, alert-collector, telegram-relay 저장소)과는 코드 구조만 참고했고
 * 봇·비밀값·저장소·DB 를 하나도 공유하지 않는다.
 *
 * 비밀값 (GitHub 비밀값에서 deploy-worker 워크플로가 Worker 비밀값으로 복사한다):
 *   BOT_TOKEN_LIFE   @ColoradoLife_bot   (성인)
 *   BOT_TOKEN_FAMILY @ColoradoFamily_bot (가족)
 *   BOT_TOKEN_KIDS   @ColoradoKids_bot   (어린이)
 *   OWNER_CHAT_ID    소유자 텔레그램 숫자 ID
 *   ADMIN_KEY        GitHub Actions → Worker 호출용. Cloudflare 토큰에서 자동 파생 (웹훅 비밀값도 여기서 파생)
 * 일반 변수(wrangler.toml [vars]): TG_API, SYSTEM_BOT, OWNER_TZ
 *
 * 경로
 *   POST /hook/<bot>          텔레그램 웹훅 (X-Telegram-Bot-Api-Secret-Token 검사)
 *   POST /admin/setup         웹훅·명령 메뉴 등록           (Authorization: Bearer ADMIN_KEY)
 *   POST /admin/test-send     소유자 DM 으로 봇별 테스트 발송
 *   GET  /admin/clicks        최근 버튼 클릭 기록
 *   GET  /admin/status        요약 상태
 *   POST /admin/expiry        {"name","expires_on","note"} 만료일 등록
 *   POST /admin/key-check     GitHub Actions 의 API 키 점검 결과 수신
 *   GET  /health              공개 상태 (빌드명만)
 * 예약 실행(매일): 만료 임박 알림, 웹훅 주소 점검.
 */

const BUILD = "us-0.1.0";
const BOTS = {
  life: { label: "성인 활동", handle: "@ColoradoLife_bot", secret: "BOT_TOKEN_LIFE" },
  family: { label: "가족 활동", handle: "@ColoradoFamily_bot", secret: "BOT_TOKEN_FAMILY" },
  kids: { label: "어린이 활동", handle: "@ColoradoKids_bot", secret: "BOT_TOKEN_KIDS" },
};
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

// ---------------------------------------------------------------- 공통
function botToken(env, bot) { return env[BOTS[bot].secret]; }
function isOwner(env, id) { return !!env.OWNER_CHAT_ID && String(id) === String(env.OWNER_CHAT_ID); }
function nowIso() { return new Date().toISOString(); }
function json(body, status = 200) {
  return new Response(JSON.stringify(body, null, 2), { status, headers: { "content-type": "application/json; charset=utf-8" } });
}

function localStamp(env, d = new Date()) {
  const tz = env.OWNER_TZ || "Asia/Seoul";
  const s = new Intl.DateTimeFormat("sv-SE", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
  return `${s} (${tz})`;
}
function localDate(env, d = new Date()) {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: env.OWNER_TZ || "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}
function daysBetween(fromYmd, toYmd) {
  return Math.round((Date.parse(toYmd + "T00:00:00Z") - Date.parse(fromYmd + "T00:00:00Z")) / 86400000);
}

async function tg(env, bot, method, payload) {
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

async function kvGet(env, k) {
  const r = await env.DB.prepare("SELECT v FROM kv WHERE k = ?").bind(k).first();
  return r ? r.v : null;
}
async function kvSet(env, k, v) {
  await env.DB.prepare("INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at")
    .bind(k, typeof v === "string" ? v : JSON.stringify(v), nowIso()).run();
}
// 같은 key 로는 한 번만 true. 시스템 알림 중복 방지용.
async function claimNotice(env, key) {
  const r = await env.DB.prepare("INSERT INTO notices (key, sent_at) VALUES (?, ?) ON CONFLICT(key) DO NOTHING").bind(key, nowIso()).run();
  return r.meta.changes === 1;
}
async function notifyOwner(env, text) {
  if (!env.OWNER_CHAT_ID) return { ok: false, description: "OWNER_CHAT_ID not set" };
  return tg(env, env.SYSTEM_BOT || "life", "sendMessage", { chat_id: env.OWNER_CHAT_ID, text, disable_web_page_preview: true });
}

// ---------------------------------------------------------------- 버튼
function keyboard(itemKey, st) {
  const b = (on, text, onAction, offAction) => ({ text: on ? `${text} ✓` : text, callback_data: `${on ? offAction : onAction}:${itemKey}` });
  return {
    inline_keyboard: [[
      b(st.state === "ACKNOWLEDGED", "✅ 확인", "ack", "unack"),
      b(st.state === "MUTED", "🚫 관심없음", "mute", "unmute"),
      b(st.tagMuted, "🔕 이 태그 그만", "tag", "untag"),
    ]],
  };
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

// ---------------------------------------------------------------- 발송
async function sendItem(env, bot, chatId, item) {
  const st = await itemStateFor(env, chatId, item.item_key);
  const d = await tg(env, bot, "sendMessage", { chat_id: chatId, text: item.text, disable_web_page_preview: true, reply_markup: keyboard(item.item_key, st) });
  const msgId = d.ok && d.result ? d.result.message_id : null;
  await env.DB.prepare("INSERT INTO deliveries (bot, chat_id, item_key, message_id, ok, error, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind(bot, String(chatId), item.item_key, msgId, d.ok ? 1 : 0, d.ok ? null : String(d.description || "").slice(0, 300), nowIso()).run();
  return { bot, ok: !!d.ok, message_id: msgId, error: d.ok ? undefined : d.description };
}

async function testSend(env, bots) {
  if (!env.OWNER_CHAT_ID) return { ok: false, error: "OWNER_CHAT_ID not set" };
  const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 13); // 20261020T0905
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
    out.push(await sendItem(env, bot, env.OWNER_CHAT_ID, { item_key: itemKey, text }));
  }
  return { ok: out.every((r) => r.ok), results: out };
}

// ---------------------------------------------------------------- 텔레그램 웹훅
async function isActiveRecipient(env, bot, id) {
  if (isOwner(env, id)) return true;
  const r = await env.DB.prepare("SELECT status FROM recipients WHERE chat_id = ? AND bot = ?").bind(String(id), bot).first();
  return !!r && r.status === "active";
}

async function handleCallback(env, bot, cq, updateId) {
  const from = String((cq.from && cq.from.id) || "");
  const msg = cq.message || {};
  const chat = msg.chat ? String(msg.chat.id) : from;
  if (!(await isActiveRecipient(env, bot, from))) {
    await tg(env, bot, "answerCallbackQuery", { callback_query_id: cq.id, text: "권한이 없습니다." });
    return;
  }
  const data = cq.data || "";
  const sep = data.indexOf(":");
  const action = sep > 0 ? data.slice(0, sep) : "";
  const itemKey = sep > 0 ? data.slice(sep + 1) : "";
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
    await tg(env, bot, "editMessageReplyMarkup", { chat_id: chat, message_id: msg.message_id, reply_markup: keyboard(itemKey, st) });
  }
}

const HELP_OWNER = [
  "소유자 명령",
  "/test — 이 봇으로 테스트 알림 1건",
  "/clicks — 최근 버튼 기록 10건 (D1)",
  "/status — 요약 상태",
  "/expiry — 만료일 목록",
  "/expiry anthropic 2027-04-20 — 만료일 등록·수정 (이름은 자유)",
  "/expiry del anthropic — 만료일 삭제",
  "/pending — 승인 대기 목록",
  "/approve <ID> — 이 봇 수신 승인",
  "/myid — 내 텔레그램 ID",
].join("\n");

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
    if (cmd === "/start") {
      await env.DB.prepare(
        "INSERT INTO recipients (chat_id, bot, name, username, status, joined_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) " +
        "ON CONFLICT(chat_id, bot) DO UPDATE SET name = excluded.name, username = excluded.username, updated_at = excluded.updated_at, " +
        "status = CASE WHEN recipients.status = 'blocked' THEN 'blocked' WHEN excluded.status = 'active' THEN 'active' ELSE recipients.status END",
      ).bind(fromId, bot, [from.first_name, from.last_name].filter(Boolean).join(" ") || null, from.username || null, owner ? "active" : "pending", at, at).run();
    }
    if (owner) { await reply(`${BOTS[bot].label} 봇 소유자로 확인되었습니다.\n\n${HELP_OWNER}`); return; }
    if (!env.OWNER_CHAT_ID) {
      await reply(`내 텔레그램 ID: ${fromId}\n\n아직 소유자가 설정되지 않았습니다. 이 숫자를 GitHub 비밀값 OWNER_CHAT_ID 로 넣고 deploy-worker 를 다시 실행하면 이 계정이 소유자가 됩니다.`);
      return;
    }
    await reply(`${BOTS[bot].label} 알림 신청이 접수되었습니다. 소유자 승인 후 알림이 옵니다.\n내 텔레그램 ID: ${fromId}`);
    if (cmd === "/start" && (await claimNotice(env, `join:${bot}:${fromId}`))) {
      await notifyOwner(env, `[${BOTS[bot].label}] 새 수신 신청: ${[from.first_name, from.last_name].filter(Boolean).join(" ")} ${from.username ? "@" + from.username : ""}\nID ${fromId}\n승인: ${BOTS[bot].handle} 에서 /approve ${fromId}`);
    }
    return;
  }
  if (!isOwner(env, fromId)) {
    if (await isActiveRecipient(env, bot, fromId)) await reply("알림의 [✅ 확인] [🚫 관심없음] [🔕 이 태그 그만] 버튼은 본인에게만 적용됩니다. 다시 누르면 취소됩니다.");
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
    const r = await env.DB.prepare("UPDATE recipients SET status = 'active', updated_at = ? WHERE chat_id = ? AND bot = ?").bind(nowIso(), args[0], bot).run();
    await reply(r.meta.changes ? `승인: ${args[0]} (${BOTS[bot].label})` : `이 봇에 /start 한 기록이 없습니다: ${args[0]}`);
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

// GitHub 예약 실행이 멈추면(공개 저장소 60일 무활동 등) 키 점검 보고가 끊긴다. 3일 넘게 없으면 알린다.
async function checkKeyCheckStale(env) {
  const raw = await kvGet(env, "keycheck:anthropic");
  if (!raw) return false; // 아직 한 번도 보고되지 않음(설치 전)
  const hours = (Date.now() - Date.parse(JSON.parse(raw).at)) / 3600000;
  if (hours < 72) return false;
  if (await claimNotice(env, `keycheck-stale:${localDate(env)}`)) {
    await notifyOwner(env, `⚠️ API 키 점검 보고가 ${Math.floor(hours / 24)}일째 없습니다. GitHub us-alert-collector 저장소 Actions 탭에서 key-check 실행 상태를 확인해 주세요.`);
  }
  return true;
}

// ---------------------------------------------------------------- 웹훅 등록
async function setupWebhooks(env, origin) {
  if (!env.ADMIN_KEY) return { ok: false, error: "ADMIN_KEY not set" };
  const secret = await hookSecret(env);
  const out = {};
  for (const bot of Object.keys(BOTS)) {
    if (!botToken(env, bot)) { out[bot] = { ok: false, error: `${BOTS[bot].secret} not set` }; continue; }
    const url = `${origin}/hook/${bot}`;
    const w = await tg(env, bot, "setWebhook", { url, secret_token: secret, allowed_updates: ["message", "callback_query"], drop_pending_updates: false });
    const c = await tg(env, bot, "setMyCommands", {
      commands: [
        { command: "start", description: "알림 받기 시작" },
        { command: "help", description: "도움말" },
        { command: "myid", description: "내 텔레그램 ID" },
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
    secrets: Object.fromEntries([...Object.values(BOTS).map((b) => b.secret), "OWNER_CHAT_ID", "ADMIN_KEY"].map((k) => [k, !!env[k]])),
    origin: await kvGet(env, "origin"),
    recipients: recips,
    deliveries: (await one("SELECT COUNT(*) AS n, SUM(ok) AS ok FROM deliveries")),
    clicks: (await one("SELECT COUNT(*) AS n, MAX(at) AS last FROM clicks")),
    expiries: (await env.DB.prepare("SELECT name, expires_on FROM key_expiry ORDER BY expires_on").all()).results,
    key_check: kc ? JSON.parse(kc) : null,
    last_cron: await kvGet(env, "last_cron"),
  };
}
function formatStatus(s) {
  const lines = [`상태 (${s.build})`];
  lines.push("비밀값: " + Object.entries(s.secrets).map(([k, v]) => `${k} ${v ? "○" : "✕"}`).join(", "));
  lines.push(`발송 ${s.deliveries.n || 0}건 (성공 ${s.deliveries.ok || 0}), 버튼 기록 ${s.clicks.n || 0}건`);
  lines.push("수신자: " + (s.recipients.map((r) => `${r.bot}/${r.status} ${r.n}`).join(", ") || "없음"));
  lines.push("만료일: " + (s.expiries.map((e) => `${e.name} ${e.expires_on}`).join(", ") || "등록 없음 → /expiry anthropic YYYY-MM-DD"));
  if (s.key_check) lines.push(`API 키 점검: ${s.key_check.ok ? "정상" : "실패 " + s.key_check.status} (${s.key_check.at.slice(0, 16)}Z)`);
  return lines.join("\n");
}

// ---------------------------------------------------------------- 진입점
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      const r = { at: nowIso() };
      try { r.expiry = await checkExpiries(env); } catch (e) { r.expiry_error = String(e && e.message); }
      try { r.keycheck_stale = await checkKeyCheckStale(env); } catch (e) { r.keycheck_error = String(e && e.message); }
      try { r.webhooks = await ensureWebhooks(env); } catch (e) { r.webhook_error = String(e && e.message); }
      await kvSet(env, "last_cron", r);
    })());
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    const p = url.pathname;

    if (p === "/health") return json({ ok: true, build: BUILD });

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
        console.log("handler error", String(e && e.stack || e));
      }
      return new Response("ok"); // 텔레그램이 같은 update 를 무한 재전송하지 않도록 항상 200
    }

    if (p.startsWith("/admin/")) {
      if (!isAdmin(env, request)) return json({ ok: false, error: "unauthorized" }, 401);
      let body = {};
      if (request.method === "POST") { try { body = await request.json(); } catch { body = {}; } }
      if (p === "/admin/setup" && request.method === "POST") return json(await setupWebhooks(env, url.origin));
      if (p === "/admin/test-send" && request.method === "POST") {
        const bots = Array.isArray(body.bots) && body.bots.length ? body.bots.filter((b) => BOTS[b]) : Object.keys(BOTS);
        return json(await testSend(env, bots));
      }
      if (p === "/admin/clicks") {
        const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") || "20", 10) || 20, 1), 200);
        return json({ ok: true, clicks: (await env.DB.prepare("SELECT * FROM clicks ORDER BY id DESC LIMIT ?").bind(limit).all()).results });
      }
      if (p === "/admin/status") return json({ ok: true, ...(await status(env)) });
      if (p === "/admin/expiry" && request.method === "POST") return json(await setExpiry(env, body.name, body.expires_on, body.note || null));
      if (p === "/admin/key-check" && request.method === "POST") return json(await keyCheckReport(env, body));
      if (p === "/admin/run-cron" && request.method === "POST") return json({ ok: true, expiry: await checkExpiries(env), webhooks: await ensureWebhooks(env) });
      return json({ ok: false, error: "not found" }, 404);
    }
    return new Response("not found", { status: 404 });
  },
};
