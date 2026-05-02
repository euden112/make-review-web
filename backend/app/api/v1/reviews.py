import hashlib
from datetime import datetime, timedelta
from typing import Dict
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import func, desc, and_

from app.schemas.metacritic import MetacriticPayload
from app.schemas.steam import SteamPayload
from app.core.database import get_db
from app.models.domain import Platform, ReviewType, Game, GamePlatformMap, IngestionRun, ExternalReview

router = APIRouter()

def generate_review_key(*args):
    raw = "|".join(str(a) for a in args if a is not None)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def parse_date(date_str: str, format_str: str):
    try:
        return datetime.strptime(date_str, format_str)
    except:
        return None

async def get_reference_data(db: AsyncSession):
    platforms = {p.code: p.id for p in (await db.execute(select(Platform))).scalars().all()}
    review_types = {rt.type_code: rt.id for rt in (await db.execute(select(ReviewType))).scalars().all()}
    return platforms, review_types

@router.post("/metacritic")
async def receive_metacritic_data(payload: Dict[str, MetacriticPayload], db: AsyncSession = Depends(get_db)):
    platforms, review_types = await get_reference_data(db)
    platform_id = platforms.get("metacritic")

    for slug, game_data in payload.items():
        # 1. Game Upsert
        canonical_title = slug.replace("-", " ").title()
        stmt = insert(Game).values(
            canonical_title=canonical_title, 
            normalized_title=slug, 
            updated_at=datetime.utcnow()
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=['normalized_title'],
            set_=dict(
                canonical_title=stmt.excluded.canonical_title, 
                updated_at=datetime.utcnow()
            )
        ).returning(Game.id)
        game_id = (await db.execute(stmt)).scalar_one()

        # 2. GamePlatformMap Upsert
        map_stmt = insert(GamePlatformMap).values(
            game_id=game_id, 
            platform_id=platform_id, 
            external_game_id=slug, 
            crawled_at=datetime.utcnow(),
            platform_meta_json=game_data.meta.model_dump(),
            updated_at=datetime.utcnow()
        )
        map_stmt = map_stmt.on_conflict_do_update(
            index_elements=['platform_id', 'external_game_id'],
            set_=dict(
                game_id=map_stmt.excluded.game_id, 
                crawled_at=map_stmt.excluded.crawled_at,
                platform_meta_json=map_stmt.excluded.platform_meta_json,
                updated_at=datetime.utcnow()
            )
        )
        await db.execute(map_stmt)

        # 3. IngestionRun 시작
        run = IngestionRun(platform_id=platform_id, game_id=game_id, status="started")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # 4. 리뷰 데이터 준비
        reviews_data = []
        for rev in game_data.reviews:
            r_type_id = review_types.get("critic") if rev.type == "critic" else review_types.get("user")
            parsed_date = parse_date(rev.date, "%b %d, %Y")
            review_key = generate_review_key(rev.author, parsed_date, rev.type, rev.body)

            # AI 파이프라인 신뢰도 검증용: 점수 정규화 (100점 만점 기준)
            normalized = None
            if rev.score:
                try:
                    score_val = float(rev.score)
                    normalized = score_val * 10 if rev.type == "user" else score_val
                except ValueError:
                    pass

            reviews_data.append({
                "platform_id": platform_id,
                "game_id": game_id,
                "ingestion_run_id": run.id,
                "source_review_key": review_key,
                "review_type_id": r_type_id,
                "author_name": rev.author,
                "score_raw": rev.score,
                "normalized_score_100": normalized,
                "review_text_clean": rev.body,
                "helpful_count": getattr(rev, 'helpful_count', 0),
                "source_meta_json": getattr(rev, 'source_meta_json', {}),
                "review_categories_json": getattr(rev, 'review_categories', []),
                "language_code": getattr(rev, 'language', 'ko') or 'ko',
                "reviewed_at": parsed_date,
                "is_deleted": False,
                "updated_at": datetime.utcnow()
            })

        # 5. ExternalReview 대량 저장 (Bulk Upsert)
        if reviews_data:
            stmt = insert(ExternalReview).values(reviews_data)
            stmt = stmt.on_conflict_do_update(
                index_elements=['platform_id', 'game_id', 'source_review_key'],
                set_=dict(
                    ingestion_run_id=stmt.excluded.ingestion_run_id,
                    review_type_id=stmt.excluded.review_type_id,
                    author_name=stmt.excluded.author_name,
                    score_raw=stmt.excluded.score_raw,
                    normalized_score_100=stmt.excluded.normalized_score_100,
                    review_text_clean=stmt.excluded.review_text_clean,
                    reviewed_at=stmt.excluded.reviewed_at,
                    helpful_count=stmt.excluded.helpful_count,
                    source_meta_json=func.coalesce(stmt.excluded.source_meta_json, ExternalReview.source_meta_json),
                    review_categories_json=stmt.excluded.review_categories_json,
                    language_code=stmt.excluded.language_code,
                    is_deleted=False,
                    updated_at=datetime.utcnow()
                )
            )
            await db.execute(stmt)

        # 6. IngestionRun 완료 기록
        run.status = "success"
        run.fetched_count = len(game_data.reviews)
        run.inserted_count = len(reviews_data)
        run.ended_at = datetime.utcnow()
        await db.commit()

    return {"status": "success", "message": f"Metacritic 데이터 {len(payload)}건 DB 저장 완료"}


