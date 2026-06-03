-- 15_migration_steam_rating.sql
-- 종합 등급(표시용)을 Steam 공식 query_summary 기준으로 영속화.
--
-- 이전: 헤드라인 종합 감성이 LLM 파생값(sentiment_overall, sentiment_score)이라
--       노이즈가 섞였다. Steam 공식 review_score_desc는 전체 리뷰 모집단 기준
--       ground truth이므로, sentiment_overall(내부 3값 enum)은 공식 desc를 3값으로
--       접어 덮고, 정밀 9밴드 라벨/공식 추천률은 별도 컬럼으로 노출한다.
--
-- steam_rating_desc : raw 영문 (예: "Very Positive")
-- steam_rating_label: 한글 9밴드 (예: "매우 긍정적")
-- steam_rating_ratio: 공식 추천률 % (total_positive/total_reviews)
-- steam_rating_count: 공식 집계 총 리뷰 수

ALTER TABLE game_review_summaries
    ADD COLUMN IF NOT EXISTS steam_rating_desc  TEXT,
    ADD COLUMN IF NOT EXISTS steam_rating_label TEXT,
    ADD COLUMN IF NOT EXISTS steam_rating_ratio NUMERIC(5, 2),
    ADD COLUMN IF NOT EXISTS steam_rating_count INTEGER;
