// 11-27 사고 재현: 같은 날 같은 '콜로라도 주립공원 무료입장'이 정보원마다 다른 한글 제목으로 3통 나감 → 1통만
import { sameAsSent } from "../src/pipeline.js";
const seen = () => ({ series: new Map(), urls: new Map(), dated: new Map(), groups: new Map(), places: new Map() });
const s = seen();
const base = { start_date: "2026-11-27", location: "Colorado state parks" };
const a = { ...base, title: "Free State Park Entry on Fresh Air Friday", title_ko: "프레시 에어 프라이데이", url: "https://cpw.state.co.us/fresh-air-friday" };
const b = { ...base, title: "Colorado Parks and Wildlife Fresh Air Friday — FREE Entry to All State Parks", title_ko: "블랙프라이데이 주립공원 나들이", url: "https://example.org/a" };
const c = { ...base, title: "Fresh Air Friday free entry Colorado state parks", title_ko: "콜로라도 주립공원 나들이", url: "https://example.org/b" };
const r = [sameAsSent(s, "1", "cpw", a, "k1"), sameAsSent(s, "1", "chatgpt", b, "k2"), sameAsSent(s, "1", "aside", c, "k3")];
if (r[0] !== null || !r[1] || !r[2]) { console.error("FAIL", r); process.exit(1); }
// 같은 장소 같은 날이라도 다른 공연은 따로
const s2 = seen();
const x = sameAsSent(s2, "1", "rr", { start_date: "2026-11-27", location: "Red Rocks Amphitheatre", title: "Film on the Rocks: Elf", title_ko: "레드락스 영화 엘프" }, "r1");
const y = sameAsSent(s2, "1", "rr", { start_date: "2026-11-27", location: "Red Rocks Amphitheatre", title: "Trans-Siberian Orchestra Winter Tour", title_ko: "트랜스시베리안 오케스트라 겨울 공연" }, "r2");
if (x !== null || y !== null) { console.error("FAIL different shows", x, y); process.exit(1); }
console.log("ok duplicate-statepark", r);
// 11-24 테보 기차: 같은 날, 제목 거의 같음, 장소를 다르게 적음 → 1통
{ const s3 = seen();
  const a = sameAsSent(s3, "1", "ext-search", { start_date: "2026-11-29", location: "Pearl Street, downtown Boulder", title: "Tebo Train's Snowflake Express", title_ko: "펄스트리트 스노우플레이크 익스프레스 기차", url: "https://boulderdowntown.com/events/light-up-the-holidays" }, "t1");
  const b = sameAsSent(s3, "1", "ext-search", { start_date: "2026-11-29", location: "1242 Pearl St (in front of Wells Fargo), Boulder", title: "Tebo Train's Snowflake Express (Pearl Street holiday kids train)", title_ko: "볼더 펄 스트리트 연말 꼬마열차", url: "https://boulderdowntown.com/do/tebo-trains-snowflake-express" }, "t2");
  if (a !== null || !b) { console.error("FAIL tebo", a, b); process.exit(1); }
  // 같은 날 같은 이름 행사라도 다른 주립공원은 따로
  const c = sameAsSent(s3, "1", "x", { start_date: "2026-12-06", location: "Golden Gate Canyon State Park", title: "Christmas Tree Cutting Day", title_ko: "크리스마스 트리 벌목" }, "p1");
  const e = sameAsSent(s3, "1", "y", { start_date: "2026-12-06", location: "Staunton State Park", title: "Christmas Tree Cutting Day", title_ko: "크리스마스 트리 벌목" }, "p2");
  if (c !== null || e !== null) { console.error("FAIL parks", c, e); process.exit(1); }
  console.log("ok tebo·parks"); }
