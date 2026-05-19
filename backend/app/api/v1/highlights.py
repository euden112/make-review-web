import asyncio
import re
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.core.database import get_db
from app.core.redis_client import get_json_cache, set_json_cache
from app.models.domain import ExternalReview
from app.api.v1.translate import _translate_one

router = APIRouter()

_CACHE_TTL = 24 * 3600  # 재수집 시에만 변경 → 24시간

# BUG-13: highlights 응답의 linked_aspect는 이 집합 내 값만 반환 (CATEGORY_LABELS 정합)
_KNOWN_CATEGORIES = {"graphics", "controls", "optimization", "content", "price_value"}

# 한·영 감정 강도 키워드 (BUG-6: 영어 리뷰 저평가 보정, IGNORECASE)
_EMOTION_RE = re.compile(
    r'인생|최고|소름|울었|순삭|중독|명작|감동|눈물|환상|전설|완벽|압도|걸작|역대급|불후'
    r'|masterpiece|emotional|cried|tears|addict|unforgettable|breathtaking'
    r'|incredible|amazing|best\s+game|life\s*-?\s*changing|goosebumps|flawless'
    r'|stunning|phenomenal|obsessed|hooked',
    re.IGNORECASE,
)


def _highlight_score(review: ExternalReview) -> float:
    is_positive = bool(review.is_recommended) or (
        review.normalized_score_100 is not None and float(review.normalized_score_100) >= 70
    )
    if not is_positive:
        return 0.0

    text = review.review_text_clean or ""
    helpful = max(review.helpful_count or 0, 1)
    keyword_hits = len(_EMOTION_RE.findall(text))
    exclamation = text.count('!')
    length_bonus = min(len(text) / 200, 2.0)

    emotion = 1.0 + keyword_hits * 2.0 + exclamation * 0.5 + length_bonus
    return helpful * emotion


def _linked_aspect(review: ExternalReview) -> str | None:
    cats = review.review_categories_json
    if not cats or not isinstance(cats, list):
        return None
    for cat in cats:
        if isinstance(cat, dict) and cat.get("sentiment") == "positive":
            category = cat.get("category")
            # BUG-13: CATEGORY_LABELS 미정의 카테고리는 raw 문자열 대신 None 반환
            return category if category in _KNOWN_CATEGORIES else None
    return None


def _is_korean(text: str) -> bool:
    korean = len(re.findall(r'[가-힣]', text))
    return korean / max(len(text), 1) >= 0.15


async def _display_text(text: str) -> str:
    """비한국어 텍스트는 번역 (BUG-12). translate._translate_one이 Redis 캐시."""
    if not text or _is_korean(text):
        return text
    return await _translate_one(text)


@router.get("/{game_id}/highlights")
async def get_highlights(
    game_id: int,
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
):
    cache_key = f"highlights:{game_id}:{limit}"
    cached = await get_json_cache(cache_key)
    if cached is not None:
        return cached

    # BUG-5: ORDER BY 없는 limit는 임의 부분집합 → helpful_count 높은 순으로
    # 정렬 후 상위 3000개를 평가 (진짜 명장면 후보가 누락되지 않도록)
    result = await db.execute(
        select(ExternalReview).where(
            and_(
                ExternalReview.game_id == game_id,
                ExternalReview.is_deleted == False,
                ExternalReview.review_text_clean != None,
                ExternalReview.review_text_clean != "",
            )
        ).order_by(
            ExternalReview.helpful_count.desc(),
            ExternalReview.id.desc(),
        ).limit(3000)
    )
    reviews = result.scalars().all()

    if not reviews:
        raise HTTPException(status_code=404, detail="리뷰 없음")

    scored = [(r, _highlight_score(r)) for r in reviews]
    scored = [(r, s) for r, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)

    top = scored[:limit]
    # BUG-12: 비한국어 원문은 번역 후 노출 (translate._translate_one 캐시 활용)
    texts = await asyncio.gather(*[_display_text(r.review_text_clean or "") for r, _ in top])

    response = {
        "highlights": [
            {
                "review_id": r.id,
                "text": translated,
                "playtime_hours": float(r.playtime_hours) if r.playtime_hours else None,
                "helpful_count": r.helpful_count,
                "linked_aspect": _linked_aspect(r),
            }
            for (r, _), translated in zip(top, texts)
        ]
    }
    await set_json_cache(cache_key, response, _CACHE_TTL)
    return response
