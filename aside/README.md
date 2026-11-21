# Aside 받은편지함 (미국)

맥북의 Aside(실제 브라우저 에이전트)가 서버(GitHub·Cloudflare)에서 403·429·시간 초과로 막히는 공식 페이지를
하루 1회 대신 읽어 온다. 무엇을 읽을지는 `aside/targets.json`, 결과는 아래 형식으로 클라우드 예약작업이 커밋한다.

## 읽을 대상: `aside/targets.json`
```json
{"version": 1,
 "pages":    [{"id": "littleton-museum-events", "url": "https://…", "want": "무엇을 찾아 올지"}],
 "searches": [{"id": "…", "site": "…", "query": "…", "want": "…"}]}
```

## 결과 파일: `external-inbox/aside/<YYYYMMDDTHHMMZ>-<짧은이름>.json`
- 파일 이름은 영문·숫자·`._-` 만, 100자 이하. **새 파일로만 추가**한다(같은 이름 덮어쓰기는 처리되지 않음).
- main 에 push 하면 `external-inbox` 워크플로가 받아 처리한다(발견 경로 `aside`).
- 형식은 다른 외부 검색(`external-inbox/claude/`)과 같고, 행사마다 `evidence`·`source_url` 을 더한다.

```json
{
  "schema": "us-alert-inbox/1",
  "run": {"started_at": "2026-11-22T15:00:00Z", "finished_at": "2026-11-22T15:20:00Z",
          "origin": "aside", "pages_read": 15, "pages_failed": 1, "notes": "막혀서 못 읽은 페이지와 이유"},
  "events": [
    {
      "title": "Candlelight Walk",
      "url": "https://visitlittleton.org/events-and-programs/candlelight-walk/",
      "start_date": "2026-12-04", "end_date": null, "time": "6:00 PM",
      "location": "Main Street, Littleton, CO", "cost": "Free", "age": "All ages",
      "description": "공식 페이지에 적힌 내용 요약(영문 그대로 가능)",
      "evidence": "공식 페이지 원문 한 줄(500자 이하) — 날짜·시간이 들어간 문장을 그대로 복사",
      "source_url": "https://visitlittleton.org/events-and-programs/candlelight-walk/",
      "found_at": "2026-11-22T15:10:00Z",
      "verified": true,
      "recommend": "push | interest | none"
    }
  ]
}
```

## 처리 규칙
- `url`·`source_url` 은 https 공식 주소. `evidence` 는 Aside 가 그 페이지에서 **직접 본 원문**(500자 이하)이어야 한다.
- 수집기는 공식 페이지를 다시 읽어 확인한다. 재확인이 **403·429·시간 초과(막힘)** 로만 실패하고 `evidence`·`source_url` 이 있으면
  그 원문을 근거로 통과시킨다(Aside 만 해당). 404·내용 없음·원문 없음은 통과하지 않는다.
- 한 파일 최대 30건. 개인 정보(가족 구성·주소 등)는 넣지 않는다.
