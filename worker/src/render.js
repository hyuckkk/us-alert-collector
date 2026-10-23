// 알림 본문과 버튼. 텔레그램 HTML 모드(제목 굵게). 원문 주소는 본문에 쓰지 않고 [🔗 공식 안내] 버튼으로 연결한다.

const U = "미확인";
const SIGNUP = { fcfs: "선착순", lottery: "추첨", open: "상시 접수", none: "신청 불필요", unknown: null };
const REG = { upcoming: "접수 예정", open: "접수 중", closed: "접수 마감", soldout: "매진", waitlist: "대기자 접수", cancelled: "행사 취소", postponed: "연기" };
const WD = ["일", "월", "화", "수", "목", "금", "토"];

// 텔레그램 HTML 모드: 본문의 & < > 는 반드시 바꿔야 한다
export function esc(x) { return String(x ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
// 항목 하나가 아무리 길어도 한 통(4,096자)을 넘지 않게 자른다(HTML 태그 중간이 잘리면 발송 자체가 거부됨)
function cut(x, n) { const t = String(x ?? ""); return t.length > n ? t.slice(0, n - 1) + "…" : t; }

// 2026-10-27 → 10월 27일(화). 올해가 아니면 연도를 붙인다
export function krDate(ymd, thisYear = new Date().getUTCFullYear()) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(ymd || "");
  if (!m) return null;
  const wd = WD[new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).getUTCDay()];
  return `${+m[1] !== thisYear ? `${m[1]}년 ` : ""}${+m[2]}월 ${+m[3]}일(${wd})`;
}
function dateRange(d) {
  if (!d.start_date) return d.registration_opens || d.deadline ? "행사일 미정 · 접수 일정 아래 참고" : null;
  const r = d.end_date && d.end_date !== d.start_date ? `${krDate(d.start_date)} ~ ${krDate(d.end_date)}` : krDate(d.start_date);
  return d.date_uncertain ? `${r} (연도 추정 — 원문 확인)` : r;
}
function regLine(d) {
  return [d.registration_opens ? `접수 시작 ${krDate(d.registration_opens)}` : null, d.deadline ? `마감 ${krDate(d.deadline)}` : null,
    REG[d.registration_status] && !(d.registration_opens && d.registration_status === "upcoming") ? REG[d.registration_status] : null,
    d.signup === "fcfs" || d.signup === "lottery" ? null : SIGNUP[d.signup] || null].filter(Boolean).join(" · "); // 선착순·추첨은 특징 표시에 이미 나옴
}

// 특징 표시(눈에 띄게 제목 아래). 추천 판정이 고른 것 + 데이터에서 확실히 알 수 있는 것
const HL_ICON = { "선착순": "🏃", "무료": "🆓", "예약 필요": "📌", "추첨": "🎲", "접수 예정": "🗓", "마감 임박": "⏳", "특별 체험": "✨", "허가 필요": "🎫", "연례 행사": "🎉" };
export function badges(d, today = new Date().toISOString().slice(0, 10)) {
  const set = new Set(Array.isArray(d.highlights) ? d.highlights : []);
  if (d.signup === "fcfs") set.add("선착순");
  if (d.signup === "lottery") set.add("추첨");
  if (/^\s*(free|무료)/i.test(d.cost_text || "")) set.add("무료");
  if (d.registration_opens && d.registration_opens > today) set.add("접수 예정");
  if (d.deadline && d.deadline >= today && Date.parse(d.deadline) - Date.parse(today) <= 3 * 86400000) set.add("마감 임박");
  if (d.annual) set.add("연례 행사");
  const order = ["마감 임박", "선착순", "추첨", "접수 예정", "예약 필요", "허가 필요", "무료", "특별 체험", "연례 행사"];
  return order.filter((x) => set.has(x)).slice(0, 4).map((x) => `${HL_ICON[x]} ${x}`).join("  ");
}

