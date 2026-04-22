-- Sprint 1 schema for review ingestion
-- Target: PostgreSQL 13+
-- 목적: Steam/Metacritic 리뷰를 중복 없이 적재하고, 조회/운영 추적이 가능하도록 구조화
-- 핵심: PK/FK/UNIQUE/CHECK/INDEX로 데이터 정합성과 성능을 DB 레벨에서 보장한다.
-- 확장: 플랫폼별 점수 체계 차이(steam binary, critic 100, user 10)와
--      AI 증분 요약(map-reduce) 운영을 지원한다.
-- TODO(Crawling/Backend): Metacritic 크롤러 출력 파일명이 문서(reviews_metacritic.json)와 실제 코드(reviews.json) 간 혼선이 있으므로 백엔드 Ingestion 연동 전에 단일 기준으로 통일할 것.
--   - 현재 문서 기준: reviews_metacritic.json
--   - 일부 구현/연동 기준: reviews.json
--   - 파일명 불일치 시 API 적재 누락/오탐 가능성이 있으므로 크롤러 파트에서 반드시 정리 필요.
-- 백엔드 연동 메모: 크롤러 metacritic 출력 파일명이 문서(reviews_metacritic.json)와 실제(reviews.json) 간 차이가 있으므로 API 연동 시 확인 요망.

begin;

-- 트랜잭션 시작: 중간에 하나라도 실패하면 전체 롤백되고,
-- 마지막 commit까지 성공했을 때만 반영된다.

-- [테이블 역할] 플랫폼 마스터
-- Steam, Metacritic 같은 리뷰 사이트를 고유 코드(code)로 관리하는 테이블.
create table if not exists platforms (
    id bigserial primary key,
    code varchar(30) not null unique,
    name varchar(100) not null,
    created_at timestamptz not null default now()
);

-- [테이블 역할] 점수 체계 마스터 (확장성: 새 플랫폼 추가 시 행만 추가)
-- binary (추천/비추천), 10점, 100점, 5점 등 플랫폼별 점수 스케일 관리
create table if not exists score_scales (
    id bigserial primary key,
    scale_code varchar(20) not null unique,
    min_value numeric not null,
    max_value numeric not null,
    description text,
    created_at timestamptz not null default now()
);

-- [테이블 역할] 리뷰 타입 마스터 (확장성: 새 플랫폼/타입 추가 시 행만 추가)
-- user: 일반 사용자 리뷰, critic: 전문가/평론가 리뷰
create table if not exists review_types (
    id bigserial primary key,
    type_code varchar(30) not null unique,
    description text,
    created_at timestamptz not null default now()
);

-- [테이블 역할] 게임 마스터   
-- 서비스에서 사용하는 통합 게임 목록.
-- 같은 게임이 여러 플랫폼에 있어도 games.id 하나로 식별한다.
-- normalized_title은 검색/중복 판별용 제목(소문자, 공백 정리 등 전처리된 값).
create table if not exists games (
    id bigserial primary key,
    canonical_title varchar(255) not null,
    normalized_title varchar(255) not null,
    release_date date,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint uq_games_normalized_title unique (normalized_title)
);

-- [테이블 역할] 외부 식별자 매핑
-- games.id(내부 통합 게임 ID)와 각 플랫폼의 게임 ID를 매핑하는 테이블.\
-- FK로 연결해 존재하지 않는 게임/플랫폼 값이 들어오는 것을 방지한다.
create table if not exists game_platform_map (
    id bigserial primary key,
    game_id bigint not null references games(id),
    platform_id bigint not null references platforms(id),
    external_game_id varchar(120) not null,
    crawled_at timestamptz,
    platform_meta_json jsonb,
    -- platform_meta_json: 플랫폼별 게임 메타데이터
    -- steam 예: {"price_usd": 19.99, "discount_percent": 0, "tags": ["Action", "RPG"], "recommendation_count": 15000}
    -- metacritic 예: {"score": 78, "user_score": 7.8, "platform_name": "PC"}
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint uq_game_platform_external_game unique (platform_id, external_game_id),
    constraint uq_game_platform_once unique (game_id, platform_id)
);

