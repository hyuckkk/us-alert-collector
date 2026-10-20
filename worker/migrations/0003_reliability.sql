-- 2단계(1차 검증 반영): 정보 유실·중복 방지, 발송 재시도, 상세 페이지 변경 감시, 정보원 상태

-- 발송 선점·재시도. status: pending | sending | sent | failed(재시도 대기) | uncertain(성공 여부 불명) | dead(확실한 실패)
--                          | skipped | expired(이미 끝난 행사) | superseded(같은 행사의 새 판이 대신 감)
ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN next_attempt_at TEXT;
ALTER TABLE outbox ADD COLUMN claim TEXT;
ALTER TABLE outbox ADD COLUMN claimed_at TEXT;
ALTER TABLE outbox ADD COLUMN resend INTEGER NOT NULL DEFAULT 0;   -- 성공 여부 불명 후 1회 재전송(표시 붙임)
CREATE INDEX outbox_retry ON outbox (status, next_attempt_at);

-- 행사: 접수 일정으로 시간 알림(접수 시작일·마감 전날)을 걸기 위한 열과 식별 근거
ALTER TABLE events ADD COLUMN identity TEXT;
ALTER TABLE events ADD COLUMN registration_opens TEXT;
ALTER TABLE events ADD COLUMN deadline TEXT;
CREATE INDEX events_reg ON events (registration_opens);
CREATE INDEX events_deadline ON events (deadline);

-- 사람별 요약 방식: daily(기본, 시험 운영) | weekly | off, 요약 받을 현지 시각
ALTER TABLE people ADD COLUMN digest_mode TEXT NOT NULL DEFAULT 'daily';
ALTER TABLE people ADD COLUMN digest_hour INTEGER NOT NULL DEFAULT 8;

-- 수집기 처리 대기열: 새로 수집한 항목을 AI 처리 전에 모두 보존한다(상한 초과·AI 실패·예산 초과에도 유실 없음)
-- status: queued | extracted(AI 끝, Worker 등록 전) | done | stuck(재시도 한도 초과, 소유자 확인 필요) | expired
CREATE TABLE pending (
  key        TEXT PRIMARY KEY,
  hash       TEXT NOT NULL,
  source_id  TEXT,
  url        TEXT NOT NULL,
  title      TEXT,
  body       TEXT NOT NULL DEFAULT '{}',    -- {text, published, extra}
  hint       TEXT,                          -- 상세 페이지 재검사에서 온 항목이면 기존 행사 키
  priority   INTEGER NOT NULL DEFAULT 0,
  status     TEXT NOT NULL DEFAULT 'queued',
  attempts   INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  result     TEXT,                          -- AI 결과(행사 목록 JSON). Worker 등록 실패 시 AI 를 다시 부르지 않기 위함
  first_seen TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  lease_id   TEXT,                          -- 꺼내 간 실행의 임대 ID(동시 실행이 같은 항목을 처리하지 않게)
  lease_until TEXT                          -- 이 시각까지 결과 보고가 없으면 다시 꺼낼 수 있음
);
CREATE INDEX pending_queue ON pending (status, priority DESC, first_seen);

-- 행사 상세 페이지 재검사 목록. fingerprint 는 날짜·가격·접수·정원·장소 관련 문장만으로 만든 해시.
-- 한 페이지에 행사가 여럿일 수 있어 event_keys 는 목록(JSON). 재검사 주기는 그 행사들 중 가장 짧은 것.
CREATE TABLE watch (
  url          TEXT PRIMARY KEY,
  event_keys   TEXT NOT NULL DEFAULT '[]',
  source_id    TEXT,
  fingerprint  TEXT,
  reg_state    TEXT,                        -- upcoming-soon | upcoming | open | closed | unknown
  interval_h   INTEGER NOT NULL DEFAULT 24,
  next_check   TEXT NOT NULL,
  until_date   TEXT,                        -- 이 날짜가 지나면 감시 종료
  last_checked TEXT,
  failures     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX watch_due ON watch (next_check);

-- 정보원별 상태(설계안 6: 정상 조회, 마지막 성공, 연속 실패, 행사 추출, 상세 페이지, 대체 정보원)
CREATE TABLE source_health (
  id                   TEXT PRIMARY KEY,
  last_attempt_at      TEXT,
  last_ok_at           TEXT,
  last_status          TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_items           INTEGER NOT NULL DEFAULT 0,
  events_total         INTEGER NOT NULL DEFAULT 0,
  details_ok           INTEGER NOT NULL DEFAULT 0,
  details_total        INTEGER NOT NULL DEFAULT 0,
  alt                  TEXT
);

-- 시간 알림(오늘 접수 시작·내일 마감) 처리 기록. 알림 대기열 생성과 같은 트랜잭션에서 기록해 누락·중복 없이 이어서 처리
CREATE TABLE trigger_log (
  event_key TEXT NOT NULL,
  day       TEXT NOT NULL,
  type      TEXT NOT NULL,
  at        TEXT NOT NULL,
  PRIMARY KEY (event_key, day)
);

-- 후보 정보원 자동 검증 결과
ALTER TABLE candidates ADD COLUMN verified TEXT;   -- JSON {fetchable, items, eventish, official, duplicate_of, checked_at}

-- 사용량: 캐시·웹 검색·실패 호출도 따로 기록
ALTER TABLE usage ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage ADD COLUMN searches INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage ADD COLUMN errors INTEGER NOT NULL DEFAULT 0;
