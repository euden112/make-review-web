import logging
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.core.database import get_db
from app.core.redis_client import get_summary_cache, set_summary_cache
from app.models.domain import GameReviewSummary, ReviewSummaryJob
from app.services.ai_service import run_ai_pipeline_task, get_pipeline_tasks

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def get_games(db: AsyncSession = Depends(get_db)):
    from app.models.domain import Game, GamePlatformMap, Platform

    games = (await db.execute(select(Game))).scalars().all()

    steam_platform = (await db.execute(
        select(Platform).where(Platform.code == "steam")
    )).scalar_one_or_none()

    metacritic_platform = (await db.execute(
        select(Platform).where(Platform.code == "metacritic")
    )).scalar_one_or_none()

    result = []
    for g in games:
        cover_image = None
        hero_image = None
        tags: list[str] = []
        # Metacritic 100점 → 5점 환산 (소수점 1자리)
        rating: float | None = None

        if steam_platform:
            steam_map = (await db.execute(
                select(GamePlatformMap).where(
                    and_(
                        GamePlatformMap.game_id == g.id,
                        GamePlatformMap.platform_id == steam_platform.id,
                    )
                )
            )).scalar_one_or_none()

            if steam_map and steam_map.platform_meta_json:
                cover_image = steam_map.platform_meta_json.get("cover_image")
                hero_image = steam_map.platform_meta_json.get("hero_image")
                tags = steam_map.platform_meta_json.get("tags") or []

        if metacritic_platform:
            meta_map = (await db.execute(
                select(GamePlatformMap).where(
                    and_(
                        GamePlatformMap.game_id == g.id,
                        GamePlatformMap.platform_id == metacritic_platform.id,
                    )
                )
            )).scalar_one_or_none()

            if meta_map and meta_map.platform_meta_json:
                score = meta_map.platform_meta_json.get("score")
                if score is not None:
                    rating = round(float(score) / 20, 1)

        result.append({
            "id": g.id,
            "canonical_title": g.canonical_title,
            "cover_image": cover_image,
            "hero_image": hero_image,
            "tags": tags,
            "rating": rating,
        })

    return result


@router.post("/{game_id}/summarize")
async def trigger_summarization(
    game_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    force: bool = Query(False, description="커서를 무시하고 전체 리뷰 재처리"),
):
    """AI 요약 파이프라인 트리거 — unified 1개 + regional N개 일괄 등록

    force=true: 커서 위치를 무시하고 전체 리뷰를 다시 처리합니다.
    오류 후 재실행하거나 요약을 강제 재생성할 때 사용합니다.
    """
    tasks = await get_pipeline_tasks(game_id, db)
    for mode, lang in tasks:
        background_tasks.add_task(run_ai_pipeline_task, game_id, mode, lang, force=force)
    return {
        "status": "processing",
        "message": f"게임 {game_id}의 AI 요약 작업이 비동기로 시작되었습니다.",
        "tasks": [{"mode": m, "language_code": l} for m, l in tasks],
        "force": force,
    }


@router.get("/{game_id}/summary")
async def get_unified_summary(
    game_id: int,
    db: AsyncSession = Depends(get_db),
    compact: bool = Query(True, description="응답에서 None/빈값 제거 (compact)")
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
                GameReviewSummary.summary_type == summary_type,
                GameReviewSummary.review_language.is_(None),
                GameReviewSummary.is_current == True,
            )
        )
    )).scalar_one_or_none()

    if not summary:
        raise HTTPException(status_code=404, detail="AI 요약본이 없습니다.")

    job = None
    if summary.job_id is not None:
        job = (await db.execute(
            select(ReviewSummaryJob).where(ReviewSummaryJob.id == summary.job_id)
        )).scalar_one_or_none()

    result = _serialize_summary(summary, job, compact=compact)

    logger.info("cache_miss game_id=%s summary_type=%s", game_id, summary_type)
    await set_summary_cache(game_id, summary_type, result)

    return result


@router.get("/{game_id}/perspectives")
async def get_regional_perspectives(
    game_id: int,
    db: AsyncSession = Depends(get_db),
    compact: bool = Query(True, description="응답에서 None/빈값 제거 (compact)")
):
    """언어권별 시각 목록 반환"""
    rows = (await db.execute(
        select(GameReviewSummary).where(
            and_(
                GameReviewSummary.game_id == game_id,
                GameReviewSummary.summary_type == "regional",
                GameReviewSummary.is_current == True,
            )
        )
    )).scalars().all()

    if not rows:
        raise HTTPException(status_code=404, detail="언어권별 요약본이 없습니다.")

    return [_serialize_summary(s, compact=compact) for s in rows]


def _serialize_summary(summary: GameReviewSummary, job: ReviewSummaryJob | None = None, compact: bool = True) -> dict:
    result = {
        "game_id": summary.game_id,
        "summary_type": summary.summary_type,
        "review_language": summary.review_language,
        "language_code": summary.review_language if summary.review_language is not None else "unified",
        "version": summary.summary_version,
        "summary_text": summary.summary_text,
        "pros": summary.pros_json,
        "cons": summary.cons_json,
        "keywords": summary.keywords_json,
        "representative_reviews": summary.representative_reviews_json,
        "sentiment_overall": summary.sentiment_overall,
        "sentiment_score": float(summary.sentiment_score) if summary.sentiment_score is not None else None,
        "aspect_sentiment": summary.aspect_sentiment_json,
        "created_at": summary.created_at.isoformat(),
        "reliability": None,
    }
    if job is not None:
        result["reliability"] = {
            "schema_compliance": float(job.schema_compliance) if job.schema_compliance is not None else None,
            "hallucination_score": float(job.hallucination_score) if job.hallucination_score is not None else None,
            "sentiment_consistency": job.sentiment_consistency,
            "anchor_deviation": float(job.anchor_deviation) if job.anchor_deviation is not None else None,
            "input_review_count": job.input_review_count,
            "reduce_input_tokens": job.reduce_input_tokens,
            "reduce_output_tokens": job.reduce_output_tokens,
        }
    if compact:
        def _clean(obj):
            if not isinstance(obj, dict):
                return obj
            out = {}
            for k, v in obj.items():
                # keep zero and False; remove None, empty string, empty list/dict
                if v is None:
                    continue
                if isinstance(v, str) and v.strip() == "":
                    continue
                if isinstance(v, (list, dict)) and not v:
                    continue
                out[k] = v
            return out

        result = _clean(result)
        # nested clean for reliability and aspect_sentiment
        if "reliability" in result and isinstance(result["reliability"], dict):
            result["reliability"] = _clean(result["reliability"]) or None
        if "aspect_sentiment" in result and isinstance(result["aspect_sentiment"], dict):
            result["aspect_sentiment"] = _clean(result["aspect_sentiment"]) or None

    return result
