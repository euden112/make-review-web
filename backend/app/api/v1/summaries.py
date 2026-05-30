import logging
from typing import Any

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_
from sqlalchemy.orm import joinedload

from app.core.database import get_db
from app.core.auth import require_api_key
from app.core.redis_client import get_summary_cache, set_summary_cache
from app.models.domain import Game, GamePlatformMap, Platform, GameReviewSummary, ReviewSummaryJob, ExternalReview
from app.services.ai_service import run_ai_pipeline_task, get_pipeline_tasks


class _PlaytimeBucketsInput(BaseModel):
    early_max: float | None = None
    mid_max: float | None = None
    # 버킷별 실제 리뷰 수/추천 비율(0~100). 로컬 Map 단계에서 원본 리뷰로 산출해 전달.
    bucket_stats: dict[str, Any] | None = None


class _MapStatsInput(BaseModel):
    chunk_count: int = 0
    map_cache_hit: int = 0
    map_cache_miss: int = 0
    map_input_tokens: int = 0
    map_output_tokens: int = 0
    failure_reasons: dict[str, Any] | None = None


class _SourceStatsInput(BaseModel):
    total_reviews_in_db: int
    new_count_since_last: int
    batch_from_review_id: int
    new_max_review_id: int
    covered_from_review_id: int
    covered_to_review_id: int
    source_review_count: int


class ReduceRequest(BaseModel):
    language_code: str = "ko"
    grouped_summaries: dict[str, list[str]]
    representative_quotes: list[str] = []
    score_anchors: dict[str, Any] = {}
    category_frequency: list[list[Any]] = []
    prior_summary_text: str | None = None
    playtime_buckets: _PlaytimeBucketsInput | None = None
    map_stats: _MapStatsInput | None = None
    source_stats: _SourceStatsInput

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def get_games(db: AsyncSession = Depends(get_db)):
    steam_platform = (await db.execute(
        select(Platform).where(Platform.code == "steam")
    )).scalar_one_or_none()
    metacritic_platform = (await db.execute(
        select(Platform).where(Platform.code == "metacritic")
    )).scalar_one_or_none()

    games = (await db.execute(select(Game).order_by(Game.id))).scalars().all()

    # 플랫폼 맵을 플랫폼별 단일 쿼리로 일괄 로드 (N+1 제거)
    steam_maps: dict[int, GamePlatformMap] = {}
    meta_maps: dict[int, GamePlatformMap] = {}
    if games:
        game_ids = [g.id for g in games]
        if steam_platform:
            rows = (await db.execute(
                select(GamePlatformMap).where(
                    and_(
                        GamePlatformMap.platform_id == steam_platform.id,
                        GamePlatformMap.game_id.in_(game_ids),
                    )
                )
            )).scalars().all()
            steam_maps = {m.game_id: m for m in rows}
        if metacritic_platform:
            rows = (await db.execute(
                select(GamePlatformMap).where(
                    and_(
                        GamePlatformMap.platform_id == metacritic_platform.id,
                        GamePlatformMap.game_id.in_(game_ids),
                    )
                )
            )).scalars().all()
            meta_maps = {m.game_id: m for m in rows}

    result = []
    for g in games:
        cover_image = None
        hero_image = None
        tags: list[str] = []
        # Metacritic 100점 → 5점 환산 (소수점 1자리)
        rating: float | None = None

        steam_map = steam_maps.get(g.id)
        if steam_map and steam_map.platform_meta_json:
            cover_image = steam_map.platform_meta_json.get("cover_image")
            hero_image = steam_map.platform_meta_json.get("hero_image")
            tags = steam_map.platform_meta_json.get("tags") or []

        meta_map = meta_maps.get(g.id)
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


@router.post("/{game_id}/summarize", dependencies=[Depends(require_api_key)])
async def trigger_summarization(
    game_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    force: bool = Query(False, description="커서를 무시하고 전체 리뷰 재처리"),
):
    """AI 요약 파이프라인 트리거

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


@router.get("/{game_id}/reviews-for-map", dependencies=[Depends(require_api_key)])
async def get_reviews_for_map(
    game_id: int,
    force: bool = Query(False, description="커서 무시하고 전체 리뷰 반환"),
):
    """로컬 Map 단계 실행을 위한 리뷰 데이터 제공."""
    from app.services.ai_service import get_reviews_for_map as _svc
    return await _svc(game_id, force=force)


@router.post("/{game_id}/reduce", dependencies=[Depends(require_api_key)])
async def trigger_reduce_from_map(
    game_id: int,
    body: ReduceRequest,
    background_tasks: BackgroundTasks,
):
    """로컬 Map 결과를 받아 Reduce → DB 저장 (BackgroundTask로 즉시 반환)."""
    from app.services.ai_service import run_reduce_from_precomputed_map
    background_tasks.add_task(
        run_reduce_from_precomputed_map,
        game_id=game_id,
        language_code=body.language_code,
        grouped_summaries=body.grouped_summaries,
        representative_quotes=body.representative_quotes,
        score_anchors=body.score_anchors,
        category_frequency=body.category_frequency,
        prior_summary_text=body.prior_summary_text,
        playtime_buckets_dict=body.playtime_buckets.model_dump() if body.playtime_buckets else None,
        map_stats=body.map_stats.model_dump() if body.map_stats else None,
        source_stats=body.source_stats.model_dump(),
    )
    return {"status": "processing", "game_id": game_id}


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
        select(GameReviewSummary)
        .options(joinedload(GameReviewSummary.job))
        .where(
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

    review_text_map = await _fetch_representative_review_texts(db, summary.representative_reviews_json)
    result = _serialize_summary(summary, summary.job, compact=compact, review_text_map=review_text_map)

    logger.info("cache_miss game_id=%s summary_type=%s", game_id, summary_type)
    await set_summary_cache(game_id, summary_type, result)

    return result



async def _fetch_representative_review_texts(db: AsyncSession, rep_reviews: list | None) -> dict[int, str]:
    if not rep_reviews:
        return {}
    review_ids = [r["review_id"] for r in rep_reviews if r.get("review_id") is not None]
    if not review_ids:
        return {}
    rows = (await db.execute(
        select(ExternalReview.id, ExternalReview.review_text_clean).where(ExternalReview.id.in_(review_ids))
    )).all()
    # 표시용 대표 리뷰는 원문 verbatim이므로 경량 redaction(일반 스포일러 패턴 + 비속어)만 적용
    from ai_module.map_reduce.map_schema import redact_display_text
    return {row.id: redact_display_text(row.review_text_clean) for row in rows if row.review_text_clean}


def _serialize_summary(
    summary: GameReviewSummary,
    job: ReviewSummaryJob | None = None,
    compact: bool = True,
    review_text_map: dict[int, str] | None = None,
) -> dict:
    rep_reviews = summary.representative_reviews_json
    if rep_reviews and review_text_map:
        rep_reviews = [
            {**r, "quote": review_text_map.get(r.get("review_id"), r.get("quote"))}
            for r in rep_reviews
        ]

    result = {
        "game_id": summary.game_id,
        "summary_type": summary.summary_type,
        "review_language": summary.review_language,
        "language_code": summary.review_language if summary.review_language is not None else "unified",
        "version": summary.summary_version,
        "one_liner": summary.one_liner or (
            (summary.summary_text or "").splitlines()[0].strip().strip("*").strip()
            if summary.summary_text else None
        ),
        "summary_text": summary.summary_text,
        "pros": summary.pros_json,
        "cons": summary.cons_json,
        "keywords": summary.keywords_json,
        "representative_reviews": rep_reviews,
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
