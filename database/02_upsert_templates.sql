-- Upsert templates for Sprint 1 ingestion module
-- Replace :named placeholders with your DB client parameter style
-- 원칙: 재수집을 전제로 INSERT + ON CONFLICT UPDATE 사용
-- 주의: :param 표기법은 라이브러리에 맞게 $1, %s 등으로 변환 필요

-- 1) game upsert
-- normalized_title 기준으로 동일 게임 판단
insert into games (
    canonical_title,
    normalized_title,
    release_date,
    updated_at
)
values (
    :canonical_title,
    :normalized_title,
    :release_date,
    now()
)
on conflict (normalized_title)
do update set
    canonical_title = excluded.canonical_title,
    release_date = coalesce(excluded.release_date, games.release_date),
    updated_at = now()
returning id;

-- 2) game-platform mapping upsert
-- 외부 플랫폼 게임 ID가 같으면 동일 매핑으로 간주
insert into game_platform_map (
    game_id,
    platform_id,
    external_game_id,
    external_game_url,
    crawled_at,
    updated_at
)
values (
    :game_id,
    :platform_id,
    :external_game_id,
    :external_game_url,
    :crawled_at,
    now()
)
on conflict (platform_id, external_game_id)
do update set
    game_id = excluded.game_id,
    external_game_url = coalesce(excluded.external_game_url, game_platform_map.external_game_url),
    crawled_at = coalesce(excluded.crawled_at, game_platform_map.crawled_at),
    updated_at = now()
returning id;

-- 3) ingestion run start
-- 실행 시작 로그 생성 후 run_id를 이후 리뷰 upsert에 전달
insert into ingestion_runs (
    platform_id,
    game_id,
    status,
    started_at
)
values (
    :platform_id,
    :game_id,
    'started',
    now()
)
returning id;

-- 4) review upsert
-- source_review_key 생성 규칙 예시
-- steam: sha256(author_id + '|' + date_posted + '|' + review_text_clean)
-- metacritic: sha256(author + '|' + date + '|' + type + '|' + review_text_clean)
-- source_review_id가 없어도 source_review_key로 중복 제어 가능
insert into external_reviews (
    platform_id,
    game_id,
    ingestion_run_id,
    source_review_id,
    source_review_key,
    review_type,
    author_name,
    is_recommended,
    score_raw,
    score_100,
    language_code,
    review_text_raw,
    review_text_clean,
    reviewed_at,
    helpful_count,
    playtime_hours,
    updated_at
)
values (
    :platform_id,
    :game_id,
    :ingestion_run_id,
    :source_review_id,
    :source_review_key,
    :review_type,
    :author_name,
    :is_recommended,
    :score_raw,
    :score_100,
    :language_code,
    :review_text_raw,
    :review_text_clean,
    :reviewed_at,
    coalesce(:helpful_count, 0),
    :playtime_hours,
    now()
)
on conflict (platform_id, game_id, source_review_key)
do update set
    -- 재수집 시 최신 값으로 갱신
    ingestion_run_id = excluded.ingestion_run_id,
    source_review_id = coalesce(excluded.source_review_id, external_reviews.source_review_id),
    review_type = excluded.review_type,
    author_name = excluded.author_name,
    is_recommended = excluded.is_recommended,
    score_raw = excluded.score_raw,
    score_100 = excluded.score_100,
    language_code = excluded.language_code,
    review_text_raw = excluded.review_text_raw,
    review_text_clean = excluded.review_text_clean,
    reviewed_at = excluded.reviewed_at,
    helpful_count = excluded.helpful_count,
    playtime_hours = excluded.playtime_hours,
    is_deleted = false,
    updated_at = now();

-- 5) ingestion run finish
-- 한 실행 단위의 최종 상태/카운트를 마감 기록
update ingestion_runs
set
    status = :status,
    ended_at = now(),
    fetched_count = :fetched_count,
    inserted_count = :inserted_count,
    updated_count = :updated_count,
    error_count = :error_count,
    error_message = :error_message
where id = :ingestion_run_id;