-- [테이블 역할] 운영 로그(ingestion runs)
-- 수집/적재 작업 1회 실행에 대한 로그 레코드.
-- 시작/종료 시각, 건수, 오류를 저장해 장애 분석/재시도/주간 리포트 근거로 사용한다.
create table if not exists ingestion_runs (
    id bigserial primary key,
    platform_id bigint not null references platforms(id),
    game_id bigint references games(id),
    status varchar(20) not null,
    started_at timestamptz not null default now(),
    ended_at timestamptz,
    fetched_count integer not null default 0,
    inserted_count integer not null default 0,
    updated_count integer not null default 0,
    error_count integer not null default 0,
    error_message text,
    constraint ck_ingestion_status check (status in ('started', 'success', 'failed', 'partial'))
);

-- [테이블 역할] Ingestion Dead Letter Queue(DLQ)
-- 적재 실패 건을 건별로 보존해 재처리/장애 분석에 활용한다.
create table if not exists ingestion_dead_letters (
    id bigserial primary key,
    ingestion_run_id bigint references ingestion_runs(id) on delete set null,
    platform_id bigint references platforms(id),
    game_id bigint references games(id),
    source_review_key varchar(255),
    failure_stage varchar(50) not null,
    failure_reason text not null,
    payload_json jsonb,
    is_retryable boolean not null default true,
    resolved boolean not null default false,
    failed_at timestamptz not null default now(),
    resolved_at timestamptz,
    created_at timestamptz not null default now()
);

create index if not exists idx_ingestion_dead_letters_run_failed_at
    on ingestion_dead_letters (ingestion_run_id, failed_at desc);

create index if not exists idx_ingestion_dead_letters_unresolved
    on ingestion_dead_letters (resolved, failed_at desc)
    where resolved = false;

-- [테이블 역할] 외부 리뷰 본문 저장소(핵심)    
-- 실제 리뷰 원문/정제 텍스트/점수/작성자/작성일을 저장한다.
-- source_review_key 유니크 제약으로 중복 적재를 막고 upsert 충돌 키로 사용한다.
-- score_scale, review_type은 관리형 테이블로 참조하여 새 플랫폼 추가 시 테이블 수정 불필요
create table if not exists external_reviews (
    id bigserial primary key,
    platform_id bigint not null references platforms(id),
    game_id bigint not null references games(id),
    ingestion_run_id bigint references ingestion_runs(id),

    -- 원천 식별 정보
    -- source_review_id: 플랫폼이 제공하는 원본 리뷰 ID (없을 수 있음)
    -- source_review_key: 인위적으로 만든 안정적인 중복 방지 키 (필수)
    source_review_id varchar(150),
    source_review_key varchar(255) not null,

    -- 원천 데이터
    review_type_id bigint not null references review_types(id),
    author_name varchar(255),
    is_recommended boolean,
    score_raw varchar(50),
    -- normalized_score_100: DB 규칙으로 계산되는 공통 100점 축 점수
    score_scale_id bigint references score_scales(id),
    normalized_score_100 numeric(5,2),
    language_code varchar(10),
    review_text_raw text,
    review_text_clean text not null,
    reviewed_at timestamptz,
    helpful_count integer not null default 0,
    playtime_hours numeric(8,2),
    source_meta_json jsonb,
    review_categories_json jsonb,
    -- source_meta_json: 플랫폼별 고유 메타데이터 (정규화된 필드와 중복 금지)
    -- steam 예: {"author_id": "76561198123456789", "reviewer_level": "verified_buyer", "helpful_percent": 95.0}
    -- metacritic: 현재 수집 필드 없음 (향후 API 확장 시 고유 필드만 추가, outlet/critic_name 등 수집되지 않음)
    -- 정규화된 필드(playtime_hours, author_name, score)는 위의 표준 컬럼에만 저장, JSONB에는 저장 금지
    -- steam 예: {"author_id": "76561198123456789", "reviewer_level": "verified_buyer", "helpful_percent": 95.0}
    -- metacritic: 현재 수집 필드 없음 (향후 API 확장 시 고유 필드만 추가, outlet/critic_name 등 수집되지 않음)

    is_deleted boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    constraint ck_normalized_score_range check (
        normalized_score_100 is null or (normalized_score_100 >= 0 and normalized_score_100 <= 100)
    ),
    constraint ck_review_text_non_empty check (length(trim(review_text_clean)) > 0),
    -- 같은 플랫폼/게임에서 동일 리뷰는 1건만 유지
    constraint uq_external_review_key unique (platform_id, game_id, source_review_key)
);

