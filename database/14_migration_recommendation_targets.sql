-- 14_migration_recommendation_targets.sql
-- "이런 사람에게 추천" 개선: user reduce가 생성하는 game별 플레이어 유형을 저장.
--
-- 이전: recommendation-targets 엔드포인트가 카테고리별 하드코딩 문구를 사용해
--       모든 게임이 동일한 추천 문구를 출력했다. reduce는 recommended_for/caution_for를
--       생성하고도 폐기했다. 이를 game_review_summaries에 영속화해 실데이터로 서빙한다.
--
-- 각 컬럼은 [{label, reason}] 형태의 JSONB 배열.

ALTER TABLE game_review_summaries
    ADD COLUMN IF NOT EXISTS recommended_for_json JSONB,
    ADD COLUMN IF NOT EXISTS caution_for_json JSONB;
