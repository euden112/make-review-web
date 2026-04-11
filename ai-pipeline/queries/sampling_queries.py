"""Sampling queries used by the AI pipeline.

These constants are framework agnostic and can be bound later with asyncpg,
SQLAlchemy, or standalone DB scripts.
"""

STEAM_RATIO_QUERY = """
SELECT
    SUM(CASE WHEN is_recommended THEN 1 ELSE 0 END) AS pos_cnt,
    SUM(CASE WHEN NOT is_recommended THEN 1 ELSE 0 END) AS neg_cnt
FROM external_reviews r
JOIN platforms p ON p.id = r.platform_id
WHERE r.game_id = $1
  AND r.language_code = $2
  AND p.code = 'steam'
  AND r.is_deleted = false;
"""

METACRITIC_BIN_RATIO_QUERY = """
SELECT
    SUM(CASE WHEN normalized_score_100 < 50 THEN 1 ELSE 0 END) AS low_cnt,
    SUM(CASE WHEN normalized_score_100 >= 50 AND normalized_score_100 < 75 THEN 1 ELSE 0 END) AS mid_cnt,
    SUM(CASE WHEN normalized_score_100 >= 75 THEN 1 ELSE 0 END) AS high_cnt
FROM external_reviews r
JOIN platforms p ON p.id = r.platform_id
WHERE r.game_id = $1
  AND r.language_code = $2
  AND p.code = 'metacritic'
  AND r.is_deleted = false;
"""
