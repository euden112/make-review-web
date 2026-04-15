-- Upsert templates for Sprint 1 ingestion module
-- Replace :named placeholders with your DB client parameter style
-- 원칙: 같은 데이터를 다시 수집해도 INSERT + ON CONFLICT UPDATE로 안전하게 반영
-- 주의: :param 표기법은 라이브러리에 맞게 $1, %s 등으로 변환 필요
-- TODO(Backend): 아래 템플릿 호출은 반드시 ingestion run 단위의 단일 트랜잭션으로 묶어 구현할 것.
--   경계: [ingestion run start] -> [review upsert 일괄] -> [ingestion run finish]
--   이유: 부분 성공 상태를 방지하고 실패 시 재처리 가능성을 보장하기 위함.
-- FastAPI API 연동 시, 각 ingestion run 단위로 트랜잭션을 묶고 source_review_key 생성 규칙을 철저히 지킬 것.

-- 0) 참조 데이터 조회 (새 플랫폼 추가 시 review_type_code, scale_code 값만 넘기면 됨)
-- Review type ID 조회 (type_code: 'user', 'critic' 등)
select id from review_types where type_code = :review_type_code limit 1;

-- Score scale ID 조회 (scale_code: 'binary', '10', '100' 등)
select id from score_scales where scale_code = :scale_code limit 1;

-- 1) game upsert
-- normalized_title이 같으면 같은 게임으로 보고 upsert
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
-- 같은 플랫폼에서 external_game_id가 같으면 같은 게임 매핑으로 처리
insert into game_platform_map (
    game_id,
    platform_id,
    external_game_id,
    crawled_at,
    platform_meta_json,
    updated_at
)
values (
    :game_id,
    :platform_id,
    :external_game_id,
    :crawled_at,
    :platform_meta_json,
    now()
)
on conflict (platform_id, external_game_id)
do update set
    game_id = excluded.game_id,
    crawled_at = coalesce(excluded.crawled_at, game_platform_map.crawled_at),
    platform_meta_json = coalesce(excluded.platform_meta_json, game_platform_map.platform_meta_json),
    updated_at = now()
returning id;

-- 3) ingestion run start
-- 수집 작업 시작 로그를 남기고 생성된 run_id를 이후 리뷰 upsert에 사용
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
--
-- source_meta_json 저장 가이드 (정규화 컬럼과 중복 금지):
-- - Steam: {"author_id": "...", "reviewer_level": "verified_buyer", "helpful_percent": 95.0}
--   주의: playtime_hours, author_name, score 등은 정규화 컬럼에만 저장
-- - Metacritic: 현재 수집 필드 없음 (향후 고유 필드만 추가, outlet/critic_name 등은 미수집)
--
-- 새로운 플랫폼 추가 시:
-- 1. review_type_id: review_types 테이블에서 조회하거나 위 template 0으로 확인
-- 2. score_scale_id: score_scales 테이블에서 조회하거나 위 template 0으로 확인
-- 3. normalized_score_100은 DB 트리거가 전담 계산하므로 파라미터로 넘기지 않는다.
insert into external_reviews (
    platform_id,
    game_id,
    ingestion_run_id,
    source_review_id,
    source_review_key,
    review_type_id,
    author_name,
    is_recommended,
    score_raw,
    score_scale_id,
    language_code,
    review_text_raw,
    review_text_clean,
    reviewed_at,
    helpful_count,
    playtime_hours,
    source_meta_json,
    review_categories_json,
    updated_at
)
values (
    :platform_id,
    :game_id,
    :ingestion_run_id,
    :source_review_id,
    :source_review_key,
    :review_type_id,
    :author_name,
    :is_recommended,
    :score_raw,
    :score_scale_id,
    :language_code,
    :review_text_raw,
    :review_text_clean,
    :reviewed_at,
    coalesce(:helpful_count, 0),
    :playtime_hours,
    :source_meta_json,
    :review_categories_json,
    now()
)
on conflict (platform_id, game_id, source_review_key)
do update set
    -- 이미 존재하는 리뷰면 최신 수집값으로 갱신
    ingestion_run_id = excluded.ingestion_run_id,
    source_review_id = coalesce(excluded.source_review_id, external_reviews.source_review_id),
    review_type_id = excluded.review_type_id,
    author_name = excluded.author_name,
    is_recommended = excluded.is_recommended,
    score_raw = excluded.score_raw,
    score_scale_id = excluded.score_scale_id,
    language_code = excluded.language_code,
    review_text_raw = excluded.review_text_raw,
    review_text_clean = excluded.review_text_clean,
    reviewed_at = excluded.reviewed_at,
    helpful_count = excluded.helpful_count,
    playtime_hours = excluded.playtime_hours,
    source_meta_json = coalesce(excluded.source_meta_json, external_reviews.source_meta_json),
    review_categories_json = excluded.review_categories_json,
    is_deleted = false,
    updated_at = now();

-- 5) ingestion run finish
-- 수집 작업 종료 시 최종 상태/건수/오류 메시지 기록
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

