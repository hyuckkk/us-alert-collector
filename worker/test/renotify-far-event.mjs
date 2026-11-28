// 11-28 구조 정리 재현: 3주 109개 행사 중 19개가 2~4번 나감. 실제 '바뀐 점' 기록으로 다시 안 나가는지 확인
import { significantDiff, resendGate } from "../src/pipeline.js";
const today = "2026-11-28";
const fail = (m, x) => { console.error("FAIL", m, x); process.exit(1); };
const none = (m, a, b) => { const d = significantDiff(a, b, today); if (d.length) fail(m, d); };
const only = (m, a, b, k) => { const d = significantDiff(a, b, today); if (String(d) !== k) fail(m, d); };
// 실제 사례(change_note 그대로)
const golden = { start_date: "2027-02-06", location: "Parfet Park", signup: "open", registration_status: "open", status_note: null, cost_text: "무료" };
none("골든 리트리버: 장소 표현·open→unknown·메모", golden, { ...golden, location: "Washington Avenue", signup: "unknown", registration_status: "unknown", status_note: "보통 2월 첫째 토요일" });
const longmont = { start_date: "2026-12-12", registration_status: "open" };
none("롱몬트 퍼레이드: 접수 상태 open→unknown", longmont, { ...longmont, registration_status: "unknown" });
const holly = { start_date: "2026-12-04", location: "Arnold Hall", eligibility: "기지 출입 필요", status_note: "무료 티켓" };
none("홀리 앤 아이비: 주소 덧붙임·자격·메모", holly, { ...holly, location: "Arnold Hall, 2308 Cadet Dr", eligibility: "전 연령 일반 공개", status_note: "무료 티켓, 남문 PRO" });
const little = { start_date: "2026-11-30", time_text: "오후 5시–8시", signup: "none" };
none("리틀턴 촛불 행진: 시간 표기·신청 방식", little, { ...little, time_text: "오후 5시", signup: "unknown" });
const broom = { start_date: "2026-12-04", time_text: "오후 5시 30분–오후 11시 59분", location: "1 DesCombes Drive", signup: "unknown" };
none("브룸필드 점등식: 끝 시각·건물 이름 덧붙임", broom, { ...broom, time_text: "오후 5시 30분–8시", location: "George Di Ciero City and County Building, One Des Combes Drive", signup: "none" });
const brunch = { start_date: "2026-12-12", time_text: "오전 8:30–오후 3시" };
none("브런치: 시간 표기만", brunch, { ...brunch, time_text: "오전 8시 30분–오후 3시" });
const apex = { start_date: "2026-12-20", time_text: "13:00", location: null, signup: "unknown", registration_status: "unknown" };
none("아펙스: 먼 행사 빈칸 채워짐", apex, { ...apex, time_text: "오후 1시–4시 30분", location: "Simms Street Recreation Center", signup: "open", registration_status: "open" });
// 꼭 다시 알릴 것
only("날짜가 바뀜", golden, { ...golden, start_date: "2027-02-13" }, "start_date");
only("취소", golden, { ...golden, registration_status: "cancelled" }, "registration_status");
only("비용이 바뀜", golden, { ...golden, cost_text: "$10" }, "cost_text");
only("접수 시작이 2주 안으로 확인", golden, { ...golden, registration_opens: "2026-12-05" }, "registration_opens");
none("접수 시작이 먼 날짜면 그날 ⏰로", golden, { ...golden, registration_opens: "2027-01-20" });
only("2주 안 행사가 다른 곳으로", { start_date: "2026-12-05", location: "Westminster Library" }, { start_date: "2026-12-05", location: "Denver Central Library" }, "location");
only("1주 안 행사의 시작 시각 변경", { start_date: "2026-12-01", time_text: "오전 10시" }, { start_date: "2026-12-01", time_text: "오후 2시" }, "time_text");
// 관문: 다른 봇으로 같은 판, 3일 안 사소한 변경, ⏰·날짜 변경은 통과
const L = new Map([["1|k", { v: 1, at: new Date().toISOString() }]]);
if (resendGate(L, "1", "k", 1, "") !== "already-sent") fail("같은 판은 봇이 달라도 한 번");
if (resendGate(L, "1", "k", 2, "시간 오전 10시 → 오후 2시") !== "recently-sent") fail("3일 안 시간만 바뀐 것은 막음");
if (resendGate(L, "1", "k", 2, "⏰ 내일 신청 마감") !== null) fail("⏰은 통과");
if (resendGate(new Map([["1|k", { v: 1, at: new Date().toISOString() }]]), "1", "k", 2, "시작일 a → b") !== null) fail("날짜 변경은 통과");
const M = new Map(); if (resendGate(M, "1", "z", 1, "") !== null || resendGate(M, "1", "z", 1, "") !== "already-sent") fail("한 실행 안 두 봇");
console.log("ok renotify (실제 사례 8건·관문)");
{ const G = new Map([["1|k", { v: 1, at: "2026-11-01T00:00:00Z" }]]);
  if (resendGate(G, "1", "k", 2, "신청 방식 open → unknown, 신분·주민 메모 미확인 → x") !== "not-significant") fail("옛 대기분 신청 방식·메모만 바뀜은 막음");
  if (resendGate(new Map([["1|k", { v: 1, at: "2026-11-01T00:00:00Z" }]]), "1", "k", 2, "비용 $5 → $10") !== null) fail("비용은 통과"); }
console.log("ok renotify (옛 대기분)");
