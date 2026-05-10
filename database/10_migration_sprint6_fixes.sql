-- Sprint 6: 데이터 정합성 수정
-- 0) game_events 테이블 생성 (plan-game-issue-tracking.md 스펙)
-- 1) game_events UNIQUE 제약 추가 (중복 이벤트 방지)
-- 2) game_events.game_id ON DELETE CASCADE 추가 (고아 레코드 방지)

-- ============================================================
-- game_events 테이블 생성 (Sprint 5 이슈 트래킹 기능)
-- ============================================================
CREATE TABLE IF NOT EXISTS game_events (
    id              BIGSERIAL PRIMARY KEY,
    game_id         BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    event_date      DATE NOT NULL,
    event_type      VARCHAR(32),    -- patch / dlc / controversy / sale / unknown
    title           TEXT,           -- Steam News API 제목
    news_url        TEXT,           -- 원문 링크
    sentiment_delta FLOAT,          -- 부정 비율 변화량 (Histogram 기반)
    direction       VARCHAR(32),    -- negative_spike / positive_recovery
    created_at      TIMESTAMP DEFAULT now()
);

-- ============================================================
-- game_events UNIQUE 제약: 동일 게임·날짜·타입 중복 삽입 방지
-- ============================================================
ALTER TABLE game_events
    ADD CONSTRAINT uq_game_event_date_type
    UNIQUE (game_id, event_date, event_type);

-- ============================================================
-- game_events.game_id FK를 ON DELETE CASCADE로 교체
-- 기존 FK 이름은 PostgreSQL 기본 명명 규칙: game_events_game_id_fkey
-- ============================================================
ALTER TABLE game_events
    DROP CONSTRAINT IF EXISTS game_events_game_id_fkey;

ALTER TABLE game_events
    ADD CONSTRAINT game_events_game_id_fkey
    FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE;