-- 6) 프론트 노출용 최신 n개 조회 (플랫폼/타입별)
-- A. Steam 최신 n개
select r.*
from external_reviews r
join platforms p on p.id = r.platform_id
where r.game_id = :game_id
  and p.code = 'steam'
  and r.is_deleted = false
order by r.reviewed_at desc nulls last, r.id desc
limit :n;

-- B. Metacritic critic 최신 n개
select r.*
from external_reviews r
join platforms p on p.id = r.platform_id
join review_types t on t.id = r.review_type_id
where r.game_id = :game_id
  and p.code = 'metacritic'
  and t.type_code = 'critic'
  and r.is_deleted = false
order by r.reviewed_at desc nulls last, r.id desc
limit :n;

-- C. Metacritic user 최신 n개
select r.*
from external_reviews r
join platforms p on p.id = r.platform_id
join review_types t on t.id = r.review_type_id
where r.game_id = :game_id
  and p.code = 'metacritic'
  and t.type_code = 'user'
  and r.is_deleted = false
order by r.reviewed_at desc nulls last, r.id desc
limit :n;

-- 7) 증분 요약용 커서 조회/업데이트
select last_summarized_review_id, last_summary_version
from game_summary_cursor
where game_id = :game_id
  and language_code = :language_code;

insert into game_summary_cursor (
    game_id,
    language_code,
    last_summarized_review_id,
    last_summary_version,
    updated_at
)
values (
    :game_id,
    :language_code,
    :last_summarized_review_id,
    :last_summary_version,
    now()
)
on conflict (game_id, language_code)
do update set
    -- 커서 역행 방지: 늦게 끝난 작업이 더 작은 review_id로 덮어쓰지 못하게 한다.
    last_summarized_review_id = case
        when game_summary_cursor.last_summarized_review_id is null then excluded.last_summarized_review_id
        when excluded.last_summarized_review_id is null then game_summary_cursor.last_summarized_review_id
        else greatest(game_summary_cursor.last_summarized_review_id, excluded.last_summarized_review_id)
    end,
    last_summary_version = greatest(game_summary_cursor.last_summary_version, excluded.last_summary_version),
    updated_at = now();

-- 8) 증분 처리 대상(delta) 리뷰 조회
-- 마지막 요약 이후에 추가된 리뷰만 조회
select r.*
from external_reviews r
where r.game_id = :game_id
  and r.is_deleted = false
  and r.id > coalesce(:last_summarized_review_id, 0)
order by r.id asc;

-- 9) 요약 작업(job) 시작/종료
-- job 테이블은 상태/범위 중심으로 기록
insert into review_summary_jobs (
    game_id,
    language_code,
    spam_rule_version,
    status,
    from_review_id,
    to_review_id,
    input_review_count,
    chunk_count,
    started_at
)
values (
    :game_id,
    :language_code,
    :spam_rule_version,
    'started',
    :from_review_id,
    :to_review_id,
    :input_review_count,
    :chunk_count,
    now()
)
returning id;

update review_summary_jobs
set
    status = :status,
    error_message = :error_message,
    ended_at = now()
where id = :job_id;

-- 10) map 단계 chunk 요약 저장
-- 각 청크별 요약 결과 저장
insert into review_summary_chunks (
    job_id,
    chunk_no,
    input_review_count,
    chunk_summary_text
)
values (
    :job_id,
    :chunk_no,
    :input_review_count,
    :chunk_summary_text
)
on conflict (job_id, chunk_no)
do update set
    input_review_count = excluded.input_review_count,
    chunk_summary_text = excluded.chunk_summary_text;

-- 11) 최종 요약 저장(현재 버전 갱신)
-- 기존 현재 버전은 비활성화하고 새 요약을 현재 버전으로 저장
update game_review_summaries
set is_current = false
where game_id = :game_id
    and language_code = :language_code
  and is_current = true;

insert into game_review_summaries (
    game_id,
        language_code,
    job_id,
    summary_version,
    summary_text,
    pros_json,
    cons_json,
    keywords_json,
    steam_recommend_ratio,
    metacritic_critic_avg,
    metacritic_user_avg,
    source_review_count,
    covered_from_review_id,
    covered_to_review_id,
    is_current,
    created_at
)
values (
    :game_id,
    :language_code,
    :job_id,
    :summary_version,
    :summary_text,
    :pros_json,
    :cons_json,
    :keywords_json,
    :steam_recommend_ratio,
    :metacritic_critic_avg,
    :metacritic_user_avg,
    :source_review_count,
    :covered_from_review_id,
    :covered_to_review_id,
    true,
    now()
);

-- 12) General 우선 재분류 대상 조회 템플릿
-- 목적: review_categories_json에 General이 포함된 리뷰를 재분류 배치의 우선 입력으로 사용
-- 파라미터:
-- :game_id, :language_code, :days_back, :limit
select r.*
from external_reviews r
where r.game_id = :game_id
    and r.language_code = :language_code
    and r.is_deleted = false
    and r.reviewed_at >= now() - (:days_back || ' days')::interval
    and coalesce(r.review_categories_json, '[]'::jsonb) @> '["General"]'::jsonb
order by r.reviewed_at desc nulls last, r.helpful_count desc, r.id desc
limit :limit;