@router.post("/steam")
async def receive_steam_data(payload: Dict[str, SteamPayload], db: AsyncSession = Depends(get_db)):
    platforms, review_types = await get_reference_data(db)
    platform_id = platforms.get("steam")
    user_type_id = review_types.get("user")

    for slug, game_data in payload.items():
        # 1. Game Upsert
        canonical_title = slug.replace("-", " ").title()
        stmt = insert(Game).values(
            canonical_title=canonical_title, 
            normalized_title=slug, 
            updated_at=datetime.utcnow()
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=['normalized_title'],
            set_=dict(
                canonical_title=stmt.excluded.canonical_title, 
                updated_at=datetime.utcnow()
            )
        ).returning(Game.id)
        game_id = (await db.execute(stmt)).scalar_one()

        # 2. GamePlatformMap Upsert
        app_id = game_data.meta.game_id
        map_stmt = insert(GamePlatformMap).values(
            game_id=game_id, 
            platform_id=platform_id, 
            external_game_id=app_id, 
            crawled_at=datetime.utcnow(),
            platform_meta_json=game_data.meta.model_dump(),
            updated_at=datetime.utcnow()
        )
        map_stmt = map_stmt.on_conflict_do_update(
            index_elements=['platform_id', 'external_game_id'],
            set_=dict(
                game_id=map_stmt.excluded.game_id, 
                crawled_at=map_stmt.excluded.crawled_at,
                platform_meta_json=map_stmt.excluded.platform_meta_json,
                updated_at=datetime.utcnow()
            )
        )
        await db.execute(map_stmt)

        # 3. IngestionRun 시작
        run = IngestionRun(platform_id=platform_id, game_id=game_id, status="started")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # 4. 리뷰 데이터 준비
        reviews_data = []
        for rev in game_data.reviews:
            parsed_date = parse_date(rev.date_posted, "%Y-%m-%d")
            review_key = generate_review_key(rev.author_id, parsed_date, rev.review_text)

            reviews_data.append({
                "platform_id": platform_id,
                "game_id": game_id,
                "ingestion_run_id": run.id,
                "source_review_key": review_key,
                "review_type_id": user_type_id,
                "author_name": rev.author_id,
                "is_recommended": rev.is_recommended,
                "review_text_clean": rev.review_text,
                "playtime_hours": rev.playtime_hours,
                "helpful_count": getattr(rev, 'helpful_count', 0),
                "source_meta_json": getattr(rev, 'source_meta_json', {}),
                "review_categories_json": getattr(rev, 'review_categories', []),
                "language_code": getattr(rev, 'language', 'ko') or 'ko',
                "reviewed_at": parsed_date,
                "is_deleted": False,
                "updated_at": datetime.utcnow()
            })

        # 5. ExternalReview 대량 저장 (Bulk Upsert)
        if reviews_data:
            stmt = insert(ExternalReview).values(reviews_data)
            stmt = stmt.on_conflict_do_update(
                index_elements=['platform_id', 'game_id', 'source_review_key'],
                set_=dict(
                    ingestion_run_id=stmt.excluded.ingestion_run_id,
                    review_type_id=stmt.excluded.review_type_id,
                    score_raw=stmt.excluded.score_raw,
                    author_name=stmt.excluded.author_name,
                    is_recommended=stmt.excluded.is_recommended,
                    playtime_hours=stmt.excluded.playtime_hours,
                    review_text_clean=stmt.excluded.review_text_clean,
                    reviewed_at=stmt.excluded.reviewed_at,
                    helpful_count=stmt.excluded.helpful_count,
                    source_meta_json=func.coalesce(stmt.excluded.source_meta_json, ExternalReview.source_meta_json),
                    review_categories_json=stmt.excluded.review_categories_json,
                    language_code=stmt.excluded.language_code,
                    is_deleted=False,
                    updated_at=datetime.utcnow()
                )
            )
            await db.execute(stmt)

        # 6. IngestionRun 완료 기록
        run.status = "success"
        run.fetched_count = len(game_data.reviews)
        run.inserted_count = len(reviews_data)
        run.ended_at = datetime.utcnow()
        await db.commit()

    return {"status": "success", "message": f"Steam 데이터 {len(payload)}건 DB 저장 완료"}


@router.get("/priority/general")
async def get_general_priority_reviews(
    days_back: int = Query(30, description="최근 N일 이내의 리뷰"),
    limit: int = Query(50, description="가져올 최대 리뷰 수"),
    db: AsyncSession = Depends(get_db)
):
    """
    프론트엔드 어드민 화면용: 'General' 태그가 달린 우선 재분류 대상 리뷰 조회 API
    """
    time_threshold = datetime.utcnow() - timedelta(days=days_back)
    
    query = select(ExternalReview).where(
        and_(
            ExternalReview.is_deleted == False,
            ExternalReview.reviewed_at >= time_threshold,
            ExternalReview.review_categories_json.contains([{"category": "General"}])
        )
    ).order_by(
        desc(ExternalReview.reviewed_at),
        desc(ExternalReview.helpful_count),
        desc(ExternalReview.id)
    ).limit(limit)
    
    reviews = (await db.execute(query)).scalars().all()
    
    result = []
    for r in reviews:
        result.append({
            "id": r.id,
            "game_id": r.game_id,
            "author_name": r.author_name,
            "language_code": r.language_code,
            "playtime_hours": float(r.playtime_hours) if r.playtime_hours else 0.0,
            "review_categories": r.review_categories_json,
            "review_text": r.review_text_clean,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            "helpful_count": r.helpful_count
        })
        
    return result