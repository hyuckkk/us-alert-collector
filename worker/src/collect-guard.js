import { kvGet, kvSet, claimNotice, notifyOwner, localDate } from "./common.js";
const LIGHT_LATE_HOURS=6, FULL_LATE_HOURS=26;
// 수집 지연 감시: 마지막 실행·성공 기록을 보고 늦었으면 GitHub 에 수집 실행을 요청한다(6시간 구간마다 최대 1번)
export async function collectGuard(env, now = Date.now()) {
  const lastRun = await kvGet(env, "last_run_at");
  const lastOk = await kvGet(env, "last_collect_ok_at");
  const h = (x) => (x ? (now - Date.parse(x)) / 3600000 : null);
  const sinceRun = h(lastRun), sinceOk = h(lastOk);
  if ((lastRun && !Number.isFinite(sinceRun)) || (lastOk && !Number.isFinite(sinceOk))) return { late: false, action: "invalid-run-timestamp", error: "collection timestamps require investigation" };
  // 한 번도 실행된 적이 없으면(배포 직후) 판단하지 않는다
  if (sinceRun === null) return { late: false, reason: "no-run-yet" };
  let mode = null;
  if (sinceOk === null || sinceOk > FULL_LATE_HOURS) mode = "full";
  else if (sinceRun > LIGHT_LATE_HOURS) mode = "light";
  const out = { late: !!mode, mode, hours_since_run: Math.round(sinceRun * 10) / 10, hours_since_ok: sinceOk === null ? null : Math.round(sinceOk * 10) / 10 };
  if (!mode) return out;
  const lastRequest = await kvGet(env, "collect_recovery_attempt_at");
  if (lastRequest && now - Date.parse(lastRequest) < 6 * 3600000) return { ...out, action: "already-requested" };
  const slot = `${new Date(now).toISOString().slice(0, 10)}:${Math.floor(new Date(now).getUTCHours() / 6)}`;
  const claim = await env.DB.prepare("INSERT INTO notices(key, sent_at) SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM notices WHERE key LIKE 'collect-recover:%' AND sent_at > ?) ON CONFLICT(key) DO NOTHING")
    .bind(`collect-recover:${slot}`, new Date(now).toISOString(), new Date(now - 6 * 3600000).toISOString()).run();
  if (claim.meta.changes !== 1) return { ...out, action: "already-requested" };
  await kvSet(env, "collect_recovery_attempt_at", new Date(now).toISOString());
  const reason = mode === "full" ? `전체 수집 성공 기록이 ${sinceOk === null ? "없거나" : Math.floor(sinceOk) + "시간째 없거나"} 지연되어` : `수집 실행 기록이 ${Math.floor(sinceRun)}시간째 없어`;
  if (env.GH_DISPATCH_TOKEN && env.GH_REPO) {
    let res;
    try { res = await fetch(`${env.GH_API || "https://api.github.com"}/repos/${env.GH_REPO}/actions/workflows/collect.yml/dispatches`, {
      method: "POST",
      headers: { authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "us-alert-worker", "content-type": "application/json" },
      body: JSON.stringify({ ref: "main", inputs: { mode, dry_run: "false" } }),
      signal: AbortSignal.timeout(10000),
    }); } catch { res = { status: 0 }; }
    out.action = res.status === 204 ? "requested" : `request-failed-${res.status}`;
    if (res.status !== 204 && (await claimNotice(env, `collect-recover-fail:${localDate(env)}`))) {
      await notifyOwner(env, `⚠️ ${reason} 다시 실행을 요청했지만 ${res.status ? "실패했습니다(응답 " + res.status + ")" : "응답을 확인하지 못했습니다"}.\nGitHub 수집 실행 토큰의 권한·만료를 확인해 주세요. 발송 경로는 별도로 동작합니다.`);
    }
  } else {
    out.action = "no-token";
    if (await claimNotice(env, `collect-late:${localDate(env)}`)) {
      await notifyOwner(env, `⚠️ ${mode === "light" ? "수집이 " + Math.floor(sinceRun) + "시간째 실행되지 않았습니다" : reason + " 확인이 필요합니다"}(예약 지연 또는 수집 실패 확인 필요).\n자동 재실행용 토큰이 아직 없어 직접 실행하지 못했습니다. 알림 발송은 따로 매시 계속됩니다.`);
    }
  }
  await kvSet(env, "collect_recovery_last", { ...out, at: new Date(now).toISOString() });
  return out;
}
