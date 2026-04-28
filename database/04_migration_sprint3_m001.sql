-- Sprint 3 Migration m001: summary_type / review_language 컬럼 추가
-- 목적: language_code="unified" 워크어라운드(eb68f3e)를 정식 스키마로 교체
--   - game_review_summaries: summary_type + review_language 컬럼 추가, unique 제약 교체
--   - game_summary_cursor:   summary_type 컬럼 추가 (PK는 아직 유지 — NULL 허용 PK 불가)
-- 주의: language_code 컬럼은 제거하지 않는다. 코드 전환 완료 후 m005에서 제거 예정.
-- 주의: PostgreSQL PK는 NULL 불허. game_summary_cursor PK는 기존 유지하고
--       summary_type 컬럼을 메타데이터로만 추가. 코드는 language_code로 조회 계속 가능.

BEGIN;

-- ============================================================
-- 1. game_review_summaries
-- ============================================================

ALTER TABLE game_review_summaries
    ADD COLUMN IF NOT EXISTS summary_type    varchar(16),
    ADD COLUMN IF NOT EXISTS review_language varchar(10);

-- 기존 워크어라운드 데이터 마이그레이션
-- language_code='unified' → summary_type='unified', review_language=NULL
-- language_code='en'/'ko'/'zh' 등 → summary_type='regional', review_language=해당 코드
UPDATE game_review_summaries
SET
    summary_type    = CASE WHEN language_code = 'unified' THEN 'unified' ELSE 'regional' END,
    review_language = CASE WHEN language_code = 'unified' THEN NULL     ELSE language_code END
WHERE summary_type IS NULL;

-- 행이 하나도 없는 경우 대비 기본값 설정
UPDATE game_review_summaries SET summary_type = 'unified' WHERE summary_type IS NULL;

ALTER TABLE game_review_summaries
    ALTER COLUMN summary_type SET NOT NULL,
    ALTER COLUMN summary_type SET DEFAULT 'unified';

-- unique 제약 교체
-- 기존: (game_id, language_code, summary_version)
-- 신규: unified 행 / regional 행을 partial index 2개로 대체
--       (PostgreSQL unique constraint는 NULL=NULL 비교를 하지 않으므로 partial index 필요)
ALTER TABLE game_review_summaries
    DROP CONSTRAINT IF EXISTS uq_game_summary_version;

CREATE UNIQUE INDEX IF NOT EXISTS uq_game_summary_version_unified
    ON game_review_summaries (game_id, summary_type, summary_version)
    WHERE review_language IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_game_summary_version_regional
    ON game_review_summaries (game_id, summary_type, review_language, summary_version)
    WHERE review_language IS NOT NULL;

-- ============================================================
-- 2. game_summary_cursor
-- ============================================================
-- PK는 (game_id, language_code) 그대로 유지.
-- summary_type 컬럼만 추가해 신규 코드에서 참조 가능하도록 함.
-- review_language는 language_code와 동일하므로 별도 컬럼 추가 생략.

ALTER TABLE game_summary_cursor
    ADD COLUMN IF NOT EXISTS summary_type varchar(16);

-- 기존 커서 데이터 채우기
UPDATE game_summary_cursor
SET summary_type = CASE WHEN language_code = 'unified' THEN 'unified' ELSE 'regional' END
WHERE summary_type IS NULL;

ALTER TABLE game_summary_cursor
    ALTER COLUMN summary_type SET NOT NULL,
    ALTER COLUMN summary_type SET DEFAULT 'unified';

COMMIT;
