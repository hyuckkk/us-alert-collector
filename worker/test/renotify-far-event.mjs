// 11-28 사고 재현: 2월 '골든 리트리버 축제'가 메모·시간 글만 달라져 11/20·11/24·11/27 세 번 나감 → 다시 보내지 않기
import { notifyDiff } from "../src/pipeline.js";
const today = "2026-11-28";
const v2 = { start_date: "2027-02-06", cost_text: "무료", time_text: null, status_note: "보통 2월 첫째 토요일, 확정일 없음", age_text: null };
const v3 = { ...v2, time_text: "오전 9시 시작 (오후 1시 행사 종료 언급)", status_note: "목줄을 한 사교적인 반려견만 · 2월 첫째 토요일" };
const fail = (m, x) => { console.error("FAIL", m, x); process.exit(1); };
let d = notifyDiff(v2, v3, today); if (d.length) fail("먼 행사 시간 보충·메모 변경은 조용히", d);
d = notifyDiff(v3, { ...v3, time_text: "9 a.m. – 2 p.m." }, today); if (d.length) fail("먼 행사 시간 글 변경도 조용히", d);
d = notifyDiff(v3, { ...v3, start_date: "2027-02-13" }, today); if (String(d) !== "start_date") fail("날짜가 바뀌면 다시 알림", d);
d = notifyDiff(v3, { ...v3, cost_text: "$10" }, today); if (String(d) !== "cost_text") fail("비용이 바뀌면 다시 알림", d);
const near = { ...v2, start_date: "2026-12-05" };
d = notifyDiff(near, { ...near, time_text: "오전 10시" }, today); if (String(d) !== "time_text") fail("2주 안 행사의 시간 보충은 알림", d);
d = notifyDiff(near, { ...near, age_text: "5세 이상" }, today); if (d.length) fail("연령 글은 알림 사유 아님", d);
console.log("ok renotify-far-event");
{ const d = notifyDiff(v3, { ...v3, registration_opens: "2027-01-05" }, today); if (String(d) !== "registration_opens") fail("먼 행사라도 접수 시작일이 새로 확인되면 알림", d); }
console.log("ok renotify-far-event (접수)");
// 실제 11-27 바뀐 점: 장소 Parfet Park → Washington Avenue(같은 곳), 메모 추가 → 먼 행사면 조용히
{ const a = { ...v3, location: "Parfet Park", signup: "open", registration_status: "open" };
  const d = notifyDiff(a, { ...a, location: "Washington Avenue", status_note: "보통 2월 첫째 토요일" }, today); if (d.length) fail("먼 행사 장소 표현 차이는 조용히", d); }
console.log("ok renotify-far-event (장소)");
{ const d = notifyDiff(near, { ...near, location: "Denver Central Library" }, today); if (String(d) !== "location") fail("2주 안 행사 장소 변경은 알림", d); }
console.log("ok renotify-far-event (가까운 장소)");
