from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, Numeric, Date, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import Base

# [마스터 테이블] Steam, Metacritic 같은 플랫폼 정보를 담는 테이블
class Platform(Base):
    __tablename__ = "platforms"
    id = Column(BigInteger, primary_key=True, index=True)
    code = Column(String(30), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

# [마스터 테이블] 플랫폼별 점수 체계 (새로 추가됨)
class ScoreScale(Base):
    __tablename__ = "score_scales"
    id = Column(BigInteger, primary_key=True, index=True)
    scale_code = Column(String(20), unique=True, nullable=False)
    min_value = Column(Numeric, nullable=False)
    max_value = Column(Numeric, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

# [마스터 테이블] 리뷰의 종류(전문가, 유저)를 구분하는 테이블
class ReviewType(Base):
    __tablename__ = "review_types"
    id = Column(BigInteger, primary_key=True, index=True)
    type_code = Column(String(30), unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

# [핵심 테이블] 우리가 수집한 게임 목록 테이블
class Game(Base):
    __tablename__ = "games"
    id = Column(BigInteger, primary_key=True, index=True)
    canonical_title = Column(String(255), nullable=False)
    normalized_title = Column(String(255), unique=True, nullable=False)
    release_date = Column(Date)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

# [매핑 테이블] 내부 게임 ID와 각 플랫폼의 고유 ID를 연결해 주는 테이블
class GamePlatformMap(Base):
    __tablename__ = "game_platform_map"
    id = Column(BigInteger, primary_key=True, index=True)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    platform_id = Column(BigInteger, ForeignKey("platforms.id"), nullable=False)
    external_game_id = Column(String(120), nullable=False)
    crawled_at = Column(DateTime(timezone=True))
    platform_meta_json = Column(JSONB)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint('platform_id', 'external_game_id', name='uq_game_platform_external_game'),
        UniqueConstraint('game_id', 'platform_id', name='uq_game_platform_once'),
    )

# [운영 테이블] 크롤러 수집 성공/실패 여부와 개수를 기록하는 로그 테이블
class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id = Column(BigInteger, primary_key=True, index=True)
    platform_id = Column(BigInteger, ForeignKey("platforms.id"), nullable=False)
    game_id = Column(BigInteger, ForeignKey("games.id"))
    status = Column(String(20), nullable=False)
    started_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    ended_at = Column(DateTime(timezone=True))
    fetched_count = Column(Integer, default=0, nullable=False)
    inserted_count = Column(Integer, default=0, nullable=False)
    updated_count = Column(Integer, default=0, nullable=False)
    error_count = Column(Integer, default=0, nullable=False)
    error_message = Column(Text)

# [핵심 테이블] 크롤링해 온 실제 리뷰 데이터가 저장되는 테이블
class ExternalReview(Base):
    __tablename__ = "external_reviews"
    id = Column(BigInteger, primary_key=True, index=True)
    platform_id = Column(BigInteger, ForeignKey("platforms.id"), nullable=False)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    ingestion_run_id = Column(BigInteger, ForeignKey("ingestion_runs.id"))
    
    source_review_id = Column(String(150))
    source_review_key = Column(String(255), nullable=False) 
    
    review_type_id = Column(BigInteger, ForeignKey("review_types.id"), nullable=False)
    author_name = Column(String(255))
    is_recommended = Column(Boolean)
    score_raw = Column(String(50))
    score_scale_id = Column(BigInteger, ForeignKey("score_scales.id"))
    normalized_score_100 = Column(Numeric(5, 2))
    language_code = Column(String(10))
    review_text_raw = Column(Text)
    review_text_clean = Column(Text, nullable=False)
    reviewed_at = Column(DateTime(timezone=True))
    helpful_count = Column(Integer, default=0, nullable=False)
    playtime_hours = Column(Numeric(8, 2))
    source_meta_json = Column(JSONB)
    review_categories_json = Column(JSONB)  # [{"category": "그래픽", "sentiment": "positive"}, ...]
    is_deleted = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint('platform_id', 'game_id', 'source_review_key', name='uq_external_review_key'),
    )
    
# ==============================================================================
# [AI 요약 파이프라인 관련 테이블]
# ==============================================================================

class GameSummaryCursor(Base):
    """파이프라인 상태 추적 커서 (게임별)

    - (game_id, language_code) PK
    - summary_type: 구분 메타 필드
    - last_summarized_review_id: 증분 파이프라인용 (다음 조회 시작점)
    - last_summary_version: 현재 요약본 버전 (중복 생성 방지)
    """
    __tablename__ = "game_summary_cursor"
    game_id = Column(BigInteger, ForeignKey("games.id"), primary_key=True)
    language_code = Column(String(10), primary_key=True)  # 기존 PK 유지 ('unified' | 언어코드)
    summary_type = Column(String(16), nullable=False, default="unified")
    last_summarized_review_id = Column(BigInteger, ForeignKey("external_reviews.id"))
    last_summary_version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

# 수정됨(Sprint 3): ReviewSummaryJob에 토큰/신뢰도 필드가 추가되었습니다.
class ReviewSummaryJob(Base):
    """파이프라인 실행 로그 및 메트릭 저장
    
    Sprint 3: 토큰 추적 및 신뢰도 계산 추가
    - map_*_tokens: Ollama (로컬 LLM) 토큰 사용량
    - reduce_*_tokens: Gemini API 토큰 사용량
    - schema_compliance: 9개 필드 채움 비율 (0.0~1.0)
    - hallucination_score: 인용된 review_id 존재 비율 (0.0~1.0)
    - sentiment_consistency: 레이블 vs 점수 범위 일치 (0|1)
    - anchor_deviation: |sentiment_score - steam_ratio| / 100
    
    활용: 비용 추적, 캐시 효율 모니터링, 결과 품질 평가
    """
    __tablename__ = "review_summary_jobs"
    id = Column(BigInteger, primary_key=True, index=True)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    status = Column(String(20), nullable=False)
    from_review_id = Column(BigInteger, ForeignKey("external_reviews.id"))
    to_review_id = Column(BigInteger, ForeignKey("external_reviews.id"))
    input_review_count = Column(Integer, default=0)
    chunk_count = Column(Integer, default=0)
    map_cache_hit = Column(Integer, default=0)
    map_cache_miss = Column(Integer, default=0)
    map_input_tokens = Column(Integer, default=0)      # Sprint 3: Ollama prompt_eval_count
    map_output_tokens = Column(Integer, default=0)     # Sprint 3: Ollama eval_count
    reduce_input_tokens = Column(Integer, default=0)   # Sprint 3: Gemini usage_metadata.prompt_token_count
    reduce_output_tokens = Column(Integer, default=0)  # Sprint 3: Gemini usage_metadata.candidates_token_count
    spam_rule_version = Column(String(64))
    evidence_coverage_ratio = Column(Numeric(5, 2))
    # Sprint 3: Reduce(Groq) 신뢰도 지표 (ai_service.py의 compute_reduce_reliability() 참고)
    schema_compliance = Column(Numeric(4, 3))       # 9개 필수 필드 채움 비율
    hallucination_score = Column(Numeric(4, 3))     # 인용 review_id 존재 비율
    sentiment_consistency = Column(Integer)          # label vs score 범위 일치 (0|1)
    anchor_deviation = Column(Numeric(4, 3))         # |AI score - steam_ratio| / 100
    failure_reasons_json = Column(JSONB)              # chunk별 실패 사유 카운트 {timeout: 0, parse_error: 0, format_invalid: 0, call_failed: 0}
    error_message = Column(Text)
    started_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    ended_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class ReviewSummaryChunk(Base):
    __tablename__ = "review_summary_chunks"
    id = Column(BigInteger, primary_key=True, index=True)
    job_id = Column(BigInteger, ForeignKey("review_summary_jobs.id", ondelete="CASCADE"), nullable=False)
    chunk_no = Column(Integer, nullable=False)
    input_review_count = Column(Integer, default=0)
    chunk_summary_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint('job_id', 'chunk_no', name='uq_summary_chunk'),
    )

