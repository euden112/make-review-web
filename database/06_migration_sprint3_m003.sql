-- Sprint 3 Migration m003: GameReviewSummary 신뢰도(Reliability) 4개 컬럼 추가
-- 목적: 요약 품질 지표를 게임별 요약 레코드에 저장
--   - sentiment_alignment:      0.0~1.0 (1 - |sentiment_score - steam_recommend_ratio| / 100)
--   - coverage_ratio:           0.0~1.0 (source_review_count / total_reviews_in_db)
--   - staleness_ratio:          0.0~1.0 (new_reviews_since_last_summary / total_reviews_in_db)
--   - semantic_similarity_score: 0.0~1.0 (paraphrase-multilingual-MiniLM-L12-v2 코사인 유사도)

BEGIN;

ALTER TABLE game_review_summaries
    ADD COLUMN IF NOT EXISTS sentiment_alignment       numeric(5, 4),
    ADD COLUMN IF NOT EXISTS coverage_ratio            numeric(5, 4),
    ADD COLUMN IF NOT EXISTS staleness_ratio           numeric(5, 4),
    ADD COLUMN IF NOT EXISTS semantic_similarity_score numeric(5, 4);

COMMIT;
