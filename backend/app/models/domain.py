from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, Numeric, Date, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import Base

# [마스터 테이블] Steam, Metacritic 같은 플랫폼 정보를 담는 테이블
class Platform(Base):
    __tablename__ = "platforms"
    id = Column(BigInteger, primary_key=True, index=True)
    code = Column(String(30), unique=True, nullable=False)  # 예: 'steam', 'metacritic'
    name = Column(String(100), nullable=False)              # 예: 'Steam'

# [마스터 테이블] 리뷰의 종류(전문가, 유저)를 구분하는 테이블
class ReviewType(Base):
    __tablename__ = "review_types"
    id = Column(BigInteger, primary_key=True, index=True)
    type_code = Column(String(30), unique=True, nullable=False) # 예: 'critic', 'user'

# [핵심 테이블] 우리가 수집한 게임 목록 테이블 (여러 플랫폼에 같은 게임이 있어도 하나로 묶어줌)
class Game(Base):
    __tablename__ = "games"
    id = Column(BigInteger, primary_key=True, index=True)
    canonical_title = Column(String(255), nullable=False)       # 예: "Grand Theft Auto V"
    normalized_title = Column(String(255), unique=True, nullable=False) # 예: "grand-theft-auto-v" (중복 판별용)
    release_date = Column(Date)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

# [매핑 테이블] 내부 게임 ID와 각 플랫폼(스팀 등)의 고유 ID를 연결해 주는 테이블
class GamePlatformMap(Base):
    __tablename__ = "game_platform_map"
    id = Column(BigInteger, primary_key=True, index=True)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    platform_id = Column(BigInteger, ForeignKey("platforms.id"), nullable=False)
    external_game_id = Column(String(120), nullable=False)      # 예: 스팀의 '271590' 번호
    crawled_at = Column(DateTime(timezone=True))
    platform_meta_json = Column(JSONB)  # 각 플랫폼별 특수한 메타데이터(통계, 가격 등)를 유연하게 저장하는 JSON 공간

# [운영 테이블] 크롤러가 데이터를 잘 수집했는지 성공/실패 여부와 개수를 기록하는 로그 테이블
class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id = Column(BigInteger, primary_key=True, index=True)
    platform_id = Column(BigInteger, ForeignKey("platforms.id"), nullable=False)
    game_id = Column(BigInteger, ForeignKey("games.id"))
    status = Column(String(20), nullable=False)                 # 예: 'started', 'success'
    started_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    ended_at = Column(DateTime(timezone=True))
    inserted_count = Column(Integer, default=0)                 # 성공적으로 저장한 리뷰 개수

# [핵심 테이블] 크롤링해 온 수만 개의 실제 리뷰 데이터가 저장되는 테이블
class ExternalReview(Base):
    __tablename__ = "external_reviews"
    id = Column(BigInteger, primary_key=True, index=True)
    platform_id = Column(BigInteger, ForeignKey("platforms.id"), nullable=False)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    ingestion_run_id = Column(BigInteger, ForeignKey("ingestion_runs.id"))
    
    source_review_id = Column(String(150))
    # 중복 저장을 막기 위해 작성자+날짜+본문을 암호화(해싱)해서 만든 절대 고유 키입니다.
    source_review_key = Column(String(255), nullable=False) 
    
    review_type_id = Column(BigInteger, ForeignKey("review_types.id"), nullable=False)
    author_name = Column(String(255))
    is_recommended = Column(Boolean)               # 스팀의 추천/비추천 (True/False)
    score_raw = Column(String(50))                 # 메타크리틱의 점수 (예: "95")
    language_code = Column(String(10))
    review_text_raw = Column(Text)
    review_text_clean = Column(Text, nullable=False) # 불순물을 제거한 실제 리뷰 본문
    reviewed_at = Column(DateTime(timezone=True))
    playtime_hours = Column(Numeric(8, 2))         # 스팀의 플레이 타임 (예: 120.5시간)
    source_meta_json = Column(JSONB)
    is_deleted = Column(Boolean, default=False)