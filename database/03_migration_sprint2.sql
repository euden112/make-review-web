-- Sprint 2 additive migration for existing databases
-- 목적: 이미 생성된 스키마에 감성 분석/비용 지표 컬럼과 샘플링 인덱스를 무중단으로 추가
-- 전제: 01_schema.sql 이 한 번 이상 적용되어 기본 테이블이 존재해야 함
-- 주의: 본 스크립트는 데이터 손실 없이 구조만 확장한다.
-- TODO(Crawling/Backend): Metacritic 크롤러 출력 파일명이 문서(reviews_metacritic.json)와 실제 코드(reviews.json) 간 혼선이 있으므로 백엔드 Ingestion 연동 전에 단일 기준으로 통일할 것.

begin;

-- 1) game_review_summaries: 대표 리뷰 매핑 + 감성 분석 결과 저장 필드
alter table if exists game_review_summaries
    add column if not exists representative_reviews_json jsonb,
    add column if not exists sentiment_overall varchar(16),
    add column if not exists sentiment_score numeric(5,2),
    add column if not exists aspect_sentiment_json jsonb;

-- 2) review_summary_jobs: 캐시/토큰/근거 커버리지 지표
alter table if exists review_summary_jobs
    add column if not exists map_cache_hit integer not null default 0,
    add column if not exists map_cache_miss integer not null default 0,
    add column if not exists map_input_tokens integer not null default 0,
    add column if not exists map_output_tokens integer not null default 0,
    add column if not exists reduce_input_tokens integer not null default 0,
    add column if not exists reduce_output_tokens integer not null default 0,
    add column if not exists evidence_coverage_ratio numeric(5,2);

-- 3) 층화 추출 성능 보강용 partial index
alter table if exists external_reviews
    add column if not exists review_categories_json jsonb;

create index if not exists idx_reviews_sampling_steam
    on external_reviews (game_id, language_code, is_recommended, helpful_count desc, playtime_hours desc)
    where is_deleted = false;

create index if not exists idx_reviews_sampling_meta
    on external_reviews (game_id, language_code, normalized_score_100, helpful_count desc, playtime_hours desc)
    where is_deleted = false;

create index if not exists idx_reviews_categories_json
    on external_reviews using gin (review_categories_json);

commit;