class GameReviewSummary(Base):
    """게임 리뷰 AI 요약본 저장

    조회 로직:
    - Unified 요약: summary_type='unified' AND review_language IS NULL AND is_current=TRUE
    """
    __tablename__ = "game_review_summaries"
    id = Column(BigInteger, primary_key=True, index=True)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    summary_type = Column(String(16), nullable=False, default="unified")
    review_language = Column(String(10), nullable=True)
    job_id = Column(BigInteger, ForeignKey("review_summary_jobs.id"))
    job = relationship("ReviewSummaryJob", lazy="select")
    summary_version = Column(Integer, nullable=False)
    summary_text = Column(Text, nullable=False)
    one_liner = Column(Text, nullable=True)
    representative_reviews_json = Column(JSONB)
    sentiment_overall = Column(String(16))
    sentiment_score = Column(Numeric(5, 2))
    aspect_sentiment_json = Column(JSONB)
    pros_json = Column(JSONB)
    cons_json = Column(JSONB)
    keywords_json = Column(JSONB)
    steam_recommend_ratio = Column(Numeric(5, 2))
    metacritic_critic_avg = Column(Numeric(5, 2))
    metacritic_user_avg = Column(Numeric(5, 2))
    source_review_count = Column(Integer, default=0)
    covered_from_review_id = Column(BigInteger, ForeignKey("external_reviews.id"))
    covered_to_review_id = Column(BigInteger, ForeignKey("external_reviews.id"))
    is_current = Column(Boolean, nullable=False, default=True)
    # Sprint 3: 요약 품질 지표 (ai_service.py에서 계산)
    # - sentiment_alignment: |sentiment_score - steam_recommend_ratio| 일치도 (1.0이 최고)
    # - coverage_ratio: source_review_count / total_reviews_in_db (요약이 커버한 리뷰 비율)
    # - staleness_ratio: new_reviews_since_last / total_reviews_in_db (요약이 얼마나 오래됐는지)
    # - semantic_similarity_score: 요약과 원본 리뷰 의미 유사도 (paraphrase-multilingual-MiniLM)
    sentiment_alignment = Column(Numeric(5, 4))        # 0.0~1.0
    coverage_ratio = Column(Numeric(5, 4))             # 0.0~1.0
    staleness_ratio = Column(Numeric(5, 4))            # 0.0~1.0
    semantic_similarity_score = Column(Numeric(5, 4))  # 0.0~1.0
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    # 마이그레이션: 04_migration_sprint3_m001.sql 참고