-- [함수 역할] 플랫폼별 점수 체계를 공통 100점 축으로 자동 정규화
-- steam: 추천/비추천 -> 100/0
-- metacritic critic: score_raw를 0~100으로 해석
-- metacritic user: score_raw를 0~10으로 해석 후 x10
-- 새로운 플랫폼은 적재 코드에서 score_scale_id를 명시하면 트리거가 자동 정규화
create or replace function fn_normalize_review_score()
returns trigger
language plpgsql
as $$
declare
    v_platform_code varchar(30);
    v_review_type_code varchar(30);
    v_score_raw text;
    v_num_text text;
    v_score numeric(10,4);
begin
    select code into v_platform_code
    from platforms
    where id = new.platform_id;

    select type_code into v_review_type_code
    from review_types
    where id = new.review_type_id;

    -- 숫자가 전혀 없는 입력값(N/A, 별점없음, 기호/알파벳-only)은 정규식 추출 전에 null 처리
    -- 하위 캐스팅 단계 예외를 사전에 차단해 트랜잭션 중단을 방지한다.
    v_score_raw := btrim(coalesce(new.score_raw, ''));
    if v_score_raw = '' or v_score_raw !~ '[0-9]' then
        v_num_text := null;
    else
        v_num_text := substring(v_score_raw from '([0-9]+(?:\.[0-9]+)?)');
    end if;

    if v_num_text is not null and btrim(v_num_text) <> '' and v_num_text ~ '^[0-9]+(?:\.[0-9]+)?$' then
        begin
            v_score := v_num_text::numeric;
        exception
            when others then
                -- 비정상 숫자 문자열은 캐스팅 실패 대신 null로 처리해 트랜잭션 중단을 방지한다.
                v_score := null;
        end;
    else
        v_score := null;
    end if;

    -- 플랫폼별 정규화 규칙 적용
    if v_platform_code = 'steam' then
        -- score_scale_id 자동 설정: binary
        if new.score_scale_id is null then
            select id into new.score_scale_id from score_scales where scale_code = 'binary' limit 1;
        end if;
        if new.is_recommended is null then
            new.normalized_score_100 := null;
        elsif new.is_recommended then
            new.normalized_score_100 := 100;
        else
            new.normalized_score_100 := 0;
        end if;

    elsif v_platform_code = 'metacritic' then
        if v_review_type_code = 'critic' then
            -- score_scale_id 자동 설정: 100
            if new.score_scale_id is null then
                select id into new.score_scale_id from score_scales where scale_code = '100' limit 1;
            end if;
            if v_score is null then
                new.normalized_score_100 := null;
            else
                new.normalized_score_100 := least(greatest(v_score, 0), 100);
            end if;
        elsif v_review_type_code = 'user' then
            -- score_scale_id 자동 설정: 10
            if new.score_scale_id is null then
                select id into new.score_scale_id from score_scales where scale_code = '10' limit 1;
            end if;
            if v_score is null then
                new.normalized_score_100 := null;
            else
                new.normalized_score_100 := least(greatest(v_score, 0), 10) * 10;
            end if;
        end if;
    else
        -- 향후 플랫폼 확장 시: 규칙 미정이면 normalized_score_100 입력값을 그대로 둔다.
        -- (입력값이 없으면 null 유지)
        null;
    end if;

    return new;
