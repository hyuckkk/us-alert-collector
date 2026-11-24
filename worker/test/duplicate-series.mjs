// 같은 행사를 정보원이 회차(날짜)마다 따로 올려도 한 번만 보낸다 (2026-11-25 레드락스 크리스마스 인 컬러 10번 발송).
// 같은 장소의 서로 다른 공연은 따로 보낸다.
import assert from "node:assert/strict";
import { sameAsSent, baseTitle } from "../src/pipeline.js";

const fresh = () => ({ series: new Map(), urls: new Map(), dated: new Map(), groups: new Map(), places: new Map() });
const ev = (k, o) => ({ event_key: k, location: "Red Rocks Amphitheatre", ...o });

assert.equal(baseTitle("Christmas in Color (Dec 1 session)"), "christmas in color");
assert.equal(baseTitle("Christmas in Color - Nov 28"), "christmas in color");

const s = fresh();
const rr = [
  ev("a", { title: "Christmas in Color (Nov 27 session)", title_ko: "레드락스 크리스마스 인 컬러 개막", start_date: "2026-11-27", url: "https://www.redrocksonline.com/events/christmas-in-color-2/", src: "red-rocks" }),
  ev("b", { title: "Christmas in Color (Dec 1 session)", title_ko: "레드락스 크리스마스 인 컬러", start_date: "2026-12-01", url: "https://www.redrocksonline.com/events/christmas-in-color-3/", src: "red-rocks" }),
  ev("c", { title: "Christmas in Color Drive-Thru Light Show", title_ko: "레드락스 드라이브스루 크리스마스 조명쇼", start_date: "2026-12-02", url: "https://visitdenver.com/event/christmas-in-color/123/", src: "visitdenver" }),
  ev("d", { title: "Christmas in Color", title_ko: "레드락스 크리스마스 인 컬러 (차량 관람)", start_date: "2026-12-10", location: "Red Rocks Park", url: "https://christmasincolor.com/denver", src: "ext" }),
];
const res = rr.map((x) => sameAsSent(s, "owner", x.src, x, x.event_key));
assert.equal(res[0], null);
assert.ok(res.slice(1).every(Boolean), JSON.stringify(res));

// 같은 장소의 다른 공연은 막지 않는다
const s2 = fresh();
assert.equal(sameAsSent(s2, "owner", "red-rocks", ev("x", { title: "deadmau5 at Red Rocks", title_ko: "데드마우스 레드락스 공연", start_date: "2026-10-30", url: "https://www.redrocksonline.com/events/deadmau5/" }), "x"), null);
assert.equal(sameAsSent(s2, "owner", "red-rocks", ev("y", { title: "ATLiens at Red Rocks", title_ko: "ATLiens 레드락스 공연", start_date: "2026-10-31", url: "https://www.redrocksonline.com/events/atliens/" }), "y"), null);
// 다른 사람에게는 따로 보낸다
assert.equal(sameAsSent(s, "friend", "red-rocks", rr[1], "b"), null);
console.log("duplicate-series ok");
