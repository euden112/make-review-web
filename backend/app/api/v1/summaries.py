import logging

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.core.database import get_db
from app.core.redis_client import get_summary_cache, set_summary_cache
from app.models.domain import GameReviewSummary
from app.services.ai_service import run_ai_pipeline_task, get_pipeline_tasks

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/{game_id}/summarize")
async def trigger_summarization(
    game_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """AI 요약 파이프라인 트리거 — unified 1개 + regional N개 일괄 등록"""
    tasks = await get_pipeline_tasks(game_id, db)
    for mode, lang in tasks:
        background_tasks.add_task(run_ai_pipeline_task, game_id, mode, lang)
    return {
        "status": "processing",
        "message": f"게임 {game_id}의 AI 요약 작업이 비동기로 시작되었습니다.",
        "tasks": [{"mode": m, "language_code": l} for m, l in tasks],
    }


@router.get("/{game_id}/summary")
async def get_unified_summary(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """통합 요약 반환 (Redis 캐싱 적용)"""
    summary_type = "unified"

    cached = await get_summary_cache(game_id, summary_type)
    if cached:
        logger.info("cache_hit game_id=%s summary_type=%s", game_id, summary_type)
        return cached

    summary = (await db.execute(
        select(GameReviewSummary).where(
            and_(
                GameReviewSummary.game_id == game_id,
                GameReviewSummary.language_code == summary_type,
                GameReviewSummary.is_current == True,
            )
        )
    )).scalar_one_or_none()

    if not summary:
        raise HTTPException(status_code=404, detail="AI 요약본이 없습니다.")

    result = _serialize_summary(summary)

    await set_summary_cache(game_id, summary_type, result)
    logger.info("cache_miss game_id=%s summary_type=%s", game_id, summary_type)

    return result


@router.get("/{game_id}/perspectives")
async def get_regional_perspectives(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """언어권별 시각 목록 반환"""
    rows = (await db.execute(
        select(GameReviewSummary).where(
            and_(
                GameReviewSummary.game_id == game_id,
                GameReviewSummary.language_code != "unified",
                GameReviewSummary.is_current == True,
            )
        )
    )).scalars().all()

    if not rows:
        raise HTTPException(status_code=404, detail="언어권별 요약본이 없습니다.")

    return [_serialize_summary(s) for s in rows]


def _serialize_summary(summary: GameReviewSummary) -> dict:
    return {
        "game_id": summary.game_id,
        "language_code": summary.language_code,
        "version": summary.summary_version,
        "summary_text": summary.summary_text,
        "pros": summary.pros_json,
        "cons": summary.cons_json,
        "keywords": summary.keywords_json,
        "representative_reviews": summary.representative_reviews_json,
        "sentiment_overall": summary.sentiment_overall,
        "sentiment_score": float(summary.sentiment_score) if summary.sentiment_score is not None else None,
        "aspect_sentiment": summary.aspect_sentiment_json,
        "updated_at": summary.created_at.isoformat(),
    }