end;
$$;

drop trigger if exists trg_normalize_review_score on external_reviews;
create trigger trg_normalize_review_score
before insert or update on external_reviews
for each row
execute function fn_normalize_review_score();

-- [테이블 역할] 게임/언어별 증분 요약 커서
-- 마지막으로 AI 요약에 반영된 review_id를 기록해 다음 배치에서 delta만 처리한다.
create table if not exists game_summary_cursor (
    game_id bigint not null references games(id),
    language_code varchar(10) not null,
    last_summarized_review_id bigint references external_reviews(id),
    last_summary_version integer not null default 0,
    updated_at timestamptz not null default now(),
    constraint pk_game_summary_cursor primary key (game_id, language_code)
);

-- [테이블 역할] AI 요약 실행 이력
-- 배치 작업 상태 추적: 시작/종료, 처리 범위, 청크 개수, 언어, 에러 메시지 기록
-- 운영 정책: 토큰 비용 최소화를 위해 요약 전략은 map_reduce로 고정한다.
create table if not exists review_summary_jobs (
    id bigserial primary key,
    game_id bigint not null references games(id),
    language_code varchar(10) not null,
    spam_rule_version varchar(64),
    status varchar(20) not null,
    from_review_id bigint references external_reviews(id),
    to_review_id bigint references external_reviews(id),
    input_review_count integer not null default 0,
    chunk_count integer not null default 0,
    map_cache_hit integer not null default 0,
    map_cache_miss integer not null default 0,
    map_input_tokens integer not null default 0,
    map_output_tokens integer not null default 0,
    reduce_input_tokens integer not null default 0,
    reduce_output_tokens integer not null default 0,
    evidence_coverage_ratio numeric(5,2),
    error_message text,
    started_at timestamptz not null default now(),
    ended_at timestamptz,
    created_at timestamptz not null default now(),
    constraint ck_summary_job_status check (status in ('started', 'success', 'failed', 'partial'))
);

-- [테이블 역할] map 단계 청크 요약 저장
create table if not exists review_summary_chunks (
    id bigserial primary key,
    job_id bigint not null references review_summary_jobs(id) on delete cascade,
    chunk_no integer not null,
    input_review_count integer not null default 0,
    chunk_summary_text text not null,
    created_at timestamptz not null default now(),
    constraint uq_summary_chunk unique (job_id, chunk_no)
);

-- [테이블 역할] 게임/언어별 최종 AI 요약 결과 저장
-- is_current=true가 현재 프론트에 노출되는 버전이다.
create table if not exists game_review_summaries (
    id bigserial primary key,
    game_id bigint not null references games(id),
    language_code varchar(10) not null,
    job_id bigint references review_summary_jobs(id),
    summary_version integer not null,
    summary_text text not null,
    representative_reviews_json jsonb,
    sentiment_overall varchar(16),
    sentiment_score numeric(5,2),
    aspect_sentiment_json jsonb,
    pros_json jsonb,
    cons_json jsonb,
    keywords_json jsonb,
    steam_recommend_ratio numeric(5,2),
    metacritic_critic_avg numeric(5,2),
    metacritic_user_avg numeric(5,2),
    source_review_count integer not null default 0,
    covered_from_review_id bigint references external_reviews(id),
    covered_to_review_id bigint references external_reviews(id),
    is_current boolean not null default true,
    created_at timestamptz not null default now(),
    constraint uq_game_summary_version unique (game_id, language_code, summary_version),
    constraint ck_ratio_range check (
        steam_recommend_ratio is null or (steam_recommend_ratio >= 0 and steam_recommend_ratio <= 100)
    )
);

