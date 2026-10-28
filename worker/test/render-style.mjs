// 이슈 #46: 행사 알림 본문은 한국 텔레그램처럼 짧은 한국어, 장식 이모지 없이.
// 실행: node test/render-style.mjs
import assert from "node:assert/strict";
import { renderEvent, renderCount, keyboard } from "../src/render.js";

const base = {
  title_ko: "Whaaat!? 실험적 게임 페스티벌",
  url: "https://whaaat.io/",
  start_date: "2026-11-07",
  time_text: "오전 10시~오후 5시",
  location: "CU Boulder ATLAS 센터",
  cost_text: "투어리스트 패스 $21; 자율 지불 옵션",
  registration_status: "open",
  signup: "fcfs",
  highlights: ["선착순", "특별 체험", "연례 행사"],
  annual: true,
  summary_ko: "CU 볼더에서 실험 게임과 빈티지 게임기를 직접 체험하는 연례 축제입니다.",
  special_reason: "연 1회 실험 게임 축제, 평소 접하기 어려운 게임을 직접 체험할 수 있음",
};
const text = renderEvent(base, { kind: "digest" });
assert.match(text, /^<b><a href="https:\/\/whaaat.io\/">Whaaat!\? 실험적 게임 페스티벌<\/a><\/b>/);
assert.match(text, /CU 볼더에서 실험 게임/);
assert.match(text, /일시: 11월 7일\(토\)/);
assert.match(text, /장소: <a href="/);
assert.match(text, /비용: 투어리스트 패스 \$21/);
assert.match(text, /신청: 접수 중 · 선착순/);
assert.equal(text.includes("연 1회 실험 게임 축제, 평소 접하기 어려운"), false);
assert.equal(text.includes("특별 체험"), false);
assert.equal(text.includes("🎉") || text.includes("✨") || text.includes("📅") || text.includes("🏃") || text.includes("🆕"), false);
assert.ok(text.split("\n").length <= 7, "일반 행사에 장식 줄이 없어야 함");

const restricted = renderEvent({
  ...base, summary_ko: "",
  cost_text: "총 5인 $125, 헬멧 별도",
  age_text: "7~10세, 보호자 동반",
  eligibility: "비미국 시민권자 입장 조건 미확인",
  status_note: "안전요건 확인 필요",
  signup: "lottery", deadline: "2026-11-01",
}, {kind:"now"});
assert.match(restricted, /연 1회 실험 게임 축제/); // 요약 없을 때 특별 이유 사용
assert.match(restricted, /총 5인 \$125, 헬멧 별도/);
assert.match(restricted, /대상: 7~10세, 보호자 동반/);
assert.match(restricted, /추첨/);
assert.match(restricted, /비미국 시민권자 입장 조건 미확인/);
assert.match(restricted, /안전요건 확인 필요/);

assert.match(renderEvent({...base,registration_status:"cancelled"}), /^\[취소\] /);
assert.match(renderEvent(base,{kind:"now",version:2,note:"신청 기간 변경"}), /^\[변경\] /);
assert.match(renderEvent(base,{kind:"season",version:1}), /^\[사전 안내\] /);
assert.match(renderEvent(base,{kind:"now",note:"⏰ 오늘 오후 마감"}), /^\[마감 임박\] /);
assert.match(renderEvent({...base,title_ko:"연구 & 체험 <행사>", url:"https://test.com/?a=1&b=2"}), /연구 &amp; 체험 &lt;행사&gt;/);
assert.match(renderEvent({...base,title_ko:"연구 & 체험 <행사>", url:"https://test.com/?a=1&b=2"}), /href="https:\/\/test.com\/\?a=1&amp;b=2"/);
assert.match(renderEvent({...base,cost_text:undefined}), /비용: 입장료 미확인/);
assert.match(renderCount("가족",{total:9,sent:2,held:7}), /^오늘 새로 찾은 가족 소식 9건/);

const buttons=keyboard("sample",{state:"ACKNOWLEDGED"},{url:"https://whaaat.io/"});
assert.equal(buttons.inline_keyboard[0].length,2);
assert.equal(buttons.inline_keyboard[0][0].callback_data,"unack:sample");
assert.equal(buttons.inline_keyboard[0][1].callback_data,"mute:sample");
console.log("render-style: Korean-style message, critical facts, status, HTML and buttons passed");
