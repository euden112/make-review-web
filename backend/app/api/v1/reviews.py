import hashlib
from datetime import datetime
from typing import Dict
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.dialects.postgresql import insert  # PostgreSQL 전용 Upsert 기능
from app.schemas.metacritic import MetacriticPayload
from app.schemas.steam import SteamPayload
from app.core.database import get_db
from app.models.domain import Platform, ReviewType, Game, GamePlatformMap, IngestionRun, ExternalReview

router = APIRouter()

# ==============================================================================
# 유틸리티 함수
# ==============================================================================

# 해시 키 생성 유틸리티 (source_review_key 생성용)
def generate_review_key(*args):
    raw = "|".join(str(a) for a in args if a is not None)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

# 날짜 파싱 유틸리티
def parse_date(date_str: str, format_str: str):
    try:
        return datetime.strptime(date_str, format_str)
    except:
        return None

# 공통 참조 데이터(Platform, ReviewType) 가져오는 헬퍼 함수
async def get_reference_data(db: AsyncSession):
    platforms = {p.code: p.id for p in (await db.execute(select(Platform))).scalars().all()}
    review_types = {rt.type_code: rt.id for rt in (await db.execute(select(ReviewType))).scalars().all()}
    return platforms, review_types


# ==============================================================================
# [POST] 메타크리틱 리뷰 수신 라우터
# ==============================================================================
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

        # 2. GamePlatformMap 매핑 (메타크리틱은 slug 자체를 외부 ID로 사용)
        map_stmt = insert(GamePlatformMap).values(
            game_id=game_id, 
            platform_id=platform_id, 
            external_game_id=slug, 
            crawled_at=datetime.utcnow(),
            platform_meta_json=game_data.meta.model_dump()
        )
        map_stmt = map_stmt.on_conflict_do_update(
            index_elements=['platform_id', 'external_game_id'],
            set_=dict(
                game_id=map_stmt.excluded.game_id, 
                crawled_at=map_stmt.excluded.crawled_at,
                platform_meta_json=map_stmt.excluded.platform_meta_json
            )
        )
        await db.execute(map_stmt)

        # 3. Ingestion Run 시작 로그
        run = IngestionRun(platform_id=platform_id, game_id=game_id, status="started")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # 4. Review Bulk Upsert 준비
        reviews_data = []
        for rev in game_data.reviews:
            r_type_id = review_types.get("critic") if rev.type == "critic" else review_types.get("user")
            parsed_date = parse_date(rev.date, "%b %d, %Y")
            
            # 해시 키 생성: sha256(author | date | type | review_text_clean)
            review_key = generate_review_key(rev.author, parsed_date, rev.type, rev.body)

            reviews_data.append({
                "platform_id": platform_id,
                "game_id": game_id,
                "ingestion_run_id": run.id,
                "source_review_key": review_key,
                "review_type_id": r_type_id,
                "author_name": rev.author,
                "score_raw": rev.score,
                "review_text_clean": rev.body,
                "reviewed_at": parsed_date,
                "is_deleted": False
            })

        # 5. Review Upsert 실행
        if reviews_data:
            stmt = insert(ExternalReview).values(reviews_data)
            stmt = stmt.on_conflict_do_update(
                index_elements=['platform_id', 'game_id', 'source_review_key'],
                set_=dict(
                    score_raw=stmt.excluded.score_raw,
                    review_text_clean=stmt.excluded.review_text_clean,
                    is_deleted=False
                )
            )
            await db.execute(stmt)

        # 6. Ingestion Run 종료 업데이트
        run.status = "success"
        run.inserted_count = len(reviews_data)
        run.ended_at = datetime.utcnow()
        await db.commit()

    return {"status": "success", "message": f"Metacritic 데이터 {len(payload)}건 DB 저장 완료"}


# ==============================================================================
# [POST] 스팀 리뷰 수신 라우터
# ==============================================================================
@router.post("/steam")
async def receive_steam_data(payload: Dict[str, SteamPayload], db: AsyncSession = Depends(get_db)):
    platforms, review_types = await get_reference_data(db)
    platform_id = platforms.get("steam")
    user_type_id = review_types.get("user")  # 스팀은 기본적으로 user 리뷰

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

        # 2. GamePlatformMap 매핑 (스팀은 app_id를 외부 ID로 사용)
        app_id = game_data.meta.game_id
        map_stmt = insert(GamePlatformMap).values(
            game_id=game_id, 
            platform_id=platform_id, 
            external_game_id=app_id, 
            crawled_at=datetime.utcnow(),
            platform_meta_json=game_data.meta.model_dump()
        )
        map_stmt = map_stmt.on_conflict_do_update(
            index_elements=['platform_id', 'external_game_id'],
            set_=dict(
                game_id=map_stmt.excluded.game_id, 
                crawled_at=map_stmt.excluded.crawled_at,
                platform_meta_json=map_stmt.excluded.platform_meta_json
            )
        )
        await db.execute(map_stmt)

        # 3. Ingestion Run 시작 로그
        run = IngestionRun(platform_id=platform_id, game_id=game_id, status="started")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # 4. Review Bulk Upsert 준비
        reviews_data = []
        for rev in game_data.reviews:
            parsed_date = parse_date(rev.date_posted, "%Y-%m-%d")
            
            # 해시 키 생성: sha256(author_id | date_posted | review_text_clean)
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
                "reviewed_at": parsed_date,
                "is_deleted": False
            })

        # 5. Review Upsert 실행
        if reviews_data:
            stmt = insert(ExternalReview).values(reviews_data)
            stmt = stmt.on_conflict_do_update(
                index_elements=['platform_id', 'game_id', 'source_review_key'],
                set_=dict(
                    is_recommended=stmt.excluded.is_recommended,
                    playtime_hours=stmt.excluded.playtime_hours,
                    review_text_clean=stmt.excluded.review_text_clean,
                    is_deleted=False
                )
            )
            await db.execute(stmt)

        # 6. Ingestion Run 종료 업데이트
        run.status = "success"
        run.inserted_count = len(reviews_data)
        run.ended_at = datetime.utcnow()
        await db.commit()

    return {"status": "success", "message": f"Steam 데이터 {len(payload)}건 DB 저장 완료"}