-- [JSONB 인덱싱 전략]
-- JSONB GIN 인덱스는 구조화된 필터/집계 요구가 실제로 생겼을 때만 추가한다.
-- JSONB는 유연하지만 쓰기 비용이 크므로, 조회 패턴이 확정되기 전에는 기본 B-Tree 인덱스와 정규 컬럼만으로 버틴다.
-- 
-- [대상] 리뷰 API가 비평가 소속사(outlet), 플레이 시간 구간(playtime_bracket) 같은 리뷰 메타 필드로 필터링/집계할 때 사용한다.
-- [원리] JSONB GIN은 문서 내부 키를 역색인처럼 찾아가므로, 특정 키/값 경로를 자주 조회할 때 전체 행 스캔을 줄일 수 있다. 다만 삽입/갱신 비용이 증가하므로 EXPLAIN ANALYZE로 실제 seq scan이 확인될 때만 추가한다.
--   CREATE INDEX idx_source_meta_gin ON external_reviews USING gin (source_meta_json);

-- [대상] 프론트엔드가 review_categories_json(예: spam/abuse/bug_report 태그)으로 필터링하는 API에서 사용한다.
-- [원리] review_categories_json 내부 키/배열 값 탐색 비용을 줄이기 위해 정식 GIN 인덱스를 제공한다.
create index if not exists idx_external_reviews_review_categories_gin
    on external_reviews using gin (review_categories_json);

-- [대상] 게임 메타 API가 가격(price_usd), 할인율(discount_percent), 태그(tags)처럼 플랫폼별 부가 메타데이터로 조회할 때 사용한다.
-- [원리] 플랫폼 메타는 키 종류가 넓을 수 있어 B-Tree 단일 컬럼보다 JSONB GIN이 적합하다. 단, 범위 검색이나 키 존재 검색 수요가 명확해졌을 때만 추가해야 쓰기 성능 저하를 막을 수 있다.
--   CREATE INDEX idx_platform_meta_gin ON game_platform_map USING gin (platform_meta_json);

-- 조회 요구가 없으면 JSONB GIN은 만들지 않는다. 필요할 때마다 실제 쿼리 패턴을 분석해 추가하는 것을 권장한다.

-- [대상] 프론트엔드의 특정 게임 최신 리뷰 API가 `game_id = ?` 조건으로 리뷰를 가장 최근 순으로 보여줄 때 사용한다.
-- [원리] B-Tree는 왼쪽부터 순서대로 탐색하므로 `game_id`를 선두에 두고, 그 뒤에 `reviewed_at desc`를 배치해 동일 게임 내에서 필터 후 정렬을 한 번에 처리하도록 설계했다.
create index if not exists idx_external_reviews_game_reviewed_at
    on external_reviews (game_id, reviewed_at desc);

-- [대상] 플랫폼+게임 필터 API가 `platform_id`와 `game_id`로 리뷰 목록을 좁혀서 조회할 때 사용한다.
-- [원리] 동등 조건이 먼저 오는 쿼리에서 두 컬럼을 선두 순서로 배치해 탐색 범위를 빠르게 줄인다. 두 값 모두 필터 키이므로 별도 정렬보다 조회 범위 축소가 우선이다.
create index if not exists idx_external_reviews_platform_game
    on external_reviews (platform_id, game_id);

-- [대상] 플랫폼/리뷰타입별 최신 n개 리뷰 API가 Steam, Metacritic critic, Metacritic user 목록을 각각 빠르게 뽑을 때 사용한다.
-- [원리] 앞의 `platform_id`, `review_type_id`, `game_id`는 필터 조건을 먼저 좁히는 용도이고, 마지막 `reviewed_at desc`는 좁혀진 결과를 최신순으로 바로 읽기 위한 정렬 키다.
create index if not exists idx_external_reviews_platform_type_game_reviewed
    on external_reviews (platform_id, review_type_id, game_id, reviewed_at desc);

