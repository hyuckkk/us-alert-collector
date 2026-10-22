-- 외부 검색(다른 AI)이 찾은 행사를 우리 수집이 놓쳤는지 기록하고(원인·조치), 그 결과로 자동 추가한 정보원·검색어를 둔다.
CREATE TABLE ext_gaps (
  event_key  TEXT PRIMARY KEY,
  url        TEXT NOT NULL,
  title      TEXT,
  host       TEXT,
  found      TEXT NOT NULL,        -- found(우리도 찾음) | queued(수집했지만 아직 미처리) | registered-missed(등록 정보원인데 놓침) | unregistered(정보원 없음)
  cause      TEXT,                 -- 원인(한국어 한 문장)
  action     TEXT,                 -- 자동 조치(정보원 추가·검색어 추가)
  at         TEXT NOT NULL
);
ALTER TABLE sources_extra ADD COLUMN mode TEXT;
ALTER TABLE sources_extra ADD COLUMN reason TEXT;
CREATE TABLE queries_extra (
  query      TEXT PRIMARY KEY,
  bot        TEXT NOT NULL DEFAULT 'any',
  reason     TEXT,
  added_at   TEXT NOT NULL
);
