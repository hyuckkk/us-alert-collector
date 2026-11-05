import { kvGet, kvSet } from './common.js';

const timestamp = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? new Date(value).toISOString() : null;
const safe = value => String(value || '').replace(/sk-[A-Za-z0-9_-]+/g, '[REDACTED]').replace(/(bearer\s+|(?:api[_-]?key|token|authorization)\s*[:=]\s*)[^\s,;]+/gi, '$1[REDACTED]').slice(0, 500);
async function latest(env, key, value) {
  if (!value) return;
  await env.DB.prepare('INSERT INTO kv(k,v,updated_at) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at WHERE julianday(excluded.v) > julianday(kv.v) OR julianday(kv.v) IS NULL').bind(key, value, new Date().toISOString()).run();
}

// Report receipt, collector execution and successful AI responses are different clocks.
export async function recordRunHealth(env, body, received, alerts) {
  const started = timestamp(body.started_at), finished = timestamp(body.finished_at);
  const bounded = t => t && t <= received ? t : null;
  await latest(env, 'last_run_at', received); // backwards-compatible receipt timestamp
  await latest(env, 'last_run_received_at', received);
  await latest(env, 'last_collect_started_at', bounded(started));
  await latest(env, 'last_collect_finished_at', bounded(finished));
  if (body.completed) await latest(env, 'last_collect_ok_at', bounded(finished) || received);
  const ai = body.ai;
  if (!ai || ai.attempted === false || typeof ai.ok !== 'boolean') return;
  const prev = JSON.parse((await kvGet(env, 'ai_health')) || '{}');
  const reportAt = bounded(finished) || bounded(started) || received;
  // Delayed/repeated reports cannot overwrite newer health or repeat recovery notices.
  if (prev.last_reported_at && reportAt <= prev.last_reported_at) return;
  const success = bounded(timestamp(ai.last_success_at));
  const diagnostic = ai.error && typeof ai.error === 'object' ? {
    category: safe(ai.error.category), http_status: Number.isInteger(ai.error.http_status) ? ai.error.http_status : null,
    error_type: safe(ai.error.error_type), message: safe(ai.error.message), at: bounded(timestamp(ai.error.at)),
    request_id: /^req_[\w-]{1,100}$/.test(ai.error.request_id || '') ? ai.error.request_id : null,
  } : null;
  const next = { ...prev, last_reported_at: reportAt, last_attempt_at: diagnostic?.at || success || reportAt };
  if (success && (!prev.last_api_success_at || success > prev.last_api_success_at)) next.last_api_success_at = success;
  if (ai.ok && (ai.attempted === undefined || success)) {
    if (prev.failing_since) alerts.push('AI 분석 복구 (API 응답 확인). 기존 대기열을 순서대로 처리하며 GitHub 수집 지연 여부는 별도로 감시합니다.');
    Object.assign(next, { status: 'ok', last_ok_at: success || reportAt, last_ok_basis: success ? 'api-response' : 'legacy-report', failing_since: null, consecutive: 0, last_error: null, diagnostic: null });
  } else {
    Object.assign(next, { status: success ? 'partial' : 'failed', failing_since: prev.failing_since || diagnostic?.at || reportAt,
      consecutive: (prev.consecutive || 0) + 1, diagnostic, last_error: diagnostic?.category || safe(ai.error) });
  }
  await kvSet(env, 'ai_health', next);
}
