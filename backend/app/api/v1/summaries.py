from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.core.database import get_db
from app.core.redis_client import get_summary_cache, set_summary_cache
from app.models.domain import GameReviewSummary
from app.services.ai_service import run_ai_pipeline_task

router = APIRouter()

@router.post("/{game_id}/summarize")
async def trigger_summarization(
    game_id: int, 
    background_tasks: BackgroundTasks, 
    language: str = "ko", 
    db: AsyncSession = Depends(get_db)
):
    """AI 요약 파이프라인 트리거 (비동기 큐 삽입)"""
    # 백그라운드에서 ai-pipeline을 가동하도록 지시합니다.
    background_tasks.add_task(run_ai_pipeline_task, game_id, language, db)
    return {
        "status": "processing", 
        "message": f"게임 {game_id}의 AI 요약 작업이 비동기로 시작되었습니다."
    }

@router.get("/{game_id}")
async def get_latest_summary(
    game_id: int, 
    language: str = "ko", 
    db: AsyncSession = Depends(get_db)
):
    """프론트엔드 조회 API (Redis 캐싱 적용)"""
    # 1. Redis 캐시 먼저 확인 (Cache Hit)
    cached_summary = await get_summary_cache(game_id, language)
    if cached_summary:
        print(f"⚡ [Redis Cache Hit] 게임 {game_id} 요약본 즉시 반환")
        return cached_summary

    # 2. Redis에 없으면 DB에서 조회 (Cache Miss)
    query = select(GameReviewSummary).where(
        and_(
            GameReviewSummary.game_id == game_id, 
            GameReviewSummary.language_code == language, 
            GameReviewSummary.is_current == True
        )
    )
    summary = (await db.execute(query)).scalar_one_or_none()
    
    if not summary:
        raise HTTPException(status_code=404, detail="AI 요약본이 없습니다.")
        
    # 3. DB 조회 결과를 포맷팅
    result = {
        "game_id": summary.game_id,
        "version": summary.summary_version,
        "summary_text": summary.summary_text,
        "pros": summary.pros_json,
        "cons": summary.cons_json,
        "keywords": summary.keywords_json,
        "updated_at": summary.created_at.isoformat()
    }
    
    # 4. 다음 요청을 위해 Redis에 저장해두기
    await set_summary_cache(game_id, language, result)
    print(f"💾 [Redis Cache Set] 게임 {game_id} 요약본 DB 조회 후 캐싱 완료")
    
    return result