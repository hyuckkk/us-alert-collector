import { BOTS, isOwner, nowIso, tg, notifyOwner, ownerOnly, claimNotice, localDate, ownerTz, hourIn, kvSet } from './common.js';
import { welcomeQueue } from './pipeline.js';

const systemBot = env => env.SYSTEM_BOT || 'life';
const validId = id => /^[1-9]\d{0,15}$/.test(String(id));
export function approvalButtons(bot, id, label = '') {
  if (!BOTS[bot] || !validId(id)) return [];
  return [{ text: `${label}✅ 승인`, callback_data: `suba:${bot}:${id}` }, { text: `${label}🚫 거절`, callback_data: `subr:${bot}:${id}` }];
}
export async function pendingPayload(env, page = 1) {
  const total = (await env.DB.prepare("SELECT COUNT(*) AS n FROM recipients WHERE status = 'pending'").first()).n;
  const pages = Math.max(1, Math.ceil(total / 20));
  page = Math.max(1, Math.min(pages, Number.isSafeInteger(page) ? page : 1));
  const rows = (await env.DB.prepare("SELECT chat_id, bot, name, joined_at FROM recipients WHERE status = 'pending' ORDER BY joined_at, bot, chat_id LIMIT 20 OFFSET ?").bind((page - 1) * 20).all()).results || [];
  const lines = rows.map((r, i) => `${i + 1}. [${BOTS[r.bot]?.label || r.bot}] ${String(r.name || '이름 없음').replace(/[\r\n]/g, ' ').slice(0, 40)} · ${String(r.joined_at).slice(0, 16)} UTC`);
  return { total, text: total ? `승인 대기 ${total}건 (${page}/${pages}쪽)\n${lines.join('\n')}${pages > 1 ? '\n다음 목록: /pending ' + (page < pages ? page + 1 : 1) : ''}` : '승인 대기 없음', reply_markup: { inline_keyboard: rows.map((r, i) => approvalButtons(r.bot, r.chat_id, `${i + 1}. `)).filter(r => r.length) } };
}
export async function sendPending(env, page = 1) {
  const { total, ...payload } = await pendingPayload(env, page);
  return notifyOwner(env, payload.text, { reply_markup: payload.reply_markup });
}
export async function decideSubscription(env, bot, id, approve) {
  if (!BOTS[bot] || !validId(id)) return { changed: false };
  const r = await env.DB.prepare("UPDATE recipients SET status = ?, updated_at = ? WHERE chat_id = ? AND bot = ? AND status = 'pending'")
    .bind(approve ? 'active' : 'blocked', nowIso(), String(id), bot).run();
  if (r.meta.changes !== 1) return { changed: false };
  let queued = 0, welcome_error = false;
  if (approve) {
    try { queued = (await welcomeQueue(env, String(id), bot)).queued; } catch { welcome_error = true; }
    if (!ownerOnly(env) || isOwner(env, id)) {
      const sent = await tg(env, bot, 'sendMessage', { chat_id: String(id), text: `${BOTS[bot].label} 알림 신청이 승인되었습니다. /settings 로 설정을 확인하세요.` });
      if (!sent.ok) welcome_error = true;
    }
  }
  return { changed: true, queued, welcome_error };
}
export async function subscriptionCallback(env, bot, cq) {
  const match = /^sub([ar]):(life|family|kids):([1-9]\d{0,15})$/.exec(cq.data || '');
  const msg = cq.message || {};
  const answer = text => tg(env, bot, 'answerCallbackQuery', { callback_query_id: cq.id, text });
  if (!match || !isOwner(env, cq.from?.id) || !isOwner(env, msg.chat?.id) || bot !== systemBot(env)) {
    await answer('권한이 없거나 잘못된 요청입니다.'); return;
  }
  const [, action, targetBot, id] = match;
  const result = await decideSubscription(env, targetBot, id, action === 'a');
  await answer(!result.changed ? '이미 처리됨 / 승인 대기 대상 없음' : (action === 'a' ? '승인했습니다.' : '거절했습니다.') + (result.welcome_error ? ' 첫 안내 준비 실패: 관리자 확인 필요.' : ''));
  if (msg.message_id) {
    // Remove only this applicant's buttons; keep every other applicant in the message.
    const rows = (msg.reply_markup?.inline_keyboard || []).map(row => row.filter(b => ![`suba:${targetBot}:${id}`, `subr:${targetBot}:${id}`].includes(b.callback_data))).filter(row => row.length);
    await tg(env, bot, 'editMessageReplyMarkup', { chat_id: env.OWNER_CHAT_ID, message_id: msg.message_id, reply_markup: { inline_keyboard: rows } });
  }
}
export async function pendingReminder(env, now = new Date()) {
  if (!env.OWNER_CHAT_ID || hourIn(ownerTz(env), now) < 9) return { sent: 0, reason: 'before-hour-or-no-owner' };
  const { total, ...payload } = await pendingPayload(env);
  if (!total) return { sent: 0, reason: 'no-pending' };
  const key = `pending-reminder:${localDate(env, now)}`;
  if (!await claimNotice(env, key)) return { sent: 0, reason: 'already-attempted' };
  // At-most-once attempt: retain claim on uncertain Telegram response, never blindly retry.
  const result = await notifyOwner(env, payload.text, { reply_markup: payload.reply_markup });
  const status = result.ok ? 'sent' : result.network ? 'uncertain' : 'failed';
  await kvSet(env, 'pending_reminder_last', { date: localDate(env, now), status, count: total });
  return { sent: result.ok ? 1 : 0, status, count: total };
}
