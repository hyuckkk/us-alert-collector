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
