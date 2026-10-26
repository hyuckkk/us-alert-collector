import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync,readdirSync } from 'node:fs';
import { pendingReminder,pendingPayload } from '../src/subscriptions.js';
import { sportsWeekly, isCalendarMatch, rowAsCalendar, sportsEvents } from '../src/sports.js';
import { collectGuard } from '../src/collect-guard.js';
const sqlite=new DatabaseSync(':memory:');for(const f of readdirSync('migrations').filter(f=>f.endsWith('.sql')).sort())sqlite.exec(readFileSync('migrations/'+f,'utf8'));
const DB={prepare(sql){
 const stmt=sqlite.prepare(sql);let args=[];
 return {bind(...a){args=a;return this},async first(){return stmt.get(...args)||null},async all(){return {results:stmt.all(...args)}},async run(){return {meta:{changes:stmt.run(...args).changes}}}};
}};
const env={DB,OWNER_CHAT_ID:'111',BOT_TOKEN_LIFE:'fake',BOT_TOKEN_FAMILY:'fake-family',SYSTEM_BOT:'life',OWNER_TZ:'Asia/Seoul',TG_API:'https://fake.invalid',DELIVERY_MODE:'owner-only'};
let calls=[],fail=false;globalThis.fetch=async(url,opts)=>{assert.ok(url.startsWith(env.TG_API));calls.push(JSON.parse(opts.body));return Response.json(fail?{ok:false,error_code:500}:{ok:true,result:{message_id:1}})};
const morning=new Date('2026-10-26T00:00:00Z');
assert.equal((await pendingReminder(env,morning)).sent,0);assert.equal(sqlite.prepare('SELECT count(*) n FROM notices').get().n,0);
for(let i=0;i<65;i++)sqlite.prepare("INSERT INTO recipients(chat_id,bot,name,status,joined_at,updated_at) VALUES (?,? ,?,'pending',?,?)").run(String(1000+i),['life','family','kids'][i%3],'🙂'.repeat(100),'2026-10-25T01:00:00Z','2026-10-25T01:00:00Z');
const payload=await pendingPayload(env);assert.equal(payload.total,65);assert.ok(payload.text.length<4096);assert.equal(payload.reply_markup.inline_keyboard.length,20);assert.ok(payload.reply_markup.inline_keyboard.flat().every(b=>Buffer.byteLength(b.callback_data)<64));
assert.equal((await pendingReminder(env,new Date('2026-10-25T23:59:59Z'))).sent,0);
const concurrent=await Promise.all([pendingReminder(env,morning),pendingReminder(env,morning)]);assert.equal(concurrent.reduce((n,r)=>n+r.sent,0),1);assert.equal(calls.length,1);
assert.equal((await pendingReminder(env,new Date('2026-10-26T10:00:00Z'))).sent,0);
fail=true;assert.equal((await pendingReminder(env,new Date('2026-10-27T00:00:00Z'))).status,'failed');assert.equal((await pendingReminder(env,new Date('2026-10-27T01:00:00Z'))).sent,0);assert.match(sqlite.prepare("SELECT v FROM kv WHERE k='pending_reminder_last'").get().v,/failed/);
sqlite.exec("UPDATE recipients SET status='active'");assert.equal((await pendingReminder(env,new Date('2026-10-28T00:00:00Z'))).sent,0);fail=false;
// Weekly link must respect Saturday, Denver DST, owner-only, and concurrent duplicate claims.
sqlite.prepare("INSERT INTO recipients(chat_id,bot,status,joined_at,updated_at) VALUES ('111','family','active','2026-01-01','2026-01-01')").run();
const event={title:'Gymnastics Invitational',sport:'gymnastics',school_level:'high',event_type:'meet',start_date:'2026-10-31',venue:'Arena'};
sqlite.prepare('INSERT INTO events(event_key,bot,content_hash,urgency,url,source_id,start_date,data,first_seen,last_changed,version) VALUES (?,?,?,?,?,?,?,?,?,?,?)').run('gym','family','fixture','weekly','https://example.org','athletics',event.start_date,JSON.stringify(event),'2026-10-01','2026-10-01',1);
assert.equal((await sportsWeekly(env,'https://example.org',new Date('2026-10-31T14:59:00Z'))).eligible_now,false);
calls=[];const results=await Promise.all([sportsWeekly(env,'https://example.org',new Date('2026-10-31T15:00:00Z')),sportsWeekly(env,'https://example.org',new Date('2026-10-31T15:00:00Z'))]);assert.equal(results.reduce((n,r)=>n+r.sent,0),1);assert.equal(calls.length,1);assert.equal(String(calls[0].chat_id),'111');
assert.equal((await sportsWeekly(env,'https://example.org',new Date('2026-11-07T15:59:00Z'))).eligible_now,false);assert.equal((await sportsWeekly(env,'https://example.org',new Date('2026-11-07T16:00:00Z'))).sent,0);
const official=JSON.parse(readFileSync('test/fixtures/gymnastics-official.json','utf8')).events;
for(const [i,e] of official.entries()) {
 assert.ok(isCalendarMatch(e));const r=rowAsCalendar(e,{event_key:'official'+i,source_id:e.source_id,url:e.url});assert.equal(r.sport,'체조');assert.equal(r.date,e.start_date);assert.equal(r.price,'unknown');assert.equal(r.location,e.venue);
 sqlite.prepare('INSERT INTO events(event_key,bot,content_hash,urgency,url,source_id,start_date,data,first_seen,last_changed) VALUES (?,?,?,?,?,?,?,?,?,?)').run('official'+i,'family','fixture','weekly',e.url,e.source_id,e.start_date,JSON.stringify(e),'2026-10-26','2026-10-26');
}
const march=await (await sportsEvents(env,new URL('https://example.org/sports/events?month=2026-03'))).json();assert.equal(march.events.length,1);assert.match(march.events[0].verification,/불일치/);
const setKV=(key,value)=>sqlite.prepare('INSERT INTO kv(k,v,updated_at) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v').run(key,value,'2026-10-26');
const now=Date.parse('2026-10-26T05:59:00Z');setKV('last_run_at',new Date(now-7*3600000).toISOString());setKV('last_collect_ok_at',new Date(now-7*3600000).toISOString());
assert.equal((await collectGuard(env,now)).action,'no-token');assert.equal((await collectGuard(env,now+120000)).action,'already-requested','6h bucket boundary must not trigger again');
const resetGuard=()=>{sqlite.exec("DELETE FROM notices WHERE key LIKE 'collect-recover:%'; DELETE FROM kv WHERE k='collect_recovery_attempt_at'")};
resetGuard();setKV('last_run_at','broken');assert.equal((await collectGuard(env,now)).action,'invalid-run-timestamp');setKV('last_run_at',new Date(now-1*3600000).toISOString());setKV('last_collect_ok_at',new Date(now-30*3600000).toISOString());
const gh={...env,GH_API:'https://github.test',GH_REPO:'hyuckkk/us-alert-collector',GH_DISPATCH_TOKEN:'test-only'};
const telegram=globalThis.fetch;let ghCount=0;globalThis.fetch=async(url,opts)=>{if(url.startsWith(gh.GH_API)){ghCount++;assert.deepEqual(JSON.parse(opts.body),{ref:'main',inputs:{mode:'full',dry_run:'false'}});return new Response(null,{status:204})}return telegram(url,opts)};
assert.equal((await collectGuard(gh,now)).action,'requested');assert.equal(ghCount,1);
resetGuard();globalThis.fetch=async(url,opts)=>url.startsWith(gh.GH_API)?new Response(null,{status:403}):telegram(url,opts);assert.equal((await collectGuard(gh,now)).action,'request-failed-403');
resetGuard();globalThis.fetch=async(url,opts)=>{if(url.startsWith(gh.GH_API))throw Error('network timeout');return telegram(url,opts)};assert.equal((await collectGuard(gh,now)).action,'request-failed-0');assert.match(sqlite.prepare("SELECT v FROM kv WHERE k='collect_recovery_last'").get().v,/request-failed-0/);
console.log('PASS: reminders, weekly DST/dedup, official gymnastics, recovery no-token/6h-boundary/full/403/network/invalid-time');sqlite.close();
