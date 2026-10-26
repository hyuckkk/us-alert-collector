// 로컬 검사 공통: 가짜 텔레그램 서버 + wrangler dev(로컬 D1). 실제 텔레그램·Cloudflare 에는 아무것도 보내지 않는다.
import { spawn, execFileSync } from "node:child_process";
import http from "node:http";
import { mkdtempSync, rmSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { tmpdir } from "node:os";
import { join, resolve, dirname, basename } from "node:path";
import { createHash } from "node:crypto";

export const ADMIN = "test-admin-key";
export const HOOK = createHash("sha256").update(`us-hook:${ADMIN}`).digest("hex");
export const OWNER = "111";
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 테스트가 D1 을 직접 고치는 동안(execFileSync) 이벤트 루프가 멈춰 keep-alive 연결이 끊길 수 있으므로, 요청마다 새 연결을 쓴다
function request(url, { method = "GET", headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = http.request({ hostname: u.hostname, port: u.port, path: u.pathname + u.search, method, headers, agent: false }, (res) => {
      let b = "";
      res.on("data", (c) => (b += c));
      res.on("end", () => resolve({ status: res.statusCode, ok: res.statusCode < 400, text: async () => b, json: async () => JSON.parse(b) }));
    });
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

// fresh: true 면 배포 직후 상태 그대로(첫날 기준선 대기). 기본은 기준선을 끈 상태로 시작해 일반 동작을 시험한다.
export async function startEnv({ tgPort, wPort, vars = {}, fresh = false }) {
  const persist = mkdtempSync(join(tmpdir(), "us-d1-"));
  const calls = [];
  let nextMsgId = 500;
  const failQueue = []; // 다음 sendMessage 들에 낼 실패: "network" | 429 | 403 | 400 | 500
  const tgServer = http.createServer((req, res) => {
    let b = "";
    req.on("data", (c) => (b += c));
    req.on("end", () => {
      const payload = b ? JSON.parse(b) : {};
      // 같은 가짜 서버가 GitHub API 도 흉내 낸다(매시 감시의 수집 실행 요청)
      if (req.url.startsWith("/repos/")) {
        calls.push({ method: "github", url: req.url, payload, auth: req.headers.authorization || "" });
        res.statusCode = 204; res.end(); return;
      }
      const m = req.url.match(/^\/bot([^/]+)\/(\w+)$/);
      if (m[2] === "sendMessage" && failQueue.length) {
        const f = failQueue.shift();
        calls.push({ token: m[1], method: m[2], payload, failed: f });
        if (f === "network") { req.socket.destroy(); return; }
        res.setHeader("content-type", "application/json");
        res.statusCode = f;
        res.end(JSON.stringify({ ok: false, error_code: f, description: `fake ${f}`, ...(f === 429 ? { parameters: { retry_after: 1 } } : {}) }));
        return;
      }
      calls.push({ token: m[1], method: m[2], payload });
      let result = true;
      if (m[2] === "sendMessage") result = { message_id: nextMsgId++, chat: { id: payload.chat_id } };
      if (m[2] === "getMe") result = { username: `fake_${m[1]}` };
      if (m[2] === "getWebhookInfo") result = { url: "" };
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ ok: true, result }));
    });
  });
  await new Promise((r) => tgServer.listen(tgPort, "127.0.0.1", r));

  const allVars = {
    TG_API: `http://127.0.0.1:${tgPort}`, BOT_TOKEN_LIFE: "tLIFE", BOT_TOKEN_FAMILY: "tFAMILY", BOT_TOKEN_KIDS: "tKIDS",
    OWNER_CHAT_ID: OWNER, ADMIN_KEY: ADMIN, OWNER_TZ: "Asia/Seoul", DELIVERY_MODE: "owner-only", NOTIFY_FAR_DAYS: "100000", ...vars,  // 운영 설정과 무관하게 시험은 소유자 전용이 기본(지인 발송 시험은 vars 로 켬)
  };
  const wr = resolve("node_modules/wrangler/bin/wrangler.js");
  const localEnv = { ...process.env, CI: "1", WRANGLER_SEND_METRICS: "false", WRANGLER_LOG_PATH: join(persist, 'logs') };
  execFileSync(process.execPath, [wr, "d1", "migrations", "apply", "us-alert-db", "--local", "--persist-to", persist], { stdio: "pipe", env: localEnv });
  const dev = spawn(process.execPath, [wr, "dev", "--port", String(wPort), "--ip", "127.0.0.1", "--persist-to", persist, "--test-scheduled",
    ...Object.entries(allVars).flatMap(([k, v]) => ["--var", `${k}:${v}`])], { stdio: ["ignore", "pipe", "pipe"], env: localEnv, windowsHide: true });
  let log = "";
  dev.stdout.on("data", (d) => (log += d));
  dev.stderr.on("data", (d) => (log += d));

  const W = `http://127.0.0.1:${wPort}`;
  for (let i = 0; ; i++) {
    try { const r = await fetch(`${W}/health`, { signal: AbortSignal.timeout(1000) }); if (r.ok) break; } catch {}
    if (i > 120 || /Build failed/.test(log)) { dev.kill(); tgServer.close(); throw new Error("wrangler dev did not start\n" + log); }
    await sleep(500);
  }
  let upd = 1000;
  const env = {
    W, calls,
    failNext: (...kinds) => failQueue.push(...kinds),
    log: () => log,
    since: (n) => calls.slice(n),
    get: (path) => request(`${W}${path}`),
    admin: (path, body) => request(`${W}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { authorization: `Bearer ${ADMIN}`, "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then((r) => r.json()),
    hook: (bot, update, secret = HOOK) => request(`${W}/hook/${bot}`, {
      method: "POST", headers: { "x-telegram-bot-api-secret-token": secret, "content-type": "application/json" },
      body: JSON.stringify({ update_id: update.update_id ?? ++upd, ...update }),
    }),
    msg: (from, text) => ({ message: { message_id: 1, from: { id: Number(from), first_name: "T" + from }, chat: { id: Number(from), type: "private" }, text } }),
    cb: (from, data, message_id, reply_markup) => ({ callback_query: { id: "cq" + upd, from: { id: Number(from) }, data, message: { message_id, chat: { id: Number(from) }, reply_markup } } }),
    // 테스트에서 시간 경과를 흉내 내려고 로컬 D1 을 직접 고친다(운영 코드는 이 경로를 쓰지 않음)
    sql: (command) => {
      // A second workerd process against the same WAL causes SQLITE_IOERR_TRUNCATE on Windows.
      // Use SQLite's ordinary connection/locking for test-only inspection instead.
      if (process.platform === 'win32') {
        const folder = join(persist, 'v3', 'd1', 'miniflare-D1DatabaseObject');
        const file = readdirSync(folder).find(f => f.endsWith('.sqlite') && f !== 'metadata.sqlite');
        const db = new DatabaseSync(join(folder, file));
        try { db.exec('PRAGMA busy_timeout=10000'); if (/^\s*(SELECT|WITH|PRAGMA)\b/i.test(command)) return db.prepare(command).all().map(row => ({ ...row })); db.exec(command); return []; } finally { db.close(); }
      }
      return JSON.parse(execFileSync(process.execPath, [wr, "d1", "execute", "us-alert-db", "--local", "--persist-to", persist, "--json", "--command", command],
        { stdio: "pipe", env: localEnv }).toString())[0].results;
    },
    stop: () => { dev.once('exit', () => {
      if (dirname(resolve(persist)) !== resolve(tmpdir()) || !basename(persist).startsWith('us-d1-')) throw new Error('unsafe test cleanup path');
      try { rmSync(persist, { recursive: true, force: true, maxRetries: 4, retryDelay: 200 }); } catch { /* Windows child handles may remain; preserve local test files. */ }
    }); dev.kill("SIGTERM"); tgServer.close(); },
  };
  if (!fresh) await env.admin("/admin/baseline", { hours: 0 });
  return env;
}

export function stepper() {
  let failed = false;
  const step = async (name, fn) => {
    try { await fn(); console.log("✔", name); } catch (e) { failed = true; console.log("✘", name, "\n ", e.message); }
  };
  return { step, failed: () => failed };
}
