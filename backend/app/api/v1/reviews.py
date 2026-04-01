import hashlib
from datetime import datetime
from typing import Dict
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.dialects.postgresql import insert  # 데이터 중복을 방지하는 PostgreSQL 전용 Upsert 기능
from app.schemas.metacritic import MetacriticPayload
from app.schemas.steam import SteamPayload
from app.core.database import get_db
from app.models.domain import Platform, ReviewType, Game, GamePlatformMap, IngestionRun, ExternalReview

router = APIRouter()

# ==============================================================================
# 유틸리티 함수 (반복되는 작업을 편하게 하기 위한 도구들)
# ==============================================================================

# 리뷰의 작성자, 작성일, 본문 등을 합쳐서 '절대 겹치지 않는 지문(해시 키)'을 만드는 함수입니다.
# 똑같은 리뷰가 또 수집되더라도 이 키를 통해 중복을 막아냅니다.
def generate_review_key(*args):
    raw = "|".join(str(a) for a in args if a is not None)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

# 영어 텍스트로 된 날짜를 데이터베이스가 이해할 수 있는 날짜 포맷으로 변환해 줍니다.
def parse_date(date_str: str, format_str: str):
    try:
        return datetime.strptime(date_str, format_str)
    except:
        return None

# DB에 미리 세팅해둔 플랫폼(steam, metacritic)과 리뷰 타입(user, critic)의 고유 ID를 가져옵니다.
async def get_reference_data(db: AsyncSession):
    platforms = {p.code: p.id for p in (await db.execute(select(Platform))).scalars().all()}
    review_types = {rt.type_code: rt.id for rt in (await db.execute(select(ReviewType))).scalars().all()}
    return platforms, review_types


# ==============================================================================
# [POST] 메타크리틱 데이터 수신 및 저장 로직
# ==============================================================================
@router.post("/metacritic")
async def receive_metacritic_data(payload: Dict[str, MetacriticPayload], db: AsyncSession = Depends(get_db)):
    platforms, review_types = await get_reference_data(db)
    platform_id = platforms.get("metacritic")

    for slug, game_data in payload.items():
        # 1. 게임(Game) 테이블 저장 (Upsert)
        # 게임 이름이 이미 DB에 있으면 업데이트하고, 없으면 새로 만듭니다.
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

        # 2. 게임-플랫폼 매핑(GamePlatformMap) 정보 저장 (Upsert)
        # 메타크리틱은 별도의 고유 숫자 ID가 없으므로 영문 이름(slug) 자체를 ID로 씁니다.
        # 크롤러가 보낸 귀중한 통계 데이터(meta)를 JSONB 컬럼에 통째로 저장합니다.
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

        # 3. 데이터 수집 이력(Ingestion Run) 시작 로그 기록
        run = IngestionRun(platform_id=platform_id, game_id=game_id, status="started")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # 4. 수만 개의 리뷰를 한 번에 넣기 위해 리스트에 예쁘게 포장합니다.
        reviews_data = []
        for rev in game_data.reviews:
            r_type_id = review_types.get("critic") if rev.type == "critic" else review_types.get("user")
            parsed_date = parse_date(rev.date, "%b %d, %Y")
            
            # 리뷰 데이터로 해시 키(절대 중복되지 않는 지문)를 만듭니다.
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

        # 5. 리뷰(ExternalReview) 대량 저장 실행 (Bulk Upsert)
        # 이미 존재하는 리뷰(해시 키가 같은 것)라면 내용만 최신으로 덮어쓰고, 없으면 새로 저장합니다.
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

        # 6. 수집 작업이 무사히 끝났음을 이력(Ingestion Run)에 기록합니다.
        run.status = "success"
        run.inserted_count = len(reviews_data)
        run.ended_at = datetime.utcnow()
        await db.commit()

    return {"status": "success", "message": f"Metacritic 데이터 {len(payload)}건 DB 저장 완료"}


# ==============================================================================
# [POST] 스팀 데이터 수신 및 저장 로직 (메타크리틱과 과정은 거의 동일합니다)
# ==============================================================================
@router.post("/steam")
async def receive_steam_data(payload: Dict[str, SteamPayload], db: AsyncSession = Depends(get_db)):
    platforms, review_types = await get_reference_data(db)
    platform_id = platforms.get("steam")
    user_type_id = review_types.get("user")  # 스팀 리뷰는 전부 'user(일반 유저)' 타입입니다.

    for slug, game_data in payload.items():
        # 1. 게임(Game) 테이블 저장 (Upsert)
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

        # 2. 게임-플랫폼 매핑(GamePlatformMap) 정보 저장 (Upsert)
        # 스팀은 고유 숫자 ID(app_id)를 가지고 있으므로 이를 외부 ID로 씁니다.
        # 스팀 크롤러가 수집한 '긍정/부정 리뷰 개수' 통계를 JSONB로 통째로 저장합니다.
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

        # 3. 데이터 수집 이력 시작 로그 기록
        run = IngestionRun(platform_id=platform_id, game_id=game_id, status="started")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # 4. 리뷰 대량 저장 준비
        reviews_data = []
        for rev in game_data.reviews:
            parsed_date = parse_date(rev.date_posted, "%Y-%m-%d")
            
            # 해시 키 생성 (스팀은 작성자ID + 날짜 + 본문을 조합해 고유 지문을 만듭니다)
            review_key = generate_review_key(rev.author_id, parsed_date, rev.review_text)

            reviews_data.append({
                "platform_id": platform_id,
                "game_id": game_id,
                "ingestion_run_id": run.id,
                "source_review_key": review_key,
                "review_type_id": user_type_id,
                "author_name": rev.author_id,
                "is_recommended": rev.is_recommended,    # 스팀의 핵심인 추천/비추천 데이터
                "review_text_clean": rev.review_text,
                "playtime_hours": rev.playtime_hours,    # 플레이 타임 저장
                "reviewed_at": parsed_date,
                "is_deleted": False
            })

        # 5. 리뷰 대량 저장 실행 (Bulk Upsert)
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

        # 6. 수집 작업 종료 로그 기록
        run.status = "success"
        run.inserted_count = len(reviews_data)
        run.ended_at = datetime.utcnow()
        await db.commit()

    return {"status": "success", "message": f"Steam 데이터 {len(payload)}건 DB 저장 완료"}