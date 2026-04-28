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

# 수정됨(Sprint 3): GameSummaryCursor 구조가 Sprint 3에서 확장되었습니다.
# - summary_type 필드가 추가되어 unified/regional 구분을 명시적으로 기록합니다.
class GameSummaryCursor(Base):
    """파이프라인 상태 추적 커서 (게임별·모드별·언어별)
    
    Sprint 3: 요약 생성 진행 상황 기록
    - (game_id, language_code) PK 유지 (backward compatibility)
    - summary_type: 구분 메타 필드 (추가 정보, PK에 미포함)
    - last_summarized_review_id: 증분 파이프라인용 (다음 조회 시작점)
    - last_summary_version: 현재 요약본 버전 (중복 생성 방지)
    
    PK 설계 (m005까지 유지):
    - language_code="unified": unified 모드 커서
    - language_code="ko": regional 모드 커서 (한국어)
    """
    __tablename__ = "game_summary_cursor"
    game_id = Column(BigInteger, ForeignKey("games.id"), primary_key=True)
    language_code = Column(String(10), primary_key=True)  # 기존 PK 유지 ('unified' | 언어코드)
    summary_type = Column(String(16), nullable=False, default="unified")  # Sprint 3: 'unified' | 'regional'
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
    language_code = Column(String(10), nullable=False)
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
    # Sprint 3: Gemini 신뢰도 지표 (ai_service.py의 compute_gemini_reliability() 참고)
    schema_compliance = Column(Numeric(4, 3))       # 9개 필수 필드 채움 비율
    hallucination_score = Column(Numeric(4, 3))     # 인용 review_id 존재 비율
    sentiment_consistency = Column(Integer)          # label vs score 범위 일치 (0|1)
    anchor_deviation = Column(Numeric(4, 3))         # |AI score - steam_ratio| / 100
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

# 수정됨(Sprint 3): GameReviewSummary에 summary_type/review_language 및 품질 지표들이 추가되었습니다.
class GameReviewSummary(Base):
    """게임 리뷰 AI 요약본 저장
    
    Sprint 3: 통합/지역별 요약 구분 및 품질 지표 추가
    
    스키마 진화 (마이그레이션 단계):
    - m001: summary_type, review_language 추가 + partial indexes 생성
    - m002-m003: 신뢰도 지표 컬럼 추가
    - m004-m005: language_code 제거 (정식 필드로 완전 전환)
    
    조회 로직 (스키마별):
    - Unified 요약 (is_current=TRUE):
      SELECT * WHERE game_id=X AND summary_type='unified' AND review_language IS NULL
    - Regional 요약 (is_current=TRUE):
      SELECT * WHERE game_id=X AND summary_type='regional' AND review_language='ko'
    
    Backward Compatibility:
    - language_code 필드: m005 이후 제거 (현재는 역호환용)
    - API: _serialize_summary() 함수에서 language_code 자동 생성
    """
    __tablename__ = "game_review_summaries"
    id = Column(BigInteger, primary_key=True, index=True)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    language_code = Column(String(10), nullable=False)  # 레거시 필드 (m005 제거 예정)
    # Sprint 3: 요약 모드 구분 필드
    summary_type = Column(String(16), nullable=False, default="unified")  # 'unified' | 'regional'
    review_language = Column(String(10), nullable=True)                    # NULL (unified) | 'en'/'ko'/'zh' (regional)
    job_id = Column(BigInteger, ForeignKey("review_summary_jobs.id"))
    summary_version = Column(Integer, nullable=False)
    summary_text = Column(Text, nullable=False)
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
    # Sprint 3: 고유성 보장 전략 (PostgreSQL NULL 제약 우회)
    # - 기존: UNIQUE(game_id, language_code, summary_version) → NULL 문제
    # - 변경: Partial Index 2개 사용
    #   * uq_game_summary_version_unified (m001):
    #     (game_id, summary_type, summary_version) WHERE review_language IS NULL
    #   * uq_game_summary_version_regional (m001):
    #     (game_id, summary_type, review_language, summary_version) WHERE review_language IS NOT NULL
    # 마이그레이션: 04_migration_sprint3_m001.sql 참고