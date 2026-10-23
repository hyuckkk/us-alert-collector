import { tg, claimNotice, isOwner } from "./common.js";

// Read-only public school/club sports calendar. Do NOT export people, chat ids, or admin data.
const ZONE = "America/Denver";
const SPORTS = /\b(hockey|soccer|football|baseball|basketball|volleyball|softball|tennis|wrestling|lacrosse|rugby|gymnastics|swimming|water polo|field hockey|cross.country|track.and.field|futsal|badminton)\b|농구|배구|야구|축구|아이스하키|하키|풋볼|미식축구|레슬링|테니스|수영|체조|육상/i;
const VS = /\b(?:vs\.?|v\.|versus|against)\b|(?:\s+@\s+)|\s+대\s+|대항전/i;
const ATHLETIC_SOURCE = /athlet|sport.?club|club.?sports|soccer|hockey|high.?school.?sport|school.?athlet|varsity|^cu-.*club/i;
const UNIVERSITY = /cu-|boulder|university.of.denver|du-athlet|csu-|colorado.state|mines-|air.force|afa-|university|college|sport.?club/i;
const GRADE = {
  "elementary": /elementary|초등|grade.?school|little.league|u[6-9]\b/i,
  "middle": /middle.?school|junior.?high|중학/i,
  "high": /high.?school|prep.?school|고등|varsity|jv\b/i
};
const SCHOOL_LABEL = { elementary: "초등", middle: "중등", high: "고등", university: "대학", unknown: "학교급 미확인" };
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
const CORS = { "Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=600", "Content-Type": "application/json; charset=utf-8", "X-Content-Type-Options": "nosniff" };
function monthShift(m, by) {
  const [y, n] = m.split("-").map(Number);
  const d = new Date(Date.UTC(y, n - 1 + by, 1));
  return d.toISOString().slice(0, 7);
}
function denverToday() {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: ZONE, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
  const get = (type) => parts.find((x) => x.type === type).value;
  return get("year") + "-" + get("month") + "-" + get("day");
}
function schoolGrade(event, source) {
  const hint = [event.school_level, event.grade_level, event.school_type, event.org, source, event.title].filter(Boolean).join(" ");
  if (/^(elementary|middle|high|university)$/.test(String(event.school_level || ""))) return event.school_level;
  for (const [level, re] of Object.entries(GRADE)) if (re.test(hint)) return level;
  if (UNIVERSITY.test(hint)) return "university";
  return "unknown";
}
function sportName(e) {
  const tags = Array.isArray(e.tags) ? e.tags.join(" ") : "";
  const hint = [e.title, e.title_ko, tags].filter(Boolean).join(" ");
  const m = hint.match(SPORTS);
  const word = m ? m[0].toLowerCase().replace(/[\s.-]+/g, " ") : "";
  const names = {
    hockey: "아이스하키", soccer: "축구", football: "미식축구", baseball: "야구",
    basketball: "농구", volleyball: "배구", softball: "소프트볼",
    tennis: "테니스", wrestling: "레슬링", lacrosse: "라크로스",
    rugby: "럭비", gymnastics: "체조", swimming: "수영",
    "water polo": "수구", "field hockey": "필드하키",
    "cross country": "크로스컨트리", "track and field": "육상",
    futsal: "풋살", badminton: "배드민턴"
  };
  return names[word] || (m ? m[0] : "종목 미확인");
}
function costClass(raw) {
  const s = String(raw || "").trim();
  if (!s || /별도 확인|미정|not (?:listed|available)|unknown|tbd/i.test(s)) return "unknown";
  if (/(^|[\s,(])(?:free|complimentary|no (?:admission|charge|fee)|무료)(?=[\s.,)]|$)/i.test(s) || /(^|\s)\$0(?:\.00)?(\s|$)/.test(s)) {
    if (!/(유료|paid|admission\s*\$\d+|\$[1-9]\d*)/i.test(s)) return "free";
  }
  if (/(유료|admission\s*\$[1-9]|\$\s*[1-9]\d*|ticket.{0,15}\$[1-9]|paid admission)/i.test(s)) return "paid";
  return "unknown";
}
function matchup(e) {
  const title = String(e.title || e.title_ko || "");
  const m = title.match(/^(.*?)\s+(?:vs\.?|v\.|versus|against)\s+(.*)$/i);
  if (m) return { home: m[1].trim(), away: m[2].trim() };
  const a = title.match(/^(.*?)\s+@\s+(.*?)$/i);
  if (a) return { home: a[2].trim(), away: a[1].trim() };
  return { home: e.home_team || "", away: e.away_team || "" };
}
export function isCalendarMatch(e, sourceOverride = "") {
  const src = String(sourceOverride || e.source_id || "");
  const title = [e.title, e.title_ko].filter(Boolean).join(" ");
  const tags = Array.isArray(e.tags) ? e.tags.join(" ") : "";
  const school = schoolGrade(e, src);
  return !!(e.start_date && /^\d{4}-\d{2}-\d{2}$/.test(e.start_date)
    && (ATHLETIC_SOURCE.test(src) || e.category === "sports" || e.event_type === "match" || school !== "unknown")
    && (SPORTS.test(title + " " + tags) || ATHLETIC_SOURCE.test(src))
    && (VS.test(title) || e.home_team && e.away_team || /(?:match|game|대항|경기)/i.test(title) && ATHLETIC_SOURCE.test(src)));
}
export function isRoutineSportsMatch(e) {
  if (!isCalendarMatch(e)) return false;
  const title = [e.title, e.title_ko].filter(Boolean).join(" ");
  // Specials still require an independent documented non-routine benefit. Most ordinary fixtures stay in calendar.
  const special = /championship|finals?\b|state.?tournament|district.?tournament|special.?access|open.?house|결승|전국대회|특별개방/i;
  return !special.test(title) && !e.sports_special_verified;
}
function rowAsCalendar(e, r) {
  const match = matchup(e);
  const src = r.source_id || e.source_id || "";
  return {
    id: r.event_key,
    date: e.start_date,
    end_date: e.end_date || e.start_date,
    grade: schoolGrade(e, src),
    sport: sportName(e),
    title: e.title || e.title_ko || "경기",
    home: match.home || "",
    away: match.away || "",
    time: e.time_text || "시간 미확인",
    location: e.location || "장소 미확인",
    city: e.city || "",
    cost: e.cost_text || "입장료 미확인",
    price: costClass(e.cost_text),
    url: /^https:\/\//.test(String(r.url || "")) ? r.url : "",
    org: e.org || "",
    source: src,
    verification: e.verified === false || e.year_basis === "assumed" ? "추가 확인 필요" : "수집된 공식 정보",
    special: !!e.sports_special_verified
  };
}
export async function sportsEvents(env, url) {
  const now = denverToday();
  const start = url.searchParams.get("month") || now.slice(0, 7);
  if (!MONTH_RE.test(start) || Number(start.slice(0, 4)) < 2026 || Number(start.slice(0, 4)) > 2030) {
    return new Response(JSON.stringify({ ok: false, error: "month must be YYYY-MM (2026–2030)" }), { status: 400, headers: CORS });
  }
  const end = monthShift(start, 2) + "-01";
  const rows = (await env.DB.prepare(
    "SELECT event_key, data, source_id, url, start_date, last_changed FROM events WHERE start_date >= ? AND start_date < ? ORDER BY start_date, event_key LIMIT 3500"
  ).bind(start + "-01", end).all()).results || [];
  const events = [], seen = new Set();
  for (const r of rows) {
    let e;
    try { e = JSON.parse(r.data || "{}"); } catch { continue; }
    if (!isCalendarMatch(e, r.source_id)) continue;
    if (e.registration_status === "cancelled" || e.registration_status === "postponed") continue;
    const rec = rowAsCalendar(e, r);
    const key = [rec.date, rec.title.toLowerCase(), rec.location.toLowerCase()].join("|");
    if (seen.has(key)) continue;
    seen.add(key);
    events.push(rec);
  }
  return new Response(JSON.stringify({ ok: true, month: start, through: monthShift(start, 1), generated_at: new Date().toISOString(), events, inspected: rows.length, latest_record_changed: rows.map((x) => x.last_changed).filter(Boolean).sort().pop() || null,
    limit_reached: rows.length >= 3500, coverage: "현재 수집된 공식 일정 중 날짜·학교·종목·경기로 식별되는 항목만 표시됩니다. 전체 초·중·고 경기 수집을 보장하지 않습니다." }), { headers: CORS });
}
const PAGE = String.raw`<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>Colorado | 학교·대학 스포츠 경기 달력</title><style>
:root{font-family:system-ui,-apple-system,Segoe UI,Apple SD Gothic Neo,sans-serif;color:#172234;background:#f4f6fa}*{box-sizing:border-box}body{margin:0}.wrap{max-width:1170px;margin:0 auto;padding:26px 18px 50px}
h1{font-size:27px;letter-spacing:-.9px;margin:5px 0}p{margin:5px 0;line-height:1.6}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:15px;margin-bottom:18px}
.eyebrow{font-size:12px;letter-spacing:1.5px;font-weight:800;color:#275899}.subtitle{color:#66738a;font-size:13px}.share{background:#fff;border:1px solid #d9e2ef;padding:10px 15px;border-radius:11px;font-size:13px;cursor:pointer}
.filters{background:white;border:1px solid #dce4ef;border-radius:16px;padding:16px;margin-bottom:15px;display:grid;grid-template-columns:1.6fr 1fr 1fr;gap:15px;box-shadow:0 4px 20px #163b6620}
fieldset{border:0;margin:0;padding:0;min-width:0}legend{font-size:12px;font-weight:750;color:#526078;margin-bottom:9px}.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{border:1px solid #dce4ee;background:#f8fafc;color:#3b4a62;padding:8px 11px;border-radius:9px;cursor:pointer;font-size:13px}.chip.selected{background:#e6f1fc;border-color:#3c82ce;color:#13538d;font-weight:800}select{width:100%;font:inherit;font-size:13px;background:#f8fafc;border:1px solid #dce4ee;padding:9px;border-radius:9px}
.toolbar{display:flex;justify-content:space-between;align-items:center;margin:15px 1px 10px;gap:10px}.toolbar strong{font-size:14px}.toolbar .muted{color:#738198;font-size:12px}.nav{display:flex;align-items:center;gap:7px}.nav button{border:1px solid #d7e0ed;background:#fff;border-radius:9px;padding:8px 10px;cursor:pointer}.nav span{font-weight:750;font-size:13px;white-space:nowrap}
.months{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}.month{background:#fff;border:1px solid #dce4ed;border-radius:16px;padding:18px;min-width:0;box-shadow:0 6px 20px #163b660e}.month h2{font-size:16px;margin:0 0 15px}.days{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}.day-head{color:#7a8799;font-weight:650;text-align:center;font-size:11px;padding:0 0 7px}.day-head:first-child{color:#df6565}.day-head:nth-child(7){color:#466fc3}
.day{border:1px solid transparent;background:#f6f8fb;border-radius:9px;min-height:75px;padding:6px;cursor:pointer;text-align:left;color:#364458;display:flex;flex-direction:column;gap:4px;overflow:hidden}.day:not(:disabled):hover{border-color:#96badb}.day.out{opacity:.3;cursor:default}.day.today{border-color:#a9d0f6}.day.chosen{border-color:#2c80c5;background:#ecf5ff}.day .num{font-size:12px;font-weight:750}.day .events{font-size:10px;color:#23639a;background:#dbeaf9;padding:4px;border-radius:5px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;max-width:100%}.day .more{font-size:10px;color:#6a7180}.day.noevents .num{color:#aab4c2}
.day-panel{background:#fff;border:1px solid #dce4ef;border-radius:16px;margin-top:15px;padding:17px}.day-panel h3{margin:0 0 11px;font-size:16px}.item{border:1px solid #e2e8f0;border-radius:11px;padding:13px;margin:8px 0;cursor:pointer;display:flex;gap:12px;justify-content:space-between}.item:hover{border-color:#94bfe5;background:#fafcff}.item strong{font-size:14px}.item p{font-size:12px;color:#647286;margin-top:5px}.tag{font-size:11px;color:#275b95;background:#e9f2fc;border-radius:6px;padding:3px 7px;display:inline-block;margin-right:5px}.free{color:#20764b;background:#e5f6ed}.paid{color:#aa6320;background:#fcf0e1}.unknown{color:#727a88;background:#eff1f4}
.notice{color:#697991;font-size:12px;line-height:1.65;margin-top:12px}.empty{color:#78879c;background:#f7f9fc;padding:25px;text-align:center;border-radius:10px;font-size:13px}
dialog{border:0;border-radius:18px;width:min(520px,94vw);padding:24px;box-shadow:0 24px 80px #142a4b55}dialog::backdrop{background:#15284690}dialog h3{margin:8px 0 18px;font-size:21px}dialog dl{display:grid;grid-template-columns:80px 1fr;gap:11px 10px;font-size:14px}dt{color:#718096}dd{margin:0;overflow-wrap:anywhere}.actions{display:flex;justify-content:flex-end;gap:9px;margin-top:22px}.actions button,.actions a{background:#eaf0f7;color:#254666;border:0;text-decoration:none;padding:10px 12px;border-radius:9px;font-weight:700;font-size:13px;cursor:pointer}.actions a{background:#246eac;color:white}
@media(max-width:730px){.top{flex-direction:column}.filters{grid-template-columns:1fr}.months{grid-template-columns:1fr}.month{padding:13px}.day{min-height:64px}.day .events{font-size:9px}.wrap{padding:18px 12px}}
</style></head><body><main class="wrap"><div class="top"><div><div class="eyebrow">COLORADO · COMMUNITY SPORTS</div><h1>학교·대학 스포츠 경기 달력</h1><p class="subtitle">초·중·고·대학 경기 | 이번 달·다음 달 | 미국 콜로라도 현지시간</p></div><button class="share" id="share">달력 링크 복사 ↗</button></div>
<section class="filters" aria-label="경기 필터"><fieldset><legend>학교급</legend><div class="chips" id="grades"></div></fieldset><fieldset><legend>종목</legend><select id="sports"><option value="all">모든 종목</option></select></fieldset><fieldset><legend>입장료</legend><div class="chips" id="prices"></div></fieldset></section>
<div class="toolbar"><div><strong id="status">경기 자료 불러오는 중…</strong><div class="muted" id="source-note"></div></div><div class="nav"><button id="prev" aria-label="이전 달">‹</button><span id="period"></span><button id="next" aria-label="다음 달">›</button><button id="reset">이번 달</button></div></div>
<div id="months" class="months"></div><section class="day-panel" id="details"><h3>날짜를 선택해 주세요</h3><p class="subtitle">달력의 날짜를 누르면 해당 경기의 팀·시각·장소를 볼 수 있습니다.</p></section>
<p class="notice">* 공개된 공식 일정 중 경기로 식별할 수 있는 자료만 보여줍니다. 수집되지 않은 학교·종목은 빈 날짜로 보일 수 있습니다. 무료/유료가 확인되지 않은 경기는 ‘요금 미확인’으로 구분합니다. 경기 시간·장소·입장료는 변경될 수 있으므로 방문 전에 공식 링크를 확인해 주세요.</p>
</main><dialog id="modal"><div id="modal-body"></div><div class="actions"><button id="close">닫기</button><a id="official" target="_blank" rel="noopener noreferrer" href="#">공식 일정 확인 ↗</a></div></dialog><script>
(function(){
"use strict";
var state={month:"",data:[],grade:"all",sport:"all",price:"all",selected:"",loading:false};
var grades=[["all","전체"],["elementary","초등"],["middle","중등"],["high","고등"],["university","대학"],["unknown","미분류"]];
var prices=[["all","전체"],["free","무료"],["paid","유료"],["unknown","요금 미확인"]];
var names=["일","월","화","수","목","금","토"];
function el(tag,cls,text){var x=document.createElement(tag);if(cls)x.className=cls;if(text!=null)x.textContent=String(text);return x}
function monthAdd(v,n){var a=v.split("-").map(Number);return new Date(Date.UTC(a[0],a[1]-1+n,1)).toISOString().slice(0,7)}
function currentMonth(){var p=new Intl.DateTimeFormat("en-US",{timeZone:"America/Denver",year:"numeric",month:"2-digit"}).formatToParts(new Date());return p.find(x=>x.type==="year").value+"-"+p.find(x=>x.type==="month").value}
function label(m){return m.replace("-","년 ")+"월"}
function dStr(y,m,d){return y+"-"+String(m).padStart(2,"0")+"-"+String(d).padStart(2,"0")}
function node(id){return document.getElementById(id)}
function setChips(id,options,key){var root=node(id);root.replaceChildren();options.forEach(function(a){var b=el("button","chip"+(state[key]===a[0]?" selected":""),a[1]);b.type="button";b.setAttribute("aria-pressed",state[key]===a[0]);b.onclick=function(){state[key]=a[0];setChips(id,options,key);render()};root.appendChild(b)})}
function filtered(){return state.data.filter(function(x){return (state.grade==="all"||x.grade===state.grade)&&(state.sport==="all"||x.sport===state.sport)&&(state.price==="all"||x.price===state.price)})}
function readDate(d){return new Date(d+"T12:00:00Z")}
function monthView(month,items){
 var a=month.split("-").map(Number),year=a[0],mon=a[1],first=new Date(Date.UTC(year,mon-1,1)).getUTCDay(),days=new Date(Date.UTC(year,mon,0)).getUTCDate();
 var section=el("section","month"),head=el("h2","",label(month));section.appendChild(head);var grid=el("div","days");names.forEach(function(n){grid.appendChild(el("div","day-head",n))});
 for(var i=0;i<Math.ceil((first+days)/7)*7;i++){
   var day=i-first+1,valid=day>=1&&day<=days,date=valid?dStr(year,mon,day):"";
   var xs=valid?items.filter(function(x){return x.date===date}):[];
   var b=el("button","day"+(valid?"":" out")+(date===state.selected?" chosen":"")+(xs.length?"":" noevents"));b.type="button";b.disabled=!valid;if(valid)b.setAttribute("aria-label",date+" "+xs.length+"경기");
   b.appendChild(el("span","num",valid?day:""));
   if(xs.length){b.appendChild(el("span","events",(xs[0].sport||"경기")+" · "+(xs[0].home||xs[0].title).slice(0,17)));if(xs.length>1)b.appendChild(el("span","more","+"+(xs.length-1)+"건"))}
   if(valid)b.onclick=function(){state.selected=date;render()};
   grid.appendChild(b);
 }
 section.appendChild(grid);return section
}
function paintList(items){var root=node("details");root.replaceChildren();var x=state.selected?items.filter(a=>a.date===state.selected):items.filter(a=>a.date>=state.month+"-01").slice(0,12);
 root.appendChild(el("h3","",state.selected?state.selected+" · "+x.length+"경기":"다가오는 경기 · "+items.length+"건"));
 if(!x.length){root.appendChild(el("div","empty",state.selected?"이 날짜에 등록된 경기 정보가 없습니다.":"현재 필터에 해당하는 경기 자료가 없습니다."));return}
 x.forEach(function(v){var card=el("div","item");card.tabIndex=0;card.setAttribute("role","button");
 var body=el("div");body.appendChild(el("span","tag",({elementary:"초등",middle:"중등",high:"고등",university:"대학",unknown:"미분류"})[v.grade]||"학교"));body.appendChild(el("span","tag",v.sport||"경기"));
 body.appendChild(el("strong","",v.title));body.appendChild(el("p","",v.date+" · "+v.time+" · "+v.location));card.appendChild(body);
 card.appendChild(el("span","tag "+v.price,({free:"무료",paid:"유료",unknown:"요금 미확인"})[v.price]));card.onclick=function(){show(v)};card.onkeydown=function(e){if(e.key==="Enter"||e.key===" "){e.preventDefault();show(v)}};root.appendChild(card)})}
function show(v){var body=node("modal-body");body.replaceChildren();body.appendChild(el("div","eyebrow",v.sport));body.appendChild(el("h3","",v.title));
 var dl=el("dl");[["대진",v.home&&v.away?v.home+" vs "+v.away:"공식 제목에서 상대팀 미확인"],["날짜",v.date],["시간",v.time],["장소",v.location],["학교급",({elementary:"초등",middle:"중등",high:"고등",university:"대학",unknown:"미분류"})[v.grade]],["입장료",v.cost],["주최",v.org||"미확인"],["검증",v.verification]].forEach(function(a){dl.appendChild(el("dt","",a[0]));dl.appendChild(el("dd","",a[1]))});body.appendChild(dl);
 var link=node("official");link.href=v.url||"#";link.hidden=!v.url;node("modal").showModal()}
function render(){
 var items=filtered().sort(function(a,b){return a.date.localeCompare(b.date)||a.time.localeCompare(b.time)});
 node("status").textContent=state.loading?"경기 일정 확인 중…":"현재 필터: "+items.length+"경기";
 node("period").textContent=label(state.month)+" — "+label(monthAdd(state.month,1));
 var root=node("months");root.replaceChildren();root.appendChild(monthView(state.month,items));root.appendChild(monthView(monthAdd(state.month,1),items));paintList(items)}
async function load(){state.loading=true;render();try{
 var res=await fetch("/sports/events?month="+encodeURIComponent(state.month),{cache:"no-store"});if(!res.ok)throw Error("HTTP "+res.status);
 var payload=await res.json();if(!payload.ok)throw Error(payload.error||"자료 조회 실패");
 state.data=payload.events||[];var sport=node("sports"),old=state.sport;var kinds=Array.from(new Set(state.data.map(a=>a.sport).filter(Boolean))).sort();sport.replaceChildren();sport.appendChild(new Option("모든 종목","all"));kinds.forEach(function(x){sport.appendChild(new Option(x,x))});if(kinds.indexOf(old)<0)state.sport="all";sport.value=state.sport;
 node("source-note").textContent=payload.limit_reached?"자료가 많아 일부만 표시됨 · 업데이트 "+payload.generated_at.slice(0,10):"수집된 경기 "+state.data.length+"건 · "+payload.generated_at.slice(0,10)+" 조회";
 }catch(e){state.data=[];node("source-note").textContent="자료 불러오기 실패: "+e.message+" · 새로고침 후 다시 시도해 주세요";}finally{state.loading=false;render()}}
node("sports").onchange=function(e){state.sport=e.target.value;render()};
node("prev").onclick=function(){state.month=monthAdd(state.month,-1);state.selected="";history.replaceState(null,"","?month="+state.month);load()};
node("next").onclick=function(){state.month=monthAdd(state.month,1);state.selected="";history.replaceState(null,"","?month="+state.month);load()};
node("reset").onclick=function(){state.month=currentMonth();state.selected="";history.replaceState(null,"",location.pathname);load()};
node("close").onclick=function(){node("modal").close()};
node("share").onclick=function(){if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(location.href).then(function(){node("share").textContent="복사됐습니다 ✓"})}else{node("share").textContent="주소창에서 링크를 복사해 주세요"}};
setChips("grades",grades,"grade");setChips("prices",prices,"price");
var urlMonth=new URLSearchParams(location.search).get("month");state.month=/^\d{4}-(0[1-9]|1[0-2])$/.test(urlMonth||"")?urlMonth:currentMonth();load();
})();
</script></body></html>`;
export function sportsPage() {
  return new Response(PAGE, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" } });
}
export async function sportsWeekly(env, origin) {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: ZONE, weekday: "long", hour: "2-digit", hour12: false }).formatToParts(new Date());
  const day = parts.find((x) => x.type === "weekday").value, hour = Number(parts.find((x) => x.type === "hour").value);
  if (day !== "Saturday" || hour < 9) return { checked: true, eligible_now: false };
  const month = denverToday().slice(0, 7);
  const page = new URL(origin + "/sports/events?month=" + month);
  const result = await sportsEvents(env, page), { events } = await result.json();
  if (!events || !events.length) return { checked: true, eligible_now: true, sent: 0, reason: "no scheduled verified matches" };
  const all = (await env.DB.prepare("SELECT r.chat_id, p.available_from FROM recipients r LEFT JOIN people p ON p.chat_id = r.chat_id WHERE r.bot = 'family' AND r.status = 'active'").all()).results || [];
  let sent = 0, failed = 0, skipped = 0;
  const stamp = denverToday();
  for (const p of all) {
    if (env.DELIVERY_MODE === "owner-only" && !isOwner(env, p.chat_id)) { skipped++; continue; }
    if (p.available_from && p.available_from > monthShift(month,2)+"-01") { skipped++; continue; }
    if (!(await claimNotice(env, "sports-calendar-weekly:" + stamp + ":" + p.chat_id))) { skipped++; continue; }
    const text = "🏒⚽ 학교·대학 스포츠 정기경기 달력\n\n이번 달·다음 달 경기 " + events.length + "건이 등록돼 있습니다.\n초·중·고·대학, 종목, 무료·유료를 골라 확인하세요.\n\n📅 경기 달력: " + origin + "/sports\n\n일반 정기경기는 개별 알림으로 보내지 않습니다.";
    const response = await tg(env, "family", "sendMessage", { chat_id: p.chat_id, text, disable_web_page_preview: true });
    if (response.ok) sent++; else failed++;
  }
  return { checked: true, eligible_now: true, sent, failed, skipped, events: events.length };
}
