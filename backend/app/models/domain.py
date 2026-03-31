from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, Numeric, Date, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import Base

class Platform(Base):
    __tablename__ = "platforms"
    id = Column(BigInteger, primary_key=True, index=True)
    code = Column(String(30), unique=True, nullable=False)
    name = Column(String(100), nullable=False)

class ReviewType(Base):
    __tablename__ = "review_types"
    id = Column(BigInteger, primary_key=True, index=True)
    type_code = Column(String(30), unique=True, nullable=False)

class Game(Base):
    __tablename__ = "games"
    id = Column(BigInteger, primary_key=True, index=True)
    canonical_title = Column(String(255), nullable=False)
    normalized_title = Column(String(255), unique=True, nullable=False)
    release_date = Column(Date)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class GamePlatformMap(Base):
    __tablename__ = "game_platform_map"
    id = Column(BigInteger, primary_key=True, index=True)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    platform_id = Column(BigInteger, ForeignKey("platforms.id"), nullable=False)
    external_game_id = Column(String(120), nullable=False)
    crawled_at = Column(DateTime(timezone=True))
    platform_meta_json = Column(JSONB)

class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id = Column(BigInteger, primary_key=True, index=True)
    platform_id = Column(BigInteger, ForeignKey("platforms.id"), nullable=False)
    game_id = Column(BigInteger, ForeignKey("games.id"))
    status = Column(String(20), nullable=False)
    started_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    ended_at = Column(DateTime(timezone=True))
    inserted_count = Column(Integer, default=0)

class ExternalReview(Base):
    __tablename__ = "external_reviews"
    id = Column(BigInteger, primary_key=True, index=True)
    platform_id = Column(BigInteger, ForeignKey("platforms.id"), nullable=False)
    game_id = Column(BigInteger, ForeignKey("games.id"), nullable=False)
    ingestion_run_id = Column(BigInteger, ForeignKey("ingestion_runs.id"))
    source_review_id = Column(String(150))
    source_review_key = Column(String(255), nullable=False) # 중복 방지 해시 키
    review_type_id = Column(BigInteger, ForeignKey("review_types.id"), nullable=False)
    author_name = Column(String(255))
    is_recommended = Column(Boolean)
    score_raw = Column(String(50))
    language_code = Column(String(10))
    review_text_raw = Column(Text)
    review_text_clean = Column(Text, nullable=False)
    reviewed_at = Column(DateTime(timezone=True))
    playtime_hours = Column(Numeric(8, 2))
    source_meta_json = Column(JSONB)
    is_deleted = Column(Boolean, default=False)