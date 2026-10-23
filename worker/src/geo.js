// 지인별 거리 판정: /city 로 정한 기준 도시에서 너무 먼 일반 행사는 보내지 않는다.
// 정확한 주소는 저장하지 않고 도시 중심 좌표(공개 지명 좌표, 소수 둘째 자리)만 쓴다.
// 국립공원 허가·추첨처럼 드물고 멀어도 갈 만한 것은 거리와 상관없이 보낸다(근거가 확인된 희귀 기회만).

// 콜로라도 주요 도시·마을(덴버 광역권, 프런트 레인지, 산간 관광지) 중심 좌표 [위도, 경도]
export const CITIES = {
  "denver": [39.74, -104.99], "westminster": [39.84, -105.04], "thornton": [39.87, -104.97], "northglenn": [39.89, -104.99],
  "federal heights": [39.85, -105.02], "broomfield": [39.92, -105.09], "arvada": [39.80, -105.09], "wheat ridge": [39.77, -105.08],
  "lakewood": [39.70, -105.08], "golden": [39.76, -105.22], "aurora": [39.73, -104.83], "commerce city": [39.81, -104.93],
  "brighton": [39.99, -104.82], "henderson": [39.92, -104.87], "lafayette": [39.99, -105.09], "louisville": [39.98, -105.13],
  "superior": [39.95, -105.17], "erie": [40.05, -105.05], "boulder": [40.01, -105.27], "longmont": [40.17, -105.10],
  "firestone": [40.11, -104.94], "frederick": [40.10, -104.94], "dacono": [40.08, -104.94], "berthoud": [40.31, -105.08],
  "loveland": [40.40, -105.07], "fort collins": [40.59, -105.08], "greeley": [40.42, -104.71], "windsor": [40.48, -104.90],
  "estes park": [40.38, -105.52], "nederland": [39.96, -105.51], "lyons": [40.22, -105.27], "englewood": [39.65, -104.99],
  "littleton": [39.61, -105.02], "centennial": [39.58, -104.88], "greenwood village": [39.62, -104.95], "lone tree": [39.55, -104.89],
  "parker": [39.52, -104.76], "castle rock": [39.37, -104.86], "highlands ranch": [39.55, -104.97], "morrison": [39.65, -105.19],
  "evergreen": [39.63, -105.32], "idaho springs": [39.74, -105.51], "georgetown": [39.71, -105.70], "black hawk": [39.80, -105.49],
  "colorado springs": [38.83, -104.82], "manitou springs": [38.86, -104.92], "monument": [39.09, -104.87], "pueblo": [38.25, -104.61],
  "breckenridge": [39.48, -106.04], "frisco": [39.57, -106.10], "keystone": [39.61, -105.94], "vail": [39.64, -106.37],
  "winter park": [39.89, -105.76], "grand lake": [40.25, -105.82], "steamboat springs": [40.48, -106.83], "aspen": [39.19, -106.82],
  "glenwood springs": [39.55, -107.32], "grand junction": [39.06, -108.55], "durango": [37.28, -107.88], "salida": [38.53, -105.99],
  "buena vista": [38.84, -106.13], "canon city": [38.44, -105.24], "mosca": [37.65, -105.87], "alamosa": [37.47, -105.87],
  "usafa": [38.99, -104.86], "air force academy": [38.99, -104.86],
};

// 같은 뜻의 다른 이름
const ALIAS = { "fort collins": ["ft collins", "ft. collins"], "canon city": ["cañon city"], "usafa": ["u.s. air force academy", "us air force academy"],
  "highlands ranch": ["highlands"], "colorado springs": ["colo springs"] };

export function normCity(name) {
  const n = String(name || "").toLowerCase().replace(/,\s*(co|colorado)\b.*$/, "").replace(/\s+/g, " ").trim();
  if (CITIES[n]) return n;
  for (const [k, list] of Object.entries(ALIAS)) if (list.includes(n)) return k;
  return null;
}

// 행사 위치 글자(도시 칸이 비어도 장소·주소에 도시 이름이 들어 있는 경우가 많다)에서 도시를 찾는다
export function eventCity(d) {
  const direct = normCity(d.city);
  if (direct) return direct;
  const text = ` ${String(d.location || "").toLowerCase()} `;
  let best = null;
  for (const k of Object.keys(CITIES)) if (text.includes(` ${k}`) || text.includes(`${k},`)) if (!best || k.length > best.length) best = k;
  return best;
}

export function miles(a, b) {
  const [la1, lo1] = CITIES[a], [la2, lo2] = CITIES[b];
  const r = Math.PI / 180;
  const x = Math.sin(((la2 - la1) * r) / 2) ** 2 + Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin(((lo2 - lo1) * r) / 2) ** 2;
  return 3958.8 * 2 * Math.asin(Math.sqrt(x));
}

// 거리와 상관없이 보낼 행사(이슈 #22: '예약'·'축제'라는 글자만으로는 예외가 아니다)
// 국립·주립공원 허가·추첨·시간지정 입장, 그리고 추천 판정이 원문 근거로 확인한 드문 기회(비공개 시설 개방·한정 정원·희귀 행사)만
const SPECIAL_TAGS = new Set(["permit", "lottery", "timed-entry", "national-park", "state-park"]);
const SPECIAL_TEXT = /허가|추첨|시간\s*지정|국립공원|주립공원|permit|lottery|timed[- ]entry|national park|state park/i;
const RARE_BASIS = new Set(["restricted_access", "scarce_quota_or_lottery", "rare_one_off"]);
export function distanceExempt(d) {
  if (d.rec_level === "push" && RARE_BASIS.has(d.special_basis)) return true;
  if ((d.tags || []).some((t) => SPECIAL_TAGS.has(String(t).toLowerCase()))) return true;
  return SPECIAL_TEXT.test(`${d.title || ""} ${d.title_ko || ""}`) || d.signup === "lottery";
}

// 반환: null(보냄) 또는 "too-far"
export function tooFar(person, d) {
  const home = normCity(person.city);
  const radius = Number(person.radius_mi) || 0;
  if (!home || !radius) return null;          // 기준 도시·반경을 정하지 않았으면 거르지 않는다
  const c = eventCity(d);
  if (!c) return null;                         // 행사 도시를 모르면 거르지 않는다(놓치는 쪽보다 받는 쪽)
  if (distanceExempt(d)) return null;
  return miles(home, c) > radius ? "too-far" : null;
}
