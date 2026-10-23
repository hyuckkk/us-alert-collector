-- 이슈 #20: 같은 행사를 어느 경로가 언제 찾았는지(GitHub 수집기·Claude 예약 검색·ChatGPT 예약 검색) 경로별로 따로 기록한다.
-- 발송은 행사 단위로 한 번뿐이므로 실제 발송 여부는 outbox 에서 계산하고, 여기에는 경로별 최초 발견·검증·추천만 둔다.
CREATE TABLE event_finds (
  event_key   TEXT NOT NULL,
  finder      TEXT NOT NULL,          -- github_collector | claude | chatgpt
  first_seen  TEXT NOT NULL,          -- 이 경로가 처음 등록한 시각(Worker 기준)
  found_at    TEXT,                   -- 이 경로가 스스로 밝힌 발견 시각(외부 경로)
  last_seen   TEXT NOT NULL,
  url         TEXT,
  verified    INTEGER,                -- 1: 공식 페이지 재확인 통과, 0: 못 함
  rec_level   TEXT,                   -- 이 경로 입력을 우리 추천 판정이 본 결과
  ext_level   TEXT,                   -- 외부 경로가 스스로 매긴 추천(push|interest|none|unknown)
  run_id      TEXT,
  PRIMARY KEY (event_key, finder)
);
CREATE INDEX event_finds_finder ON event_finds (finder, first_seen);

-- 실행(경로)별 작업량·비용. data 는 JSON. 모르는 값은 "unknown".
CREATE TABLE workload (
  run_id     TEXT PRIMARY KEY,
  path       TEXT NOT NULL,           -- github_collector | claude | chatgpt
  started_at TEXT NOT NULL,
  data       TEXT NOT NULL
);
CREATE INDEX workload_path ON workload (path, started_at);

-- 외부 입력 파일(external-inbox/<finder>/<이름>.json) 처리 기록: 같은 파일·같은 내용은 다시 처리하지 않는다.
CREATE TABLE inbox_files (
  path       TEXT PRIMARY KEY,
  sha        TEXT NOT NULL,
  finder     TEXT NOT NULL,
  status     TEXT NOT NULL,           -- processing | done | failed
  at         TEXT NOT NULL,
  report     TEXT
);

-- 누락 원인 분류(이슈 #20 원인 코드)·발견 경로·재발견 확인
ALTER TABLE ext_gaps ADD COLUMN finder TEXT;
ALTER TABLE ext_gaps ADD COLUMN cause_code TEXT;
ALTER TABLE ext_gaps ADD COLUMN rediscovered INTEGER;   -- 보완(정보원·검색어) 후 같은 행사가 다시 잡히는지: 1 예, 0 아니오, NULL 확인 못 함

-- 기존 기록 이전: events.source_id 는 처음 등록한 경로(이후 갱신하지 않음). 외부 입력(ext-search)은 지금까지 ChatGPT 경로뿐이었다.
INSERT INTO event_finds (event_key, finder, first_seen, last_seen, url, verified, rec_level)
  SELECT event_key, CASE WHEN source_id = 'ext-search' THEN 'chatgpt' ELSE 'github_collector' END, first_seen, last_changed, url, 1, json_extract(data, '$.rec_level')
  FROM events WHERE true ON CONFLICT(event_key, finder) DO NOTHING;
-- 외부 대조에서 '우리도 찾음'·'수집 후 처리 대기'였던 행사는 ChatGPT 도 찾은 것이다
INSERT INTO event_finds (event_key, finder, first_seen, last_seen, url, verified)
  SELECT event_key, 'chatgpt', at, at, url, 1 FROM ext_gaps WHERE true ON CONFLICT(event_key, finder) DO NOTHING;
UPDATE ext_gaps SET finder = 'chatgpt' WHERE finder IS NULL;
UPDATE ext_gaps SET cause_code = CASE found WHEN 'queued' THEN 'freshness_or_schedule' WHEN 'unregistered' THEN 'missing_source' ELSE 'uncertain' END WHERE cause_code IS NULL AND found <> 'found';