-- [대상] 운영자가 플랫폼별 수집 실행 이력을 최근 시작 시각 순으로 확인하는 모니터링/장애 대응 화면에서 사용한다.
-- [원리] 먼저 `platform_id`로 대상 플랫폼을 좁히고, 그 안에서 `started_at desc`로 최신 실행부터 읽도록 배치해 최근 로그 조회 비용을 줄인다.
create index if not exists idx_ingestion_runs_platform_started_at
    on ingestion_runs (platform_id, started_at desc);

-- [대상] AI 증분 요약 잡이 마지막 요약 이후 추가된 리뷰만 delta 스캔할 때 사용한다.
-- [원리] `game_id`로 게임 범위를 먼저 고정하고, 그 안에서 `id` 증가 순으로 이어지는 새 리뷰 구간을 빠르게 순회하도록 설계했다.
create index if not exists idx_external_reviews_game_id_id
    on external_reviews (game_id, id);

-- [대상] 게임 상세 화면에서 현재 노출 중인 AI 요약을 1건만 읽어올 때 사용한다.
-- [원리] `game_id`와 `language_code`로 현재 버전 행을 즉시 찾게 하고, `where is_current = true` 부분 인덱스로 게임/언어별 현재본 1건만 존재하도록 강제한다.
create unique index if not exists uq_game_review_summaries_current_one
    on game_review_summaries (game_id, language_code)
    where is_current = true;

-- [대상] 게임별 요약 배치 잡의 최근 실행 이력과 상태를 운영 대시보드에서 조회할 때 사용한다.
-- [원리] `game_id`와 `language_code`로 잡을 먼저 묶고, `started_at desc`로 최신 잡부터 읽도록 해 최근 이력 조회를 빠르게 한다.
create index if not exists idx_review_summary_jobs_game_started
    on review_summary_jobs (game_id, language_code, started_at desc);

-- [대상] Map-Reduce 초기 적재에서 Steam 리뷰를 긍/부정 비율로 층화 추출할 때 사용한다.
-- [원리] 게임/언어/추천여부 필터 후 helpful/playtime 우선순위 정렬을 빠르게 수행하도록 부분 인덱스로 구성한다.
create index if not exists idx_reviews_sampling_steam
    on external_reviews (game_id, language_code, is_recommended, helpful_count desc, playtime_hours desc)
    where is_deleted = false;

-- [대상] Map-Reduce 초기 적재에서 Metacritic 리뷰를 점수 구간 비율로 층화 추출할 때 사용한다.
-- [원리] 게임/언어/정규화 점수 필터 후 helpful/playtime 우선순위 정렬을 빠르게 수행하도록 부분 인덱스로 구성한다.
create index if not exists idx_reviews_sampling_meta
    on external_reviews (game_id, language_code, normalized_score_100, helpful_count desc, playtime_hours desc)
    where is_deleted = false;

-- 기본 플랫폼 시드 데이터
-- 스키마를 여러 번 실행해도 중복 에러가 나지 않도록 처리
insert into platforms (code, name)
values
    ('steam', 'Steam'),
    ('metacritic', 'Metacritic')
on conflict (code) do nothing;

-- 기본 점수 체계 시드 데이터
-- 새 플랫폼 추가 시: 해당하는 scale_code를 여기에 추가
insert into score_scales (scale_code, min_value, max_value, description)
values
    ('binary', 0, 1, 'Recommended (1) / Not Recommended (0)'),
    ('10', 0, 10, '10-point scale (Metacritic user, GOG)'),
    ('100', 0, 100, '100-point scale (Metacritic critic, IGDB)')
on conflict (scale_code) do nothing;

-- 기본 리뷰 타입 시드 데이터
-- 새 플랫폼/타입 추가 시: 해당하는 type_code를 여기에 추가
insert into review_types (type_code, description)
values
    ('user', 'User/Community review'),
    ('critic', 'Professional critic/expert review')
on conflict (type_code) do nothing;

commit;