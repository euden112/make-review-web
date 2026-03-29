-- Sprint 1 schema for review ingestion
-- Target: PostgreSQL 13+
-- 목적: Steam/Metacritic 리뷰를 중복 없이 적재하고, 조회/운영 추적이 가능하도록 구조화
-- 비유: 여러 시트가 연결된 "규칙이 엄격한 엑셀"로 생각하면 이해가 쉽다.
-- 핵심: PK/FK/UNIQUE/CHECK/INDEX로 데이터 정합성과 성능을 DB 레벨에서 보장한다.

begin;

-- 트랜잭션 시작: 중간에 하나라도 실패하면 전체 롤백되고,
-- 마지막 commit까지 성공했을 때만 반영된다.

-- [테이블 역할] 플랫폼 마스터
-- Steam, Metacritic 같은 "리뷰 출처"를 표준 코드로 관리하는 기준 테이블.
create table if not exists platforms (
    id bigserial primary key,
    code varchar(30) not null unique,
    name varchar(100) not null,
    created_at timestamptz not null default now()
);

-- [테이블 역할] 게임 마스터
-- 서비스 기준 게임 목록. 플랫폼이 달라도 동일 게임을 하나의 엔터티로 관리한다.
-- normalized_title은 검색과 중복 방지를 위한 정규화 키.
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
-- 우리 게임 ID와 플랫폼별 외부 ID(예: Steam app_id, Metacritic slug)를 연결하는 다리.
-- FK로 연결해 존재하지 않는 게임/플랫폼 값이 들어오는 것을 방지한다.
create table if not exists game_platform_map (
    id bigserial primary key,
    game_id bigint not null references games(id),
    platform_id bigint not null references platforms(id),
    external_game_id varchar(120) not null,
    external_game_url text,
    crawled_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint uq_game_platform_external_game unique (platform_id, external_game_id),
    constraint uq_game_platform_once unique (game_id, platform_id)
);

-- [테이블 역할] 운영 로그(ingestion runs)
-- 수집/적재 실행 1회 단위의 영수증.
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
create table if not exists external_reviews (
    id bigserial primary key,
    platform_id bigint not null references platforms(id),
    game_id bigint not null references games(id),
    ingestion_run_id bigint references ingestion_runs(id),

    -- 원천 식별 정보
    -- source_review_id: 플랫폼이 제공하는 원본 리뷰 ID (없을 수 있음)
    -- source_review_key: 우리 쪽에서 만든 안정적인 중복 방지 키 (필수)
    source_review_id varchar(150),
    source_review_key varchar(255) not null,

    -- 원천 데이터
    review_type varchar(20) not null,
    author_name varchar(255),
    is_recommended boolean,
    score_raw varchar(50),
    score_100 numeric(5,2),
    language_code varchar(10),
    review_text_raw text,
    review_text_clean text not null,
    reviewed_at timestamptz,
    helpful_count integer not null default 0,
    playtime_hours numeric(8,2),

    is_deleted boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    constraint ck_review_type check (review_type in ('user', 'critic')),
    constraint ck_review_text_non_empty check (length(trim(review_text_clean)) > 0),
    -- 같은 플랫폼/게임에서 동일 리뷰는 1건만 유지
    constraint uq_external_review_key unique (platform_id, game_id, source_review_key)
);

-- [인덱스 역할] 특정 게임의 최신 리뷰 조회 성능 최적화
create index if not exists idx_external_reviews_game_reviewed_at
    on external_reviews (game_id, reviewed_at desc);

-- [인덱스 역할] 플랫폼+게임 조건 필터 조회 성능 최적화
create index if not exists idx_external_reviews_platform_game
    on external_reviews (platform_id, game_id);

-- [인덱스 역할] 운영 로그를 최근 실행 순으로 빠르게 조회
create index if not exists idx_ingestion_runs_platform_started_at
    on ingestion_runs (platform_id, started_at desc);

-- 기본 플랫폼 시드 데이터
-- 스키마를 여러 번 실행해도 중복 에러가 나지 않도록 처리
insert into platforms (code, name)
values
    ('steam', 'Steam'),
    ('metacritic', 'Metacritic')
on conflict (code) do nothing;

commit;
