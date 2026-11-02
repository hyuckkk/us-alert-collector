// 동일 모집이 ChatGPT/Claude/GitHub 서로 다른 제목·URL·날짜로 들어와도 한 번만 발송.
// 실제 Telegram 대신 로컬 가짜 서버를 사용하고 D1 새 DB 에서 테스트한다.
import assert from "node:assert/strict";
import { eventNoticeGroup } from "../src/pipeline.js";
import { startEnv, stepper, OWNER } from "./harness.mjs";

const today = new Intl.DateTimeFormat("sv-SE", {timeZone: "America/Denver"}).format(new Date());
const plus = (n) => new Date(Date.parse(today + "T12:00:00Z") + n * 86400000).toISOString().slice(0, 10);
const first = {
  bot: "family", event_key: "gpt-permit",
  title: "Golden Gate Canyon State Park Christmas Tree Cut Lottery",
  title_ko: "골든게이트 캐니언 크리스마스 트리 벌목 추첨",
  url: "https://cpw.state.co.us/event/events-and-festivals/2026/11/christmas-tree-cut-draw",
  location: "Golden Gate Canyon State Park", org: "Colorado Parks and Wildlife",
  start_date: plus(15), end_date: plus(30), registration_opens: plus(5), deadline: plus(15),
  annual: true, signup: "lottery", cost_text: "$35", rec_level: "push",
  special_reason: "250명에게 한정 허가", source_id: "ext-search", checked_at: new Date().toISOString(),
  urgency: "now", tags: ["permit"], registration_status: "upcoming",
};
const other = {
  ...first, event_key: "claude-permit", source_id: "cpw",
  title: "Golden Gate Canyon Christmas Tree Cutting Permit Draw",
  title_ko: "골든게이트 캐년 크리스마스 트리 벌채 추첨",
  url: "https://cpw.state.co.us/state-parks/golden-gate-canyon-state-park",
  start_date: plus(30), end_date: null, registration_opens: plus(5),
};
const third = {
  ...first, event_key: "gnews-permit", source_id: "gnews:tree",
  title: "Permit to Cut Christmas Trees at Golden Gate Canyon",
  title_ko: "골든게이트 캐년 트리 벌목 허가",
  url: "https://news.google.com/rss/articles/not-the-cpw-link",
  start_date: plus(30), registration_opens: plus(5),
};
const group = eventNoticeGroup(first);
assert.ok(group && group===eventNoticeGroup(other) && group===eventNoticeGroup(third));
assert.equal(eventNoticeGroup({...first, title:"Other park tree cutting", title_ko:"다른 공원 크리스마스 트리 벌목",location:"Broomfield State Park",url:"https://example.org/other"}),null);
assert.equal(eventNoticeGroup({...first,start_date:"2027-11-01"}), "cpw:golden-gate:christmas-tree:2027");
assert.notEqual(eventNoticeGroup({...first,start_date:"2027-11-01"}),eventNoticeGroup({...first,start_date:"2028-11-01"}));

const { step, failed }=stepper();
const E = await startEnv({tgPort:9977,wPort:8877,vars:{FAMILY_AVAILABLE_FROM:"2026-01-01"}});
const {admin,hook,msg,since,sql}=E;
const send = n => since(n).filter(x=>x.method==="sendMessage" && !x.failed && String(x.payload.chat_id)===OWNER && /골든게이트/.test(x.payload.text));

try {
  await hook("family",msg(OWNER,"/start"));
  await hook("family",msg(OWNER,"/quiet 0 0"));

  await step("서로 다른 수집원·실제 벌목일 vs 접수일: 같은 수신자에게 한 통만",async()=>{
    const n=E.calls.length;
    await admin("/admin/enqueue",{events:[first,other,third]});
    const res=await admin("/admin/dispatch",{ignoreQuiet:true});
    assert.equal(send(n).length,1,JSON.stringify(res));
    assert.equal(sql("SELECT COUNT(*) AS n FROM notices WHERE key LIKE 'event-group:%'")[0].n,1);
    const again=await admin("/admin/dispatch",{ignoreQuiet:true});
    assert.equal(again.sent,0);
  });

  await step("나중에 별도 수집원에서 다시 가져오거나 접수가 시작되어도 같은 모집 재발송 금지",async()=>{
    const n=E.calls.length;
    const late={...first,event_key:"late-permit",source_id:"new-collector",title_ko:"골든게이트 캐년 크리스마스 트리 벌목 허가 신청",url:"https://cpw.state.co.us/event/another-page"};
    await admin("/admin/enqueue",{events:[late]});
    await admin("/admin/dispatch",{ignoreQuiet:true});
    assert.equal(send(n).length,0);
    const o=sql("SELECT status, reason FROM outbox WHERE event_key='late-permit'");
    assert.ok(o.some(x=>x.status==="skipped"),JSON.stringify(o));
  });

  await step("반복 실행 시 별도 소유자/봇의 기존 버튼은 유지",async()=>{
    assert.ok(E.calls.some(x=>x.method==="sendMessage"&&/골든게이트/.test(x.payload.text)&&x.payload.reply_markup?.inline_keyboard?.[0]?.length===2));
  });

  await step("미국 도착 전 올해 신청 추첨은 연례행사라도 owner에게 발송하지 않음",async()=>{
    // 사용자 체류 시작 2027년, 모집은 올해만 해당하는 날짜: 이 사례를 별도 로컬 D1에서 직접 타겟 확인한다.
    sql("UPDATE people SET available_from='2028-06-01' WHERE chat_id='111'");
    const n=E.calls.length;
    await admin("/admin/enqueue",{events:[{...first,event_key:"future-owner-restriction",start_date:"2027-12-05",registration_opens:"2027-11-01",deadline:"2027-11-15"}]});
    await admin("/admin/dispatch",{ignoreQuiet:true});
    assert.equal(send(n).length,0);
    assert.equal(sql("SELECT COUNT(*) AS n FROM outbox WHERE event_key='future-owner-restriction'")[0].n,0);
  });

  await step("다른 주립공원의 트리 신청은 잘못 합치지 않음",async()=>{
    sql("UPDATE people SET available_from='2026-01-01' WHERE chat_id='111'");
    const another={...first,event_key:"other-park",title:"Another State Park Christmas Tree Cutting Permit",title_ko:"다른 주립공원 크리스마스 트리 허가",
      location:"Another State Park",org:"Colorado Parks",url:"https://cpw.state.co.us/another-park"};
    const n=E.calls.length;
    await admin("/admin/enqueue",{events:[another]});
    await admin("/admin/dispatch",{ignoreQuiet:true});
    assert.ok(since(n).some(x=>x.method==="sendMessage"&&x.payload.text.includes("다른 주립공원")), "다른 공원까지 억제하지 않아야 함");
  });
} finally {E.stop();}
if(failed()) process.exitCode=1;