# ==============================================================================
# [Sprint 4] 플레이타임별 여론 분석 및 비평가 반응 테이블
# ==============================================================================

class PlaytimeAnalysis(Base):
    """플레이타임 버킷별(early/mid/late) AI 요약 저장

    버킷 경계는 게임별 리뷰어 플레이타임 분포의 퍼센타일(p33, p66) 기반.
    각 버킷에 리뷰가 30건 미만이면 해당 필드는 NULL.
    마이그레이션: 08_migration_sprint4.sql
    """
    __tablename__ = "playtime_analyses"
    id                  = Column(BigInteger, primary_key=True, index=True)
    game_id             = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    bucket_thresholds   = Column(JSONB, nullable=False)

    early_summary       = Column(Text)
    early_sentiment     = Column(String(16))
    early_score         = Column(Numeric(5, 2))
    early_pros          = Column(JSONB)
    early_cons          = Column(JSONB)
    early_keywords      = Column(JSONB)
    early_review_count  = Column(Integer)

    mid_summary         = Column(Text)
    mid_sentiment       = Column(String(16))
    mid_score           = Column(Numeric(5, 2))
    mid_pros            = Column(JSONB)
    mid_cons            = Column(JSONB)
    mid_keywords        = Column(JSONB)
    mid_review_count    = Column(Integer)

    late_summary        = Column(Text)
    late_sentiment      = Column(String(16))
    late_score          = Column(Numeric(5, 2))
    late_pros           = Column(JSONB)
    late_cons           = Column(JSONB)
    late_keywords       = Column(JSONB)
    late_review_count   = Column(Integer)

    created_at          = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at          = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint('game_id', name='uq_playtime_analysis_game'),
    )


class CriticSummary(Base):
    """비평가(Metacritic critic) 리뷰 AI 요약 저장

    유저 여론과 독립된 섹션으로, 출시 당시 전문가 평가를 나타냄.
    critic 리뷰가 10건 미만이면 생성하지 않음(NULL).
    마이그레이션: 08_migration_sprint4.sql
    """
    __tablename__ = "critic_summaries"
    id              = Column(BigInteger, primary_key=True, index=True)
    game_id         = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    summary         = Column(Text)
    sentiment       = Column(String(16))
    score           = Column(Numeric(5, 2))
    pros            = Column(JSONB)
    cons            = Column(JSONB)
    keywords        = Column(JSONB)
    review_count    = Column(Integer)
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at      = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint('game_id', name='uq_critic_summary_game'),
    )


class UserSummary(Base):
    """유저 리뷰 AI 요약 저장 (B안: unified body 폐지 후 신설)

    user 청크(비-critic) 기반으로만 생성되어 평론가 톤·논조와 독립.
    마이그레이션: 13_migration_user_summary_split.sql
    """
    __tablename__ = "user_summaries"
    id              = Column(BigInteger, primary_key=True, index=True)
    game_id         = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    summary         = Column(Text)
    sentiment       = Column(String(16))
    score           = Column(Numeric(5, 2))
    pros            = Column(JSONB)
    cons            = Column(JSONB)
    keywords        = Column(JSONB)
    review_count    = Column(Integer)
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at      = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint('game_id', name='uq_user_summary_game'),
    )