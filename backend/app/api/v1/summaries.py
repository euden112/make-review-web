import logging
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query
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


# 수정됨(Sprint 3): 이 엔드포인트는 Sprint 3에서 정식화되었습니다 —
# summary_type='unified' + review_language IS NULL 기반으로 동작합니다.
@router.get("/{game_id}/summary")
async def get_unified_summary(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """통합 요약 반환 (Redis 캐싱 적용)
    
    Sprint 3: 통합 요약 전용 엔드포인트
    - 쿼리: summary_type="unified" AND review_language IS NULL
    - Redis 캐싱: 통합 요약만 캐시 (성능 최적화)
    - 기존 /summary 엔드포인트와 호환 유지
    """
    summary_type = "unified"

    cached = await get_summary_cache(game_id, summary_type)
    if cached:
        # Sprint 3: 구조화된 캐시 히트 로그 (print 제거, logger 사용)
        logger.info("cache_hit game_id=%s summary_type=%s", game_id, summary_type)
        return cached

    # Sprint 3: 정식 필드 기반 쿼리 (summary_type + review_language)
    # 기존: language_code="unified" (워크어라운드, 비표준)
    # 변경: summary_type="unified" AND review_language IS NULL (정식)
    summary = (await db.execute(
        select(GameReviewSummary).where(
            and_(
                GameReviewSummary.game_id == game_id,
                GameReviewSummary.summary_type == summary_type,  # Sprint 3 추가
                GameReviewSummary.review_language.is_(None),     # Sprint 3 추가 (unified 구분)
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


# 수정됨(Sprint 3): regional 전용 엔드포인트로 확정되었습니다.
@router.get("/{game_id}/perspectives")
async def get_regional_perspectives(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """언어권별 시각 목록 반환
    
    Sprint 3: 지역별 요약 전용 엔드포인트
    - 쿼리: summary_type="regional"
    - 반환: 배열 [{summary_type, review_language, ...}, ...]
    - 캐싱: 적용 안 함 (지역별로 변동 빈번)
    """
    # Sprint 3: 지역별 요약만 선택 (regional 모드)
    rows = (await db.execute(
        select(GameReviewSummary).where(
            and_(
                GameReviewSummary.game_id == game_id,
                GameReviewSummary.summary_type == "regional",  # Sprint 3 추가
                # review_language: IS NOT NULL (자동으로 regional이므로 조건 생략)
                GameReviewSummary.is_current == True,
            )
        )
    )).scalars().all()

    if not rows:
        raise HTTPException(status_code=404, detail="언어권별 요약본이 없습니다.")

    return [_serialize_summary(s) for s in rows]


# 수정됨(Sprint 3): 응답 직렬화에 summary_type/review_language가 포함되며
# 기존 클라이언트 호환을 위해 language_code를 자동 생성합니다.
def _serialize_summary(summary: GameReviewSummary) -> dict:
    """GameReviewSummary ORM → API 응답 변환

    Sprint 3: 새 필드 포함 + 역호환성 유지
    - 신규 필드: summary_type (unified|regional), review_language (en/ko/zh)
    - 레거시 필드: language_code (자동 생성)
    """
    return {
        "game_id": summary.game_id,
        "summary_type": summary.summary_type,            # Sprint 3 신규
        "review_language": summary.review_language,      # Sprint 3 신규
        # Sprint 3: 역호환성 유지 (기존 클라이언트 호환)
        # - review_language=None → language_code="unified"
        # - review_language="en" → language_code="en"
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
        "updated_at": summary.created_at.isoformat(),
    }
