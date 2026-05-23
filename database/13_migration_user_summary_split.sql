-- Migration 13: B안 — unified 본문 폐지(한줄평 유지), user/critic 분리 저장.
-- 1) user_summaries 테이블 신설 (critic_summaries 미러)
-- 2) game_review_summaries.summary_text NOT NULL 제약 완화 — 이후 unified body는 NULL로 저장.

CREATE TABLE IF NOT EXISTS user_summaries (
    id            BIGSERIAL PRIMARY KEY,
    game_id       BIGINT NOT NULL REFERENCES games(id),
    summary       TEXT,
    sentiment     VARCHAR(16),
    score         NUMERIC(5, 2),
    pros          JSONB,
    cons          JSONB,
    keywords      JSONB,
    review_count  INTEGER,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_user_summary_game UNIQUE (game_id)
);

CREATE INDEX IF NOT EXISTS idx_user_summaries_game ON user_summaries(game_id);

ALTER TABLE game_review_summaries
    ALTER COLUMN summary_text DROP NOT NULL;
