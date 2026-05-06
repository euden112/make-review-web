-- Sprint 4: playtime_analyses, critic_summaries 테이블 추가
-- Regional Pipeline 제거 후 플레이타임별 여론 및 비평가 반응 분석으로 대체

-- ============================================================
-- 플레이타임별 여론 분석 테이블
-- ============================================================
CREATE TABLE IF NOT EXISTS playtime_analyses (
    id                  SERIAL PRIMARY KEY,
    game_id             INTEGER REFERENCES games(id),

    -- 버킷 경계값 (퍼센타일 기반, 게임마다 다름)
    bucket_thresholds   JSONB NOT NULL,
    -- {"early_max": 25.0, "mid_max": 120.0}

    -- 초반 버킷 (0 ~ p33)
    early_summary       TEXT,
    early_sentiment     VARCHAR(16),
    early_score         FLOAT,
    early_pros          JSONB,
    early_cons          JSONB,
    early_keywords      JSONB,
    early_review_count  INTEGER,

    -- 중반 버킷 (p33 ~ p66)
    mid_summary         TEXT,
    mid_sentiment       VARCHAR(16),
    mid_score           FLOAT,
    mid_pros            JSONB,
    mid_cons            JSONB,
    mid_keywords        JSONB,
    mid_review_count    INTEGER,

    -- 후반 버킷 (p66+)
    late_summary        TEXT,
    late_sentiment      VARCHAR(16),
    late_score          FLOAT,
    late_pros           JSONB,
    late_cons           JSONB,
    late_keywords       JSONB,
    late_review_count   INTEGER,

    created_at          TIMESTAMP DEFAULT now(),
    updated_at          TIMESTAMP DEFAULT now(),
    UNIQUE (game_id)
);

-- ============================================================
-- 비평가 반응 요약 테이블
-- ============================================================
CREATE TABLE IF NOT EXISTS critic_summaries (
    id              SERIAL PRIMARY KEY,
    game_id         INTEGER REFERENCES games(id),

    summary         TEXT,
    sentiment       VARCHAR(16),
    score           FLOAT,
    pros            JSONB,
    cons            JSONB,
    keywords        JSONB,
    review_count    INTEGER,

    created_at      TIMESTAMP DEFAULT now(),
    updated_at      TIMESTAMP DEFAULT now(),
    UNIQUE (game_id)
);
