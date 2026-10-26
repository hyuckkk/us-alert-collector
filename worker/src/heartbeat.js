// GitHub 예약 실행과 독립된 매시 실행 경로.
// GitHub Actions 의 예약 작업은 몇 시간씩 늦거나 건너뛸 수 있다(운영 첫날 실제로 4.5시간 지연).
// Cloudflare 예약 실행(cron)은 계정당 5개까지인데 한국 Worker 가 모두 쓰고 있으므로 쓰지 않는다.
// 대신 이 Worker 안의 Durable Object 알람으로 매시 한 번 스스로 깨어나:
//   1) 알림 발송(즉시·⏰ 접수 시작·마감·일간 요약)을 GitHub 와 상관없이 처리하고
//   2) 수집 실행이 늦어지면 GitHub 에 수집 실행을 직접 요청한다(토큰이 있을 때). 토큰이 없으면 소유자에게 알린다.
// GitHub 의 매시 dispatch 작업도 그대로 두며, 같은 알림이 두 번 나가지 않는 것은 발송 대기열의 선점(claim)이 보장한다.
import { DurableObject } from "cloudflare:workers";
import * as pipeline from "./pipeline.js";
import { kvGet, kvSet, nowIso, claimNotice, notifyOwner, localDate } from "./common.js";
import { collectGuard } from "./collect-guard.js";
export { collectGuard } from "./collect-guard.js";
import { pendingReminder } from "./subscriptions.js";

const MINUTE_OF_HOUR = 35;        // 매시 35분(GitHub dispatch 의 20분과 겹치지 않게)

export function nextSlot(now = Date.now()) {
  const d = new Date(now);
  d.setUTCMinutes(MINUTE_OF_HOUR, 0, 0);
  if (d.getTime() <= now + 60000) d.setUTCHours(d.getUTCHours() + 1);
  return d.getTime();
}

export class Heartbeat extends DurableObject {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/beat") {
      const r = await beat(this.env, "admin");
      await this.ensure();
      return Response.json(r);
    }
    await this.ensure();
    return Response.json({ ok: true, next_alarm: new Date(await this.ctx.storage.getAlarm()).toISOString() });
  }

  async ensure() {
    const at = await this.ctx.storage.getAlarm();
    if (!at || at > Date.now() + 2 * 3600000) await this.ctx.storage.setAlarm(nextSlot());
  }

  async alarm() {
    try {
      await beat(this.env, "alarm");
    } finally {
      // 처리 중 오류가 나도 다음 알람은 반드시 건다(멈춘 채로 남지 않게)
      await this.ctx.storage.setAlarm(nextSlot());
    }
  }
}

// Worker 어디서든 알람이 걸려 있는지 확인(배포 직후 setup, 관리 호출 때)
export async function ensureHeartbeat(env) {
  if (!env.HEARTBEAT) return { ok: false, error: "HEARTBEAT binding missing" };
  const stub = env.HEARTBEAT.get(env.HEARTBEAT.idFromName("main"));
  return (await stub.fetch("https://heartbeat/ensure")).json();
}
export async function beatNow(env) {
  if (!env.HEARTBEAT) return { ok: false, error: "HEARTBEAT binding missing" };
  const stub = env.HEARTBEAT.get(env.HEARTBEAT.idFromName("main"));
  return (await stub.fetch("https://heartbeat/beat")).json();
}

// 매시 한 번: 발송·요약·수집 감시. 단계마다 따로 잡아 한 단계 오류가 나머지를 막지 않게 한다.
export async function beat(env, via) {
  const r = { at: nowIso(), via };
  try { r.dispatch = await pipeline.dispatch(env, {}); } catch (e) { r.dispatch_error = String(e && e.message).slice(0, 200); }
  try { r.digest = await pipeline.digest(env, {}); } catch (e) { r.digest_error = String(e && e.message).slice(0, 200); }
  try { r.collect = await collectGuard(env); } catch (e) { r.collect_error = String(e && e.message).slice(0, 200); }
  try { r.pending = await pendingReminder(env); } catch { r.pending_error = "pending reminder failed"; }
  await kvSet(env, "last_heartbeat", { at: r.at, via, sent: r.dispatch ? r.dispatch.sent : null, digest: r.digest ? r.digest.messages : null,
    collect: r.collect || null, pending: r.pending || null, error: r.dispatch_error || r.digest_error || r.collect_error || r.pending_error || null });
  return { ok: true, ...r };
}
