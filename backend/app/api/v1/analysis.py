"""Sprint 4: 플레이타임별 여론 분석 및 비평가 반응 엔드포인트."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.models.domain import PlaytimeAnalysis, CriticSummary

logger = logging.getLogger(__name__)
router = APIRouter()

MIN_CRITIC_REVIEWS = 10


def _format_label(early_max: float | None, mid_max: float | None, bucket: str) -> str:
    if early_max is None:
        return bucket
    if bucket == "early":
        return f"초반 (~{early_max:.0f}시간)"
    if bucket == "mid":
        return f"중반 ({early_max:.0f}~{mid_max:.0f}시간)"
    return f"후반 ({mid_max:.0f}시간+)"


@router.get("/{game_id}/playtime-analysis")
async def get_playtime_analysis(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """플레이타임 버킷별 여론 분석 반환."""
    row = (await db.execute(
        select(PlaytimeAnalysis).where(PlaytimeAnalysis.game_id == game_id)
    )).scalar_one_or_none()

    if not row:
        raise HTTPException(status_code=404, detail="플레이타임 분석 데이터가 없습니다.")

    thresholds = row.bucket_thresholds or {}
    early_max  = thresholds.get("early_max")
    mid_max    = thresholds.get("mid_max")

    def serialize_bucket(prefix: str) -> dict | None:
        summary  = getattr(row, f"{prefix}_summary")
        sentiment = getattr(row, f"{prefix}_sentiment")
        score    = getattr(row, f"{prefix}_score")
        pros     = getattr(row, f"{prefix}_pros")
        cons     = getattr(row, f"{prefix}_cons")
        keywords = getattr(row, f"{prefix}_keywords")
        count    = getattr(row, f"{prefix}_review_count")

        if summary is None:
            return {
                "label": _format_label(early_max, mid_max, prefix),
                "data_available": False,
                "review_count": count or 0,
            }

        return {
            "label":            _format_label(early_max, mid_max, prefix),
            "data_available":   True,
            "sentiment_overall": sentiment,
            "sentiment_score":  float(score) if score is not None else None,
            "pros":             pros or [],
            "cons":             cons or [],
            "keywords":         keywords or [],
            "summary":          summary,
            "review_count":     count or 0,
        }

    return {
        "game_id":          game_id,
        "bucket_thresholds": thresholds,
        "buckets": {
            "early": serialize_bucket("early"),
            "mid":   serialize_bucket("mid"),
            "late":  serialize_bucket("late"),
        },
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/{game_id}/critic-summary")
async def get_critic_summary(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """비평가 반응 요약 반환."""
    row = (await db.execute(
        select(CriticSummary).where(CriticSummary.game_id == game_id)
    )).scalar_one_or_none()

    if not row:
        raise HTTPException(status_code=404, detail="비평가 반응 데이터가 없습니다.")

    review_count = row.review_count or 0
    if review_count < MIN_CRITIC_REVIEWS:
        raise HTTPException(
            status_code=404,
            detail=f"비평가 리뷰 수 부족 ({review_count}건, 최소 {MIN_CRITIC_REVIEWS}건 필요)",
        )

    return {
        "game_id":          game_id,
        "review_count":     review_count,
        "sentiment_overall": row.sentiment,
        "sentiment_score":  float(row.score) if row.score is not None else None,
        "pros":             row.pros or [],
        "cons":             row.cons or [],
        "keywords":         row.keywords or [],
        "summary":          row.summary,
        "created_at":       row.created_at.isoformat() if row.created_at else None,
    }
