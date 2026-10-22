// 알림 본문과 버튼. 텔레그램 HTML 모드로 보낸다(제목 굵게, 긴 원문 주소는 '바로가기' 글자에 숨김).

const U = "미확인";
const SIGNUP = { fcfs: "선착순", lottery: "추첨", open: "상시 접수", none: "신청 불필요", unknown: null };
const REG = { upcoming: "접수 예정", open: "접수 중", closed: "접수 마감", soldout: "매진", waitlist: "대기자 접수", cancelled: "행사 취소", postponed: "연기" };
const WD = ["일", "월", "화", "수", "목", "금", "토"];
// 텔레그램 한 통 최대 4,096자. 태그·여유를 빼고 이 길이를 넘지 않게 나눈다(일간 요약이 잘려 뒷부분 행사가 사라지던 문제)
export const MAX_TEXT = 3500;

// 텔레그램 HTML 모드: 본문의 & < > 는 반드시 바꿔야 한다
export function esc(x) { return String(x ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
// 항목 하나가 아무리 길어도 한 통(4,096자)을 넘지 않게 자른다(HTML 태그 중간이 잘리면 발송 자체가 거부됨)
function cut(x, n) { const t = String(x ?? ""); return t.length > n ? t.slice(0, n - 1) + "…" : t; }
function attr(x) { return esc(x).replace(/"/g, "&quot;"); }
function link(url, text) { return /^https?:\/\//.test(url || "") ? `<a href="${attr(url)}">${esc(text)}</a>` : esc(text); }

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
    SIGNUP[d.signup] || null].filter(Boolean).join(" · ");
}

// 한 건짜리 알림(즉시·변경·예고·요약의 주요 행사). 비어 있는 항목은 줄째로 뺀다. 원문 주소는 '바로가기' 글자에 숨긴다.
// kind: now | season | digest. opts: {version, note}
export function renderEvent(d, opts = {}) {
  const note = opts.note || "";
  const head = d.registration_status === "cancelled" ? "❌ 취소" : note.startsWith("⏰") ? "⏰" : opts.version > 1 ? "🔄 변경" : opts.kind === "season" ? "📆 예고" : "🆕";
  const when = [dateRange(d), d.time_text].filter(Boolean).join(" ");
  const money = [d.cost_text ? `💲 ${esc(cut(d.cost_text, 200))}` : null, d.age_text ? `👤 ${esc(cut(d.age_text, 100))}` : null].filter(Boolean).join("   ");
  const lines = [`${head} <b>${esc(cut(d.title_ko || d.title, 200))}</b>`];
  if (note.startsWith("⏰")) lines.push(`<b>${esc(cut(note.slice(2).trim(), 200))}</b>`);
  else if (opts.version > 1 && note) lines.push(`바뀐 점: ${esc(cut(note, 400))}`);
  if (opts.kind === "season" && d.year_label) lines.push(esc(d.year_label));
  if (d.org) lines.push(esc(cut(d.org, 150)));
  lines.push("");
  if (when) lines.push(`📅 ${esc(cut(when, 200))}`);
  if (d.location) lines.push(`📍 ${esc(cut(d.location, 200))}`);
  if (money) lines.push(money);
  const reg = regLine(d);
  if (reg) lines.push(`📝 ${esc(reg)}`);
  const cond = [d.eligibility, d.status_note].filter(Boolean).join(" · ");
  if (cond) lines.push(`🛂 ${esc(cut(cond, 500))}`);
  if (Array.isArray(d.english) && d.english.length) lines.push(`🗣 ${esc(d.english.join(" · "))}`);
  lines.push("");
  lines.push(`${link(cut(d.url, 1500), "🔗 바로가기")}`);
  return lines.join("\n");
}

// 요약의 나머지 행사: 제목(누르면 원문)·날짜·비용·접수만 한 줄씩. 버튼 없음.
// 글자 수 기준으로 여러 통으로 나누고, 각 통에 실제로 들어간 행사 목록을 함께 돌려준다(그 행사만 발송 완료로 기록).
export function renderList(botLabel, rows, o = {}) {
  const { mode = "daily", shownBefore = 0 } = o;
  const items = rows.map((r) => {
    const d = r.data;
    const title = String(d.title_ko || d.title || "").slice(0, 120);
    const meta = [dateRange(d), d.cost_text ? String(d.cost_text).slice(0, 60) : null,
      d.registration_opens ? `접수 시작 ${krDate(d.registration_opens)}` : null].filter(Boolean).join(" · ");
    return { row: r, text: `• ${link(cut(d.url, 1500), title)}${meta ? `\n   ${esc(meta)}` : ""}` };
  });
  const pages = [];
  let cur = [], len = 0;
  for (const it of items) {
    if (cur.length && len + it.text.length + 1 > MAX_TEXT - 200) { pages.push(cur); cur = []; len = 0; }
    cur.push(it); len += it.text.length + 1;
  }
  if (cur.length) pages.push(cur);
  const kind = mode === "weekly" ? "이번 주" : "오늘";
  return pages.map((pg, i) => {
    const head = `🗓 ${esc(botLabel)} · ${kind}의 다른 새 소식 ${pages.length > 1 ? `(${i + 1}/${pages.length}) ` : ""}${rows.length}건` +
      (shownBefore ? `\n위의 ${shownBefore}건 말고, 제목을 누르면 원문이 열립니다.` : "\n제목을 누르면 원문이 열립니다.");
    return { text: [head, "", ...pg.map((x) => x.text)].join("\n"), rows: pg.map((x) => x.row) };
  });
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
