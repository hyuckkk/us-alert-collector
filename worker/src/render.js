// 알림 본문과 버튼. 설계안의 '알림 메시지(텔레그램 7줄)' 형식.

const U = "미확인";
const SIGNUP = { fcfs: "선착순", lottery: "추첨", open: "상시 접수", none: "신청 불필요", unknown: U };

function v(x) { return x === null || x === undefined || x === "" ? U : String(x); }
function dateRange(d) {
  if (!d.start_date) return U;
  return d.end_date && d.end_date !== d.start_date ? `${d.start_date}~${d.end_date}` : d.start_date;
}

// kind: now | season | digest-line. change: {version, note}
export function renderEvent(d, opts = {}) {
  const head = opts.version > 1 ? "🔄 변경" : opts.kind === "season" ? "📆 예고" : "🆕";
  const when = [dateRange(d), d.time_text, d.event_tz && d.event_tz !== "America/Denver" && d.denver_time_text ? `(덴버 ${d.denver_time_text})` : null]
    .filter(Boolean).join(" ");
  const where = [d.location || U, d.travel_min ? `이동 약 ${d.travel_min}분` : null].filter(Boolean).join(" · ");
  const english = Array.isArray(d.english) && d.english.length ? d.english.join("·") : U;
  const price = d.cost_text ? `${d.cost_text}${d.price_level ? ` (${d.price_level})` : ""}` : U;
  const signup = [SIGNUP[d.signup] || U, d.deadline ? `마감 ${d.deadline}` : null].filter(Boolean).join(" · ");
  const lines = [
    `${head} ${d.title_ko || d.title}${d.org ? ` · ${d.org}` : ""}`,
    `📅 ${when} · 📍 ${where}`,
    `👤 ${v(d.age_text)} · 영어: ${english}`,
    `💲 ${price}`,
    `📝 ${signup}`,
    `🛂 ${v(d.status_note)}`,
    `🔗 ${d.url} · 확인 ${String(d.checked_at || "").slice(0, 10) || U}`,
  ];
  if (opts.version > 1 && opts.note) lines.splice(1, 0, `   바뀐 점: ${opts.note}`);
  if (opts.kind === "season" && d.year_label) lines.splice(1, 0, `   ${d.year_label}`);
  return lines.join("\n");
}

export function renderDigest(botLabel, rows) {
  const head = `🗓 ${botLabel} 주간 요약 (${rows.length}건)`;
  const body = rows.map((r, i) => {
    const d = r.data;
    return `${i + 1}. ${d.title_ko || d.title}\n   ${dateRange(d)}${d.location ? ` · ${d.location}` : ""}${d.cost_text ? ` · ${d.cost_text}` : ""}\n   ${d.url}`;
  });
  return [head, "", ...body].join("\n").slice(0, 4000);
}

// ---------------------------------------------------------------- 버튼
// 한 건짜리 알림: [확인][관심없음][이 태그 그만]. 요약(여러 건): 줄마다 [n ✅][n 🚫].
export function fullRow(itemKey, st) {
  const b = (on, text, onAction, offAction) => ({ text: on ? `${text} ✓` : text, callback_data: `${on ? offAction : onAction}:${itemKey}` });
  return [
    b(st.state === "ACKNOWLEDGED", "✅ 확인", "ack", "unack"),
    b(st.state === "MUTED", "🚫 관심없음", "mute", "unmute"),
    b(st.tagMuted, "🔕 이 태그 그만", "tag", "untag"),
  ];
}
export function compactRow(itemKey, st, label) {
  const ack = st.state === "ACKNOWLEDGED", mute = st.state === "MUTED";
  return [
    { text: `${label} ✅${ack ? "✓" : ""}`, callback_data: `${ack ? "unack" : "ack"}:${itemKey}` },
    { text: `${label} 🚫${mute ? "✓" : ""}`, callback_data: `${mute ? "unmute" : "mute"}:${itemKey}` },
  ];
}
export function keyboard(itemKey, st) { return { inline_keyboard: [fullRow(itemKey, st)] }; }

// 눌린 버튼이 있는 줄만 새 상태로 바꾼다
export function rebuildKeyboard(markup, itemKey, st) {
  const rows = ((markup || {}).inline_keyboard || []).map((r) => r.slice());
  const idx = rows.findIndex((row) => row.some((b) => (b.callback_data || "").endsWith(`:${itemKey}`)));
  if (idx < 0) return { inline_keyboard: [fullRow(itemKey, st)] };
  const m = /^(\d+) /.exec(rows[idx][0].text || "");
  rows[idx] = m ? compactRow(itemKey, st, m[1]) : fullRow(itemKey, st);
  return { inline_keyboard: rows };
}
