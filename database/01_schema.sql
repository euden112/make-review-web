-- Sprint 1 schema for review ingestion
-- Target: PostgreSQL 13+
-- 목적: Steam/Metacritic 리뷰를 중복 없이 적재하고, 조회/운영 추적이 가능하도록 구조화
-- 핵심: PK/FK/UNIQUE/CHECK/INDEX로 데이터 정합성과 성능을 DB 레벨에서 보장한다.
-- 확장: 플랫폼별 점수 체계 차이(steam binary, critic 100, user 10)와
--      AI 증분 요약(map-reduce) 운영을 지원한다.
-- TODO(Crawling): metacritic 크롤러 출력 파일명을 단일 기준으로 통일할 것.
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
-- games.id(내부 통합 게임 ID)와 각 플랫폼의 게임 ID를 매핑하는 테이블.
-- 예: games.id=10 <-> Steam app_id=570, Metacritic slug='dota-2'
-- FK로 연결해 존재하지 않는 게임/플랫폼 값이 들어오는 것을 방지한다.
create table if not exists game_platform_map (
    id bigserial primary key,
    game_id bigint not null references games(id),
    platform_id bigint not null references platforms(id),
    external_game_id varchar(120) not null,
    external_game_url text,
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
    -- score_100: 적재 코드가 직접 넣는 점수(선택)
    -- normalized_score_100: DB 규칙으로 계산되는 공통 100점 축 점수
    score_100 numeric(5,2),
    score_scale_id bigint references score_scales(id),
    normalized_score_100 numeric(5,2),
    language_code varchar(10),
    review_text_raw text,
    review_text_clean text not null,
    reviewed_at timestamptz,
    helpful_count integer not null default 0,
    playtime_hours numeric(8,2),
    source_meta_json jsonb,
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
    v_scale_code varchar(20);
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

    -- score_scale_id이 설정되었으면 해당 scale_code 조회
    if new.score_scale_id is not null then
        select scale_code into v_scale_code
        from score_scales
        where id = new.score_scale_id;
    end if;

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
        -- 향후 플랫폼 확장 시: score_scale_id가 명시되어야 함, 규칙 미정이면 null 허용
        new.normalized_score_100 := coalesce(new.normalized_score_100, null);
    end if;

    return new;
end;
$$;

drop trigger if exists trg_normalize_review_score on external_reviews;
create trigger trg_normalize_review_score
before insert or update on external_reviews
for each row
execute function fn_normalize_review_score();

-- [테이블 역할] 게임별 증분 요약 커서
-- 마지막으로 AI 요약에 반영된 review_id를 기록해 다음 배치에서 delta만 처리한다.
create table if not exists game_summary_cursor (
    game_id bigint primary key references games(id),
    last_summarized_review_id bigint references external_reviews(id),
    last_summary_version integer not null default 0,
    updated_at timestamptz not null default now()
);

-- [테이블 역할] AI 요약 실행 이력
-- 배치 작업 상태 추적: 시작/종료, 처리 범위, 청크 개수, 에러 메시지 기록
-- 운영 정책: 토큰 비용 최소화를 위해 요약 전략은 map_reduce로 고정한다.
create table if not exists review_summary_jobs (
    id bigserial primary key,
    game_id bigint not null references games(id),
    status varchar(20) not null,
    from_review_id bigint references external_reviews(id),
    to_review_id bigint references external_reviews(id),
    input_review_count integer not null default 0,
    chunk_count integer not null default 0,
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

-- [테이블 역할] 게임별 최종 AI 요약 결과 저장
-- is_current=true가 현재 프론트에 노출되는 버전이다.
create table if not exists game_review_summaries (
    id bigserial primary key,
    game_id bigint not null references games(id),
    job_id bigint references review_summary_jobs(id),
    summary_version integer not null,
    summary_text text not null,
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
    constraint uq_game_summary_version unique (game_id, summary_version),
    constraint ck_ratio_range check (
        steam_recommend_ratio is null or (steam_recommend_ratio >= 0 and steam_recommend_ratio <= 100)
    )
);

-- [JSONB 인덱싱 전략]
-- JSONB GIN 인덱스는 조회 요구가 명확할 때만 추가하여 비용(저장, 삽입 성능)을 최소화한다.
-- 
-- 1. source_meta_json (external_reviews) 인덱스 추가 타이밍:
--    - API가 source_meta_json -> '$.outlet' (비평가 소속사)로 필터링하는 경우
--    - API가 source_meta_json -> '$.playtime_bracket' (플레이 시간 범위)로 집계하는 경우
--    - 추가 시점: EXPLAIN ANALYZE로 seq scan on external_reviews 확인 후
--    CREATE INDEX idx_source_meta_gin ON external_reviews USING gin (source_meta_json);
--
-- 2. platform_meta_json (game_platform_map) 인덱스 추가 타이밍:
--    - API가 platform_meta_json -> '$.price_usd' 범위 검색 등으로 조회하는 경우
--    - 추가 시점: EXPLAIN ANALYZE로 seq scan on game_platform_map 확인 후
--    CREATE INDEX idx_platform_meta_gin ON game_platform_map USING gin (platform_meta_json);
--
-- 3. 조회 요구 없으면 GIN 생략: B-tree 인덱스만으로 충분히 성능이 나오는 경우, JSONB 인덱스는 추가하지 않는다.

-- [인덱스 역할] 특정 게임의 최신 리뷰 조회 성능 최적화
create index if not exists idx_external_reviews_game_reviewed_at
    on external_reviews (game_id, reviewed_at desc);

-- [인덱스 역할] 플랫폼+게임 조건 필터 조회 성능 최적화
create index if not exists idx_external_reviews_platform_game
    on external_reviews (platform_id, game_id);

-- [인덱스 역할] 플랫폼별(review_type_id 포함) 최신 n개 조회 성능 최적화
create index if not exists idx_external_reviews_platform_type_game_reviewed
    on external_reviews (platform_id, review_type_id, game_id, reviewed_at desc);

-- [인덱스 역할] 운영 로그를 최근 실행 순으로 빠르게 조회
create index if not exists idx_ingestion_runs_platform_started_at
    on ingestion_runs (platform_id, started_at desc);

-- [인덱스 역할] 증분 요약 시 review_id 범위 스캔 가속
create index if not exists idx_external_reviews_game_id_id
    on external_reviews (game_id, id);

-- [인덱스 역할] 게임별 현재 요약 버전 조회 가속 + 현재 버전 단일성 보장
create unique index if not exists uq_game_review_summaries_current_one
    on game_review_summaries (game_id)
    where is_current = true;

-- [인덱스 역할] 요약 잡 이력 최근 조회 가속
create index if not exists idx_review_summary_jobs_game_started
    on review_summary_jobs (game_id, started_at desc);

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