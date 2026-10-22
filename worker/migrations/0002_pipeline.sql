-- 1단계: 수집·판정 결과를 받아 사람별로 나눠 보내는 데 필요한 표

-- 사람별 설정 (봇 구독 여부는 recipients, 개인 설정은 여기)
CREATE TABLE people (
  chat_id        TEXT PRIMARY KEY,
  role           TEXT NOT NULL DEFAULT 'friend',   -- owner | friend
  tz             TEXT NOT NULL DEFAULT 'America/Denver',
  available_from TEXT,                             -- 이 날짜 이후 행사만 (가족: 입국일). NULL = 지금부터
  quiet_start    INTEGER NOT NULL DEFAULT 22,      -- 알림 정지 시작 시(현지)
  quiet_end      INTEGER NOT NULL DEFAULT 7,       -- 알림 정지 끝 시(현지)
  city           TEXT,                             -- 이동시간 기준 도시(정확한 주소는 저장하지 않음)
  updated_at     TEXT NOT NULL
);

-- 판정된 행사 1건 = 1행. data 는 수집기가 보낸 JSON(표시용 필드), 나머지는 거르기용 열.
CREATE TABLE events (
  event_key    TEXT PRIMARY KEY,
  bot          TEXT NOT NULL,
  version      INTEGER NOT NULL DEFAULT 1,
  content_hash TEXT NOT NULL,                      -- 중요 필드(날짜·장소·비용·신청·마감·신분 메모)만으로 계산
  data         TEXT NOT NULL,
  urgency      TEXT NOT NULL,                      -- now | weekly
  start_date   TEXT,
  end_date     TEXT,
  annual       INTEGER NOT NULL DEFAULT 0,
  tags         TEXT NOT NULL DEFAULT '[]',
  source_id    TEXT,
  url          TEXT,
  first_seen   TEXT NOT NULL,
  last_changed TEXT NOT NULL,
  change_note  TEXT
);

-- 사람별 발송 대기열. 같은 사람에게 같은 행사의 같은 판(version)은 한 번만.
CREATE TABLE outbox (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id    TEXT NOT NULL,
  bot        TEXT NOT NULL,
  event_key  TEXT NOT NULL,
  version    INTEGER NOT NULL,
  kind       TEXT NOT NULL,                        -- now | digest | season
  status     TEXT NOT NULL DEFAULT 'pending',      -- pending | sent | skipped | failed
  reason     TEXT,
  created_at TEXT NOT NULL,
  sent_at    TEXT,
  message_id INTEGER,
  UNIQUE (chat_id, bot, event_key, version)
);
CREATE INDEX outbox_pending ON outbox (status, kind);

-- 수집기가 이미 본 항목(주소+내용 해시). 같은 글을 AI 에 두 번 넣지 않기 위함.
CREATE TABLE seen (
  key        TEXT PRIMARY KEY,
  hash       TEXT NOT NULL,
  source_id  TEXT,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL
);

-- 실행 기록(측정 항목)
CREATE TABLE runs (
  run_id      TEXT PRIMARY KEY,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  data        TEXT NOT NULL
);

-- 월별 AI 사용량(모델별)
CREATE TABLE usage (
  month         TEXT NOT NULL,
  model         TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  usd           REAL NOT NULL DEFAULT 0,
  calls         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (month, model)
);

-- 신규 정보원 후보(B 발굴) → 2회 이상 유용하면 소유자에게 승격 여부를 묻는다
CREATE TABLE candidates (
  id         TEXT PRIMARY KEY,                     -- 주소 해시 12자
  url        TEXT NOT NULL,
  name       TEXT,
  reason     TEXT,
  bots       TEXT NOT NULL DEFAULT '[]',
  hits       INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',      -- pending | promoted | ignored
  notified   INTEGER NOT NULL DEFAULT 0
);

-- 승격된 정보원(수집기가 config/sources.json 과 함께 읽는다)
CREATE TABLE sources_extra (
  id       TEXT PRIMARY KEY,
  url      TEXT NOT NULL,
  name     TEXT,
  type     TEXT NOT NULL DEFAULT 'html',
  bots     TEXT NOT NULL DEFAULT '[]',
  added_at TEXT NOT NULL
);
