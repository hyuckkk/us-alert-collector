-- us-alert-db 초기 스키마 (0단계)

-- 봇별 수신자. /start 를 보낸 사람이 여기에 들어온다. 소유자는 바로 active, 나머지는 pending.
CREATE TABLE recipients (
  chat_id    TEXT NOT NULL,
  bot        TEXT NOT NULL,              -- life | family | kids
  name       TEXT,
  username   TEXT,
  status     TEXT NOT NULL DEFAULT 'pending',  -- active | pending | blocked
  tz         TEXT NOT NULL DEFAULT 'America/Denver',
  joined_at  TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (chat_id, bot)
);

-- 발송한 항목(행사 1건 = 1행). 0단계에서는 테스트 항목만 들어간다.
CREATE TABLE items (
  item_key   TEXT PRIMARY KEY,
  bot        TEXT NOT NULL,
  title      TEXT NOT NULL,
  tag        TEXT,
  url        TEXT,
  created_at TEXT NOT NULL
);

-- 사람별 발송 기록
CREATE TABLE deliveries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  bot        TEXT NOT NULL,
  chat_id    TEXT NOT NULL,
  item_key   TEXT,
  message_id INTEGER,
  ok         INTEGER NOT NULL,
  error      TEXT,
  sent_at    TEXT NOT NULL
);

-- 버튼 클릭 원본 기록. 텔레그램이 같은 update 를 재전송해도 한 번만 남는다.
CREATE TABLE clicks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  bot        TEXT NOT NULL,
  update_id  INTEGER NOT NULL,
  chat_id    TEXT NOT NULL,
  from_id    TEXT NOT NULL,
  message_id INTEGER,
  item_key   TEXT,
  action     TEXT NOT NULL,             -- ack | mute | tag | unack | unmute | untag
  at         TEXT NOT NULL,
  UNIQUE (bot, update_id)
);

-- 사람별 항목 상태 (확인 / 관심없음)
CREATE TABLE item_state (
  chat_id    TEXT NOT NULL,
  item_key   TEXT NOT NULL,
  state      TEXT NOT NULL,             -- ACKNOWLEDGED | MUTED
  updated_at TEXT NOT NULL,
  PRIMARY KEY (chat_id, item_key)
);

-- 사람별 차단 태그 ("이 태그 그만")
CREATE TABLE muted_tags (
  chat_id TEXT NOT NULL,
  tag     TEXT NOT NULL,
  at      TEXT NOT NULL,
  PRIMARY KEY (chat_id, tag)
);

-- 만료일 관리 대상 (API 키 등). 값 자체는 저장하지 않고 이름과 날짜만.
CREATE TABLE key_expiry (
  name       TEXT PRIMARY KEY,
  expires_on TEXT NOT NULL,             -- YYYY-MM-DD
  note       TEXT,
  updated_at TEXT NOT NULL
);

-- 같은 시스템 알림을 두 번 보내지 않기 위한 기록
CREATE TABLE notices (
  key     TEXT PRIMARY KEY,
  sent_at TEXT NOT NULL
);

-- 기타 작은 상태값 (Worker 주소, 마지막 키 점검 결과 등)
CREATE TABLE kv (
  k          TEXT PRIMARY KEY,
  v          TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
