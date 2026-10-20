// 로컬 종단 테스트: 가짜 텔레그램 서버 + wrangler dev(로컬 D1) 로 0단계 흐름 전체를 확인한다.
// 실행: cd worker && npm test   (실제 텔레그램·Cloudflare 에는 아무것도 보내지 않는다)
import { spawn, execFileSync } from "node:child_process";
import http from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";

const TG_PORT = 9988, W_PORT = 8799;
const W = `http://127.0.0.1:${W_PORT}`;
const ADMIN = "test-admin-key";
const HOOK = createHash("sha256").update(`us-hook:${ADMIN}`).digest("hex");
const OWNER = "111", OTHER = "222";
const persist = mkdtempSync(join(tmpdir(), "us-d1-"));

// ---- 가짜 텔레그램 API
const calls = [];
let nextMsgId = 500;
const tgServer = http.createServer((req, res) => {
  let b = "";
  req.on("data", (c) => (b += c));
  req.on("end", () => {
    const m = req.url.match(/^\/bot([^/]+)\/(\w+)$/);
    const payload = b ? JSON.parse(b) : {};
    calls.push({ token: m[1], method: m[2], payload });
    let result = true;
    if (m[2] === "sendMessage") result = { message_id: nextMsgId++, chat: { id: payload.chat_id } };
    if (m[2] === "getMe") result = { username: `fake_${m[1]}` };
    if (m[2] === "getWebhookInfo") result = { url: "" };
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ ok: true, result }));
  });
});
await new Promise((r) => tgServer.listen(TG_PORT, "127.0.0.1", r));

const vars = {
  TG_API: `http://127.0.0.1:${TG_PORT}`, BOT_TOKEN_LIFE: "tLIFE", BOT_TOKEN_FAMILY: "tFAMILY", BOT_TOKEN_KIDS: "tKIDS",
  OWNER_CHAT_ID: OWNER, ADMIN_KEY: ADMIN, OWNER_TZ: "Asia/Seoul",
};
const wr = ["node_modules/.bin/wrangler"];
execFileSync(wr[0], ["d1", "migrations", "apply", "us-alert-db", "--local", "--persist-to", persist], { stdio: "pipe", env: { ...process.env, CI: "1" } });
const dev = spawn(wr[0], ["dev", "--port", String(W_PORT), "--ip", "127.0.0.1", "--persist-to", persist, "--test-scheduled",
  ...Object.entries(vars).flatMap(([k, v]) => ["--var", `${k}:${v}`])], { stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, CI: "1" } });
let devLog = "";
dev.stdout.on("data", (d) => (devLog += d));
dev.stderr.on("data", (d) => (devLog += d));

