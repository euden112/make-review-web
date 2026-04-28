-- Sprint 3 Migration m002: ReviewSummaryJob 신뢰도(Reliability) 4개 컬럼 추가
-- 목적: Gemini 출력 품질을 결정론적으로 측정하는 지표 저장
--   - schema_compliance:      0.0~1.0 (9개 필수 필드 중 채워진 비율)
--   - hallucination_score:    0.0~1.0 (인용된 review_id 중 실제 존재 비율)
--   - sentiment_consistency:  0 | 1   (sentiment_overall 레이블 vs score 범위 일치)
--   - anchor_deviation:       0.0~1.0 (|AI sentiment_score - steam_ratio| / 100)

BEGIN;

ALTER TABLE review_summary_jobs
    ADD COLUMN IF NOT EXISTS schema_compliance    numeric(4, 3),
    ADD COLUMN IF NOT EXISTS hallucination_score  numeric(4, 3),
    ADD COLUMN IF NOT EXISTS sentiment_consistency integer,
    ADD COLUMN IF NOT EXISTS anchor_deviation     numeric(4, 3);

COMMIT;
