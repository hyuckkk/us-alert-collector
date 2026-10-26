import assert from 'node:assert/strict';
import { startEnv, stepper, OWNER } from './harness.mjs';
import { pendingReminder } from '../src/subscriptions.js';
import { isCalendarMatch, isRoutineSportsMatch, rowAsCalendar, sportsWeekly } from '../src/sports.js';
import { travelBucket, venueTravel, TRAVEL_ORIGIN } from '../src/sports-travel.js';
const {step,failed}=stepper();
const E=await startEnv({tgPort:10017,wPort:8827});
const state=(bot,id)=>E.sql(`SELECT status FROM recipients WHERE bot='${bot}' AND chat_id='${id}'`)[0]?.status;
try {
await step('Three bots; owner system callback approves only kids; duplicate/concurrent clicks are idempotent',async()=>{
 for(const bot of ['life','family','kids'])await E.hook(bot,E.msg('222','/start'));
 const message=E.calls.find(c=>c.method==='sendMessage'&&c.payload.reply_markup?.inline_keyboard[0][0].callback_data==='suba:kids:222');assert.ok(message);
 await Promise.all([E.hook('life',E.cb(OWNER,'suba:kids:222',42,message.payload.reply_markup)),E.hook('life',E.cb(OWNER,'suba:kids:222',42,message.payload.reply_markup))]);
 assert.equal(state('kids','222'),'active');assert.equal(state('family','222'),'pending');assert.equal(state('life','222'),'pending');
 assert.equal(E.calls.filter(c=>c.method==='sendMessage'&&String(c.payload.chat_id)==='222'&&/승인되었습니다/.test(c.payload.text)).length,0,'owner-only preserved');
});
await step('Reject is durable; /start and legacy /approve cannot bypass; pending stop/start stays pending',async()=>{
 await E.hook('life',E.cb(OWNER,'subr:family:222',42));await E.hook('family',E.msg('222','/start'));await E.hook('family',E.msg(OWNER,'/approve 222'));assert.equal(state('family','222'),'blocked');
 await E.hook('life',E.msg('222','/stop'));await E.hook('life',E.msg('222','/start'));assert.equal(state('life','222'),'pending');
});
await step('Nonowner, wrong chat, wrong bot, malformed target cannot approve; pending list uses system bot',async()=>{
 await E.hook('life',E.cb('222','suba:life:222',42));await E.hook('kids',E.cb(OWNER,'suba:life:222',42));const bad=E.cb(OWNER,'suba:life:222',42);bad.callback_query.message.chat.id=333;await E.hook('life',bad);await E.hook('life',E.cb(OWNER,'suba:bogus:222',42));assert.equal(state('life','222'),'pending');
 await E.hook('kids',E.msg(OWNER,'/pending'));const last=E.calls.filter(c=>c.method==='sendMessage').at(-1);assert.equal(last.token,'tLIFE');assert.match(last.payload.text,/승인 대기/);assert.ok(last.payload.reply_markup.inline_keyboard.length);
});
await step('Gymnastics structured and legacy meets pass; clinics and registration do not; named finals alone do not push',async()=>{
 for(const type of ['match','meet','tournament'])assert.ok(isCalendarMatch({start_date:'2026-10-10',school_level:'high',sport:'gymnastics',event_type:type,title:'Mountain Range Invitational'}));
 assert.ok(isCalendarMatch({start_date:'2026-10-10',school_level:'high',title:'State Gymnastics Championship'}));
 for(const title of ['Gymnastics clinic','Gymnastics season tickets','Gymnastics registration'])assert.equal(isCalendarMatch({start_date:'2026-10-10',school_level:'high',title}),false);
 assert.equal(isRoutineSportsMatch({start_date:'2026-10-10',school_level:'high',title:'State Gymnastics Championship'}),true);
 const row=rowAsCalendar({start_date:'2026-10-10',school_level:'high',sport:'gymnastics',event_type:'meet',title:'Quad Meet',participants:['A','B','C'],venue:'Actual venue',location:'School campus',start_time:'17:30',cost_text:'registration fee $10'},{event_key:'x',source_id:'chsaa-athletics',url:'https://example.org'});
 assert.equal(row.sport,'체조');assert.equal(row.home,'');assert.equal(row.away,'');assert.equal(row.location,'Actual venue');assert.equal(row.time,'17:30');assert.equal(row.price,'unknown');assert.equal(row.participants.length,3);
});
await step('Road provenance and actual venue required; precise travel boundaries',async()=>{
 assert.deepEqual([null,0,30,30.1,60,60.1,90,90.1].map(travelBucket),['unknown','0-30','0-30','30-60','30-60','60-90','60-90','90+']);
 const e={venue:'Magness Arena',city:'Denver',venue_route:{origin:TRAVEL_ORIGIN,mode:'driving',venue:'Magness Arena',city:'Denver',minutes:40,km:38,source_url:'https://example.org/route',checked_at:'2026-10-25T00:00:00Z'}};
 assert.equal(venueTravel(e).drive_bucket,'30-60');assert.equal(venueTravel({...e,venue:'Cadet West Gym'}).drive_bucket,'unknown');assert.equal(venueTravel({...e,venue_route:{...e.venue_route,mode:'straight-line'}}).drive_bucket,'unknown');assert.equal(venueTravel({location:'unknown',lat:39,lng:-105}).drive_minutes,null);
});
await step('Public sports API renders all levels and meet participants, retains distant/unknown records, no credentials',async()=>{
 const events=['elementary','middle','high','university'].map((school_level,i)=>({event_key:'sport'+i,title:'Gymnastics Invitational '+i,event_type:'meet',sport:'gymnastics',school_level,level_detail:i===0?'youth_league':'school_team',participants:['A','B','C'],start_date:'2026-10-29',source_id:'test-athletics',url:'https://example.org/'+i,venue:'Arena '+i,city:'Denver',rec_level:'none',bot:'family'}));
 await E.admin('/admin/enqueue',{events});const r=await (await E.get('/sports/events?month=2026-10')).json();assert.equal(r.events.filter(e=>e.id.startsWith('sport')).length,4);assert.equal(r.counts.grade.elementary,1);assert.equal(r.events[0].drive_bucket,'unknown');assert.ok(!JSON.stringify(r).includes('chat_id'));const html=await (await E.get('/sports')).text();assert.ok(html.includes('id="travel"'));assert.ok(!html.includes('function demo('));assert.ok(html.includes('체조'));
 assert.equal((await E.get('/sports/events?month=2025-01')).status,400);
});
await step('Ten ordinary games create no individual push; five verified special games retain exception',async()=>{
 const regular=Array.from({length:10},(_,i)=>({event_key:'regular'+i,title:'School A vs School B basketball',start_date:'2027-07-01',school_level:'high',event_type:'match',sport:'basketball',source_id:'test-athletics',url:'https://example.org/regular'+i,bot:'family',rec_level:'push',special_reason:'fixture',registration_status:'open',urgency:'now'}));
 await E.admin('/admin/enqueue',{events:regular});assert.equal(E.sql("SELECT COUNT(*) n FROM outbox WHERE event_key LIKE 'regular%'")[0].n,0);
 for(let i=0;i<5;i++)assert.equal(isRoutineSportsMatch({...regular[i],sports_special_verified:true}),false);
});
} finally {E.stop()}
if(failed())process.exit(1);