async function waitReady() {
  for (let i = 0; i < 120; i++) {
    try { const r = await fetch(`${W}/health`); if (r.ok) return; } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("wrangler dev did not start\n" + devLog);
}
const admin = (path, body) => fetch(`${W}${path}`, {
  method: body === undefined ? "GET" : "POST",
  headers: { authorization: `Bearer ${ADMIN}`, "content-type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
}).then((r) => r.json());
let upd = 1000;
const hook = (bot, update, secret = HOOK) => fetch(`${W}/hook/${bot}`, {
  method: "POST", headers: { "x-telegram-bot-api-secret-token": secret, "content-type": "application/json" },
  body: JSON.stringify({ update_id: update.update_id ?? ++upd, ...update }),
});
const msg = (from, text) => ({ message: { message_id: 1, from: { id: Number(from), first_name: "T" + from }, chat: { id: Number(from), type: "private" }, text } });
const cb = (from, data, message_id) => ({ callback_query: { id: "cq" + upd, from: { id: Number(from) }, data, message: { message_id, chat: { id: Number(from) } } } });
const since = (n) => calls.slice(n);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ymdPlus = (days) => new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul" }).format(new Date(Date.now() + days * 86400000));

let failed = false;
const step = async (name, fn) => {
  try { await fn(); console.log("✔", name); } catch (e) { failed = true; console.log("✘", name, "\n ", e.message); }
};

try {
  await waitReady();

  await step("관리 경로는 ADMIN_KEY 없으면 401", async () => {
    const r = await fetch(`${W}/admin/status`);
    assert.equal(r.status, 401);
  });

  await step("/admin/setup: 봇 3개 웹훅·명령 등록", async () => {
    const n = calls.length;
    const r = await admin("/admin/setup", {});
    assert.equal(r.ok, true, JSON.stringify(r));
    const hooks = since(n).filter((c) => c.method === "setWebhook");
    assert.equal(hooks.length, 3);
    for (const h of hooks) {
      assert.equal(h.payload.secret_token, HOOK);
      assert.match(h.payload.url, /\/hook\/(life|family|kids)$/);
    }
  });

  await step("웹훅 비밀값이 틀리면 403", async () => {
    const r = await hook("life", msg(OWNER, "/start"), "wrong");
    assert.equal(r.status, 403);
  });

  await step("소유자 /start → 소유자 안내", async () => {
    const n = calls.length;
    await hook("life", msg(OWNER, "/start"));
    const s = since(n).find((c) => c.method === "sendMessage");
    assert.match(s.payload.text, /소유자로 확인/);
  });

  await step("다른 사람 /start → 대기 등록 + 소유자에게 신청 알림 1회", async () => {
    let n = calls.length;
    await hook("family", msg(OTHER, "/start"));
    let sends = since(n).filter((c) => c.method === "sendMessage");
    assert.equal(sends.length, 2);
    assert.ok(sends.some((s) => s.payload.chat_id === OTHER || String(s.payload.chat_id) === OTHER));
    assert.ok(sends.some((s) => String(s.payload.chat_id) === OWNER && /새 수신 신청/.test(s.payload.text) && s.token === "tLIFE"));
    n = calls.length;
    await hook("family", msg(OTHER, "/start"));
    sends = since(n).filter((c) => c.method === "sendMessage");
    assert.equal(sends.length, 1, "두 번째 /start 에는 소유자 알림이 다시 가지 않아야 함");
  });

  let sent;
  await step("/admin/test-send → 봇 3개가 각각 소유자에게 버튼 포함 발송", async () => {
    const n = calls.length;
    const r = await admin("/admin/test-send", {});
    assert.equal(r.ok, true, JSON.stringify(r));
    const sends = since(n).filter((c) => c.method === "sendMessage");
    assert.deepEqual(sends.map((s) => s.token).sort(), ["tFAMILY", "tKIDS", "tLIFE"]);
    for (const s of sends) {
      assert.equal(String(s.payload.chat_id), OWNER);
      assert.equal(s.payload.reply_markup.inline_keyboard[0].length, 3);
    }
    sent = r.results.map((x, i) => ({ ...x, data: sends[i].payload.reply_markup.inline_keyboard[0] }));
  });

  const life = () => sent.find((s) => s.bot === "life");
  await step("[✅ 확인] 클릭 → D1 clicks 기록 + 번호 안내 + 버튼 ✓ 표시", async () => {
    const n = calls.length;
    const ack = life().data[0].callback_data;
    assert.match(ack, /^ack:test:life:/);
    await hook("life", { update_id: 5001, ...cb(OWNER, ack, life().message_id) });
    const ans = since(n).find((c) => c.method === "answerCallbackQuery");
    assert.match(ans.payload.text, /D1 기록 #1/);
    const edit = since(n).find((c) => c.method === "editMessageReplyMarkup");
    assert.equal(edit.payload.reply_markup.inline_keyboard[0][0].text, "✅ 확인 ✓");
    assert.match(edit.payload.reply_markup.inline_keyboard[0][0].callback_data, /^unack:/);
  });

  await step("같은 update 재전송 → 중복 기록 없음", async () => {
    await hook("life", { update_id: 5001, ...cb(OWNER, life().data[0].callback_data, life().message_id) });
    const r = await admin("/admin/clicks");
    assert.equal(r.clicks.length, 1);
    assert.equal(r.clicks[0].action, "ack");
    assert.equal(r.clicks[0].chat_id, OWNER);
  });

  await step("[🚫 관심없음] → 상태가 MUTED 로 바뀌고 확인 ✓ 해제", async () => {
    const n = calls.length;
    await hook("life", cb(OWNER, life().data[1].callback_data, life().message_id));
    const edit = since(n).find((c) => c.method === "editMessageReplyMarkup");
    const row = edit.payload.reply_markup.inline_keyboard[0];
    assert.equal(row[0].text, "✅ 확인");
    assert.equal(row[1].text, "🚫 관심없음 ✓");
  });

  await step("[🔕 이 태그 그만] → 같은 태그의 다른 봇 메시지에도 반영", async () => {
    await hook("life", cb(OWNER, life().data[2].callback_data, life().message_id));
    const kids = sent.find((s) => s.bot === "kids");
    const n = calls.length;
    await hook("kids", cb(OWNER, kids.data[0].callback_data, kids.message_id));
    const edit = since(n).find((c) => c.method === "editMessageReplyMarkup");
    assert.equal(edit.payload.reply_markup.inline_keyboard[0][2].text, "🔕 이 태그 그만 ✓");
  });

  await step("승인 안 된 사람의 버튼 → 거부, 기록 없음", async () => {
    const before = (await admin("/admin/clicks")).clicks.length;
    const n = calls.length;
    await hook("family", cb(OTHER, sent.find((s) => s.bot === "family").data[0].callback_data, 1));
    assert.equal(since(n).find((c) => c.method === "answerCallbackQuery").payload.text, "권한이 없습니다.");
    assert.equal((await admin("/admin/clicks")).clicks.length, before);
  });

  await step("/approve 후에는 그 사람 버튼이 본인 기준으로 기록", async () => {
    await hook("family", msg(OWNER, `/approve ${OTHER}`));
    const n = calls.length;
    await hook("family", cb(OTHER, sent.find((s) => s.bot === "family").data[0].callback_data, 1));
    assert.match(since(n).find((c) => c.method === "answerCallbackQuery").payload.text, /D1 기록 #/);
  });

  await step("/clicks 명령 → 최근 기록 회신", async () => {
    const n = calls.length;
    await hook("life", msg(OWNER, "/clicks"));
    assert.match(since(n).find((c) => c.method === "sendMessage").payload.text, /#1 .* life ack test:life:/);
  });

  await step("/expiry 등록 → 예약 실행 시 D-14 단계 알림 1회만", async () => {
    let n = calls.length;
    await hook("life", msg(OWNER, `/expiry anthropic ${ymdPlus(10)} colorado-alerts 워크스페이스`));
    assert.match(since(n).find((c) => c.method === "sendMessage").payload.text, /10일 남음/);
    n = calls.length;
    await fetch(`${W}/__scheduled?cron=5+0+*+*+*`);
    await sleep(1500);
    let alerts = since(n).filter((c) => c.method === "sendMessage" && /만료 10일 전/.test(c.payload.text));
    assert.equal(alerts.length, 1, JSON.stringify(since(n).map((c) => c.method)));
    assert.equal(alerts[0].token, "tLIFE");
    n = calls.length;
    await admin("/admin/run-cron", {});
    alerts = since(n).filter((c) => c.method === "sendMessage");
    assert.equal(alerts.length, 0, "같은 단계는 다시 알리지 않아야 함");
  });

  await step("만료일이 7일 이내로 바뀌면 다음 단계 알림", async () => {
    await admin("/admin/expiry", { name: "anthropic", expires_on: ymdPlus(3) });
    const n = calls.length;
    const r = await admin("/admin/run-cron", {});
    assert.equal(r.expiry.length, 1);
    assert.match(since(n).find((c) => c.method === "sendMessage").payload.text, /만료 3일 전/);
  });

  await step("키 점검 실패 보고 → 소유자 알림, 같은 날 반복 보고는 알림 1회", async () => {
    let r = await admin("/admin/key-check", { name: "anthropic", ok: false, status: "invalid", detail: "401 authentication_error" });
    assert.equal(r.alerted, true);
    r = await admin("/admin/key-check", { name: "anthropic", ok: false, status: "invalid", detail: "401" });
    assert.equal(r.alerted, false);
    r = await admin("/admin/key-check", { name: "anthropic", ok: true, status: "ok" });
    assert.equal(r.alerted, false);
  });

  await step("/admin/status 요약", async () => {
    const s = await admin("/admin/status");
    assert.equal(s.secrets.BOT_TOKEN_KIDS, true);
    assert.equal(s.key_check.ok, true);
    assert.ok(s.clicks.n >= 4);
    assert.equal(s.deliveries.n, 3);
  });
} finally {
  dev.kill("SIGTERM");
  tgServer.close();
  rmSync(persist, { recursive: true, force: true });
}
if (failed) { console.log("\n--- wrangler log (tail) ---\n" + devLog.slice(-3000)); process.exit(1); }
console.log("\n모든 로컬 검사 통과");
