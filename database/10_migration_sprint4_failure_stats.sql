-- Sprint 4 Migration 10: review_summary_jobs에 failure_reasons_json 컬럼 추가
-- 목적: map 단계 청크별 실패 사유 통계를 JSONB로 기록
--   - call_failed:              Ollama API 호출 자체 실패 횟수
--   - format_invalid_recovered: 형식 검증 실패 후 재시도로 복구된 청크 수
--   - format_invalid_dropped:   2회 실패 후 최종 제외된 청크 수
--   - cache_invalid:            캐시 hit이지만 형식 검증 실패한 케이스

BEGIN;

ALTER TABLE review_summary_jobs
    ADD COLUMN IF NOT EXISTS failure_reasons_json JSONB;

COMMENT ON COLUMN review_summary_jobs.failure_reasons_json IS
    'map stage chunk failure stats: {call_failed, format_invalid_recovered, format_invalid_dropped, cache_invalid}';

COMMIT;
