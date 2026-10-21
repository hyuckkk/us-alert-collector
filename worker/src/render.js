// 알림 본문과 버튼. 설계안의 '알림 메시지(텔레그램 7줄)' 형식.

const U = "미확인";
const SIGNUP = { fcfs: "선착순", lottery: "추첨", open: "상시 접수", none: "신청 불필요", unknown: U };

function v(x) { return x === null || x === undefined || x === "" ? U : String(x); }
function dateRange(d) {
  if (!d.start_date) return d.registration_opens || d.deadline ? "행사일 미정(아래 접수 일정 참고)" : U;
  const r = d.end_date && d.end_date !== d.start_date ? `${d.start_date}~${d.end_date}` : d.start_date;
  return d.date_uncertain ? `${r}(연도 추정)` : r;
}

// kind: now | season | digest-line. change: {version, note}
const REG = { upcoming: "접수 예정", open: "접수 중", closed: "접수 마감", soldout: "매진", waitlist: "대기자 접수", cancelled: "행사 취소", postponed: "연기" };

export function renderEvent(d, opts = {}) {
  const note = opts.note || "";
  const head = d.registration_status === "cancelled" ? "❌ 취소" : note.startsWith("⏰") ? "⏰" : opts.version > 1 ? "🔄 변경" : opts.kind === "season" ? "📆 예고" : "🆕";
  const when = [dateRange(d), d.time_text, d.event_tz && d.event_tz !== "America/Denver" && d.denver_time_text ? `(덴버 ${d.denver_time_text})` : null]
    .filter(Boolean).join(" ");
  const where = [d.location || U, d.travel_min ? `이동 약 ${d.travel_min}분` : null].filter(Boolean).join(" · ");
  const english = Array.isArray(d.english) && d.english.length ? d.english.join("·") : U;
  const price = d.cost_text ? `${d.cost_text}${d.price_level ? ` (${d.price_level})` : ""}` : U;
  const signup = [SIGNUP[d.signup] || U, REG[d.registration_status] || null, d.registration_opens ? `접수 시작 ${d.registration_opens}` : null,
    d.deadline ? `마감 ${d.deadline}` : null].filter(Boolean).join(" · ");
  const lines = [
    `${head} ${d.title_ko || d.title}${d.org ? ` · ${d.org}` : ""}`,
    `📅 ${when} · 📍 ${where}`,
    `👤 ${v(d.age_text)} · 영어: ${english}`,
    `💲 ${price}`,
    `📝 ${signup}`,
    `🛂 ${[d.eligibility, d.status_note].filter(Boolean).join(" · ") || U}`,
    `🔗 ${d.url} · 확인 ${String(d.checked_at || "").slice(0, 10) || U}`,
  ];
  if (note.startsWith("⏰")) lines.splice(1, 0, `   ${note.slice(2)}`);
  else if (opts.version > 1 && note) lines.splice(1, 0, `   바뀐 점: ${note}`);
  if (opts.kind === "season" && d.year_label) lines.splice(1, 0, `   ${d.year_label}`);
  return lines.join("\n");
}

// 요약: 여러 통으로 나뉘면 (1/3) 처럼 표시하고 번호는 전체 기준으로 이어 붙인다
export function renderDigest(botLabel, rows, o = {}) {
  const { part = 1, parts = 1, total = rows.length, offset = 0, mode = "daily" } = o;
  const head = `🗓 ${botLabel} ${mode === "weekly" ? "주간" : "오늘의"} 새 정보 ${parts > 1 ? `(${part}/${parts}, 총 ${total}건)` : `(${total}건)`}`;
  const body = rows.map((r, i) => {
    const d = r.data;
    const reg = d.registration_opens ? ` · 접수 ${d.registration_opens}` : "";
    return `${offset + i + 1}. ${d.title_ko || d.title}\n   ${dateRange(d)}${d.location ? ` · ${d.location}` : ""}${d.cost_text ? ` · ${d.cost_text}` : ""}${reg}\n   ${d.url}`;
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
