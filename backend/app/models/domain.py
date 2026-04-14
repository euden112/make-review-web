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
    __tablename__ = "game_summary_cursor"
    game_id = Column(BigInteger, ForeignKey("games.id"), primary_key=True)
    language_code = Column(String(10), primary_key=True)  # 👈 중요: 복합 프라이머리 키로 추가
    last_summarized_review_id = Column(BigInteger, ForeignKey("external_reviews.id"))
    last_summary_version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class ReviewSummaryJob(Base):
    __tablename__ = "review_summary_jobs"
    id = Column(BigInteger, primary_key=True, index=True)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    language_code = Column(String(10), nullable=False) # 👈 추가
    status = Column(String(20), nullable=False)
    from_review_id = Column(BigInteger, ForeignKey("external_reviews.id"))
    to_review_id = Column(BigInteger, ForeignKey("external_reviews.id"))
    input_review_count = Column(Integer, default=0)
    chunk_count = Column(Integer, default=0)
    map_input_tokens = Column(Integer, default=0)
    map_output_tokens = Column(Integer, default=0)
    reduce_input_tokens = Column(Integer, default=0)
    reduce_output_tokens = Column(Integer, default=0)
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
    __tablename__ = "game_review_summaries"
    id = Column(BigInteger, primary_key=True, index=True)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    language_code = Column(String(10), nullable=False) # 👈 추가
    job_id = Column(BigInteger, ForeignKey("review_summary_jobs.id"))
    summary_version = Column(Integer, nullable=False)
    summary_text = Column(Text, nullable=False)
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
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint('game_id', 'language_code', 'summary_version', name='uq_game_summary_version'), # 👈 복합 유니크 키 제약 수정
    )