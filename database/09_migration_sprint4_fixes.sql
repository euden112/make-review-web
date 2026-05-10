-- Sprint 4 후속 수정: 08_migration_sprint4.sql 스키마 문제 보정
-- 1) FLOAT → NUMERIC(5,2): ORM 모델(domain.py)과 타입 일치
-- 2) game_id NOT NULL: ORM nullable=False 와 DB 제약 일치
-- 주의: 08_migration_sprint4.sql이 이미 적용된 환경에서 실행할 것

BEGIN;

-- ============================================================
-- playtime_analyses 타입·제약 보정
-- ============================================================

-- game_id NOT NULL (ORM nullable=False 와 일치)
ALTER TABLE playtime_analyses
    ALTER COLUMN game_id SET NOT NULL;

-- score 컬럼 FLOAT → NUMERIC(5,2)
ALTER TABLE playtime_analyses
    ALTER COLUMN early_score TYPE NUMERIC(5, 2) USING ROUND(early_score::NUMERIC, 2),
    ALTER COLUMN mid_score   TYPE NUMERIC(5, 2) USING ROUND(mid_score::NUMERIC, 2),
    ALTER COLUMN late_score  TYPE NUMERIC(5, 2) USING ROUND(late_score::NUMERIC, 2);

-- ============================================================
-- critic_summaries 타입·제약 보정
-- ============================================================

ALTER TABLE critic_summaries
    ALTER COLUMN game_id SET NOT NULL;

ALTER TABLE critic_summaries
    ALTER COLUMN score TYPE NUMERIC(5, 2) USING ROUND(score::NUMERIC, 2);

-- ============================================================
-- id / game_id BIGINT 변환 (games.id bigserial 참조 일관성)
-- ============================================================
ALTER TABLE playtime_analyses
    ALTER COLUMN id      TYPE BIGINT,
    ALTER COLUMN game_id TYPE BIGINT;

ALTER TABLE critic_summaries
    ALTER COLUMN id      TYPE BIGINT,
    ALTER COLUMN game_id TYPE BIGINT;

COMMIT;
