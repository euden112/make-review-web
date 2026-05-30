"""Sprint 4: 플레이타임별 여론 분석 및 비평가 반응 엔드포인트."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.core.redis_client import get_json_cache, set_json_cache
from app.models.domain import PlaytimeAnalysis, CriticSummary, UserSummary

logger = logging.getLogger(__name__)
router = APIRouter()

_CACHE_TTL = 86400  # 24시간 (파이프라인 재실행 시 무효화)


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
    cache_key = f"playtime_analysis:{game_id}"
    cached = await get_json_cache(cache_key)
    if cached is not None:
        return cached

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
        review_count = getattr(row, f"{prefix}_review_count", None)

        if summary is None:
            # 안전망(방안 C): LLM 요약이 없어도 실제 추천비율(score)+리뷰수가 있으면
            # 점수/카운트는 노출해 차트가 비지 않게 한다. 요약 텍스트만 비운다.
            if score is not None and review_count:
                return {
                    "label": _format_label(early_max, mid_max, prefix),
                    "data_available": True,
                    "sentiment_overall": sentiment,
                    "sentiment_score": float(score),
                    "review_count": review_count,
                    "pros": [],
                    "cons": [],
                    "keywords": [],
                    "summary": None,
                }
            return {
                "label": _format_label(early_max, mid_max, prefix),
                "data_available": False,
            }

        return {
            "label":            _format_label(early_max, mid_max, prefix),
            "data_available":   True,
            "sentiment_overall": sentiment,
            "sentiment_score":  float(score) if score is not None else None,
            "review_count":     getattr(row, f"{prefix}_review_count", None),
            "pros":             pros or [],
            "cons":             cons or [],
            "keywords":         keywords or [],
            "summary":          summary,
        }

    result = {
        "game_id":          game_id,
        "bucket_thresholds": thresholds,
        "buckets": {
            "early": serialize_bucket("early"),
            "mid":   serialize_bucket("mid"),
            "late":  serialize_bucket("late"),
        },
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    await set_json_cache(cache_key, result, _CACHE_TTL)
    return result


@router.get("/{game_id}/critic-summary")
async def get_critic_summary(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """비평가 반응 요약 반환."""
    cache_key = f"critic_summary:{game_id}"
    cached = await get_json_cache(cache_key)
    if cached is not None:
        return cached

    row = (await db.execute(
        select(CriticSummary).where(CriticSummary.game_id == game_id)
    )).scalar_one_or_none()

    if not row:
        raise HTTPException(status_code=404, detail="비평가 반응 데이터가 없습니다.")

    result = {
        "game_id":          game_id,
        "sentiment_overall": row.sentiment,
        "sentiment_score":  float(row.score) if row.score is not None else None,
        "pros":             row.pros or [],
        "cons":             row.cons or [],
        "keywords":         row.keywords or [],
        "summary":          row.summary,
        "created_at":       row.created_at.isoformat() if row.created_at else None,
    }
    await set_json_cache(cache_key, result, _CACHE_TTL)
    return result


@router.get("/{game_id}/user-summary")
async def get_user_summary(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """유저 리뷰 요약 반환 (B안: unified 본문 폐지 후 user 전용 섹션의 데이터원)."""
    cache_key = f"user_summary:{game_id}"
    cached = await get_json_cache(cache_key)
    if cached is not None:
        return cached

    row = (await db.execute(
        select(UserSummary).where(UserSummary.game_id == game_id)
    )).scalar_one_or_none()

    if not row:
        raise HTTPException(status_code=404, detail="유저 리뷰 요약 데이터가 없습니다.")

    result = {
        "game_id":          game_id,
        "sentiment_overall": row.sentiment,
        "sentiment_score":  float(row.score) if row.score is not None else None,
        "pros":             row.pros or [],
        "cons":             row.cons or [],
        "keywords":         row.keywords or [],
        "summary":          row.summary,
        "created_at":       row.created_at.isoformat() if row.created_at else None,
    }
    await set_json_cache(cache_key, result, _CACHE_TTL)
    return result