// 한 건짜리 알림. 본문에는 원문 주소를 넣지 않는다(메시지 아래 [🔗 공식 안내] 버튼으로 연결).
// 확인된 정보만 짧게: 특징 → 한두 줄 설명 → 날짜·시간 → 장소 → 비용·대상 → 신청. 없는 항목은 줄째로 뺀다.
// kind: now | season | digest | more. opts: {version, note}
export function renderEvent(d, opts = {}) {
  const note = opts.note || "";
  const head = d.registration_status === "cancelled" ? "❌ 취소" : note.startsWith("⏰") ? "⏰" : opts.version > 1 ? "🔄 변경" : opts.kind === "season" ? "📆 예고" : "🆕";
  const lines = [`${head} <b>${esc(cut(d.title_ko || d.title, 120))}</b>`];
  if (note.startsWith("⏰")) lines.push(`<b>${esc(cut(note.slice(2).trim(), 200))}</b>`);
  else if (opts.version > 1 && note) lines.push(`바뀐 점: ${esc(cut(note, 300))}`);
  if (opts.kind === "season" && d.year_label) lines.push(esc(d.year_label));
  const b = badges(d);
  if (b) lines.push(b);
  if (d.special_reason) lines.push(`✨ <b>${esc(cut(d.special_reason, 160))}</b>`); // 왜 특별한지 한 문장
  if (d.summary_ko) lines.push(esc(cut(d.summary_ko, 240)));
  lines.push("");
  const when = [dateRange(d), d.time_text].filter(Boolean).join(" ");
  if (when) lines.push(`📅 ${esc(cut(when, 160))}`);
  const where = [d.location, d.org && !String(d.location || "").includes(d.org) ? d.org : null].filter(Boolean).join(" · ");
  if (where) lines.push(`📍 ${esc(cut(where, 160))}`);
  const money = [d.cost_text ? `💲 ${esc(cut(d.cost_text, 120))}` : null, d.age_text ? `👤 ${esc(cut(d.age_text, 80))}` : null].filter(Boolean).join("   ");
  if (money) lines.push(money);
  const reg = regLine(d);
  if (reg) lines.push(`📝 ${esc(cut(reg, 200))}`);
  const cond = [d.eligibility, d.status_note].filter(Boolean).join(" · ");
  if (cond) lines.push(`🛂 ${esc(cut(cond, 300))}`);
  while (lines[lines.length - 1] === "") lines.pop();
  return lines.join("\n");
}

// 요약은 목록이 아니라 짧은 개수 안내 한 통(버튼·주소 없음). 상세는 각각의 메시지 또는 /more
export function renderCount(botLabel, o) {
  const { total, sent, held } = o;
  const lines = [`🗓 오늘 새로 찾은 ${esc(botLabel)} 소식 ${total}건`];
  if (sent) lines.push(`중요한 ${sent}건을 따로 보냈습니다.`);
  if (held) lines.push(`나머지 ${held}건은 /more 를 보내면 중요한 순서로 5건씩 보여 드립니다.`);
  return lines.join("\n");
}

// ---------------------------------------------------------------- 버튼
// [🔗 공식 안내](원문으로 바로 열림) / [✅ 확인] [🚫 관심없음]. '이런 종류 그만'은 관심없음을 누른 뒤에만 나타난다(버튼 배치를 단순하게).
export function actionRow(itemKey, st) {
  const ack = st.state === "ACKNOWLEDGED", mute = st.state === "MUTED";
  const row = [
    { text: ack ? "✅ 확인함" : "✅ 확인", callback_data: `${ack ? "unack" : "ack"}:${itemKey}` },
    { text: mute ? "🚫 관심없음 ✓" : "🚫 관심없음", callback_data: `${mute ? "unmute" : "mute"}:${itemKey}` },
  ];
  if (mute || st.tagMuted) row.push({ text: st.tagMuted ? "🔕 이런 종류 그만 ✓" : "🔕 이런 종류 그만", callback_data: `${st.tagMuted ? "untag" : "tag"}:${itemKey}` });
  return row;
}
export function keyboard(itemKey, st, url) {
  const rows = [];
  if (/^https?:\/\//.test(url || "") && url.length <= 2000) rows.push([{ text: "🔗 공식 안내", url }]);
  rows.push(actionRow(itemKey, st));
  return { inline_keyboard: rows };
}

// 눌린 행사의 버튼 줄만 새 상태로 바꾸고, 공식 안내(주소) 버튼 줄은 그대로 둔다
export function rebuildKeyboard(markup, itemKey, st) {
  const rows = ((markup || {}).inline_keyboard || []).map((r) => r.slice());
  const idx = rows.findIndex((row) => row.some((b) => (b.callback_data || "").endsWith(`:${itemKey}`)));
  if (idx < 0) return { inline_keyboard: [...rows.filter((r) => r.some((b) => b.url)), actionRow(itemKey, st)] };
  rows[idx] = actionRow(itemKey, st);
  return { inline_keyboard: rows };
}
