-- Sprint 3 Migration m005: language_code 레거시 컬럼 제거
-- 목적: Sprint 3에서 summary_type + review_language로 역할이 분리된 language_code를
--       game_review_summaries와 review_summary_jobs에서 최종 제거한다.
-- 주의: game_summary_cursor.language_code는 PK이므로 제거하지 않는다.
--       (PostgreSQL PK NULL 불허로 review_language=NULL인 unified 모드를 PK로 사용 불가)

BEGIN;

ALTER TABLE game_review_summaries
    DROP COLUMN IF EXISTS language_code;

ALTER TABLE review_summary_jobs
    DROP COLUMN IF EXISTS language_code;

COMMIT;
