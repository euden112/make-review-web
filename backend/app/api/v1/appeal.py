from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.models.domain import ExternalReview, GameReviewSummary
from app.services.recommendation_targets import sanitize_player_targets

router = APIRouter()


_CATEGORY_ALIASES = {
    "그래픽": "graphics",
    "비주얼": "graphics",
    "graphics": "graphics",
    "visual": "graphics",
    "조작": "controls",
    "조작감": "controls",
    "controls": "controls",
    "control": "controls",
    "최적화": "optimization",
    "성능": "optimization",
    "optimization": "optimization",
    "performance": "optimization",
    "콘텐츠": "content",
    "스토리": "content",
    "content": "content",
    "story": "content",
    "가격": "price_value",
    "가성비": "price_value",
    "value": "price_value",
    "price": "price_value",
}

_RECOMMENDATION_COPY = {
    "graphics": {
        "label": "분위기와 비주얼 몰입을 중시하는 플레이어",
        "summary": "긍정 리뷰에서 그래픽과 세계 표현이 몰입을 돕는 요소로 반복 확인됩니다.",
    },
    "controls": {
        "label": "손맛과 숙련 과정을 즐기는 플레이어",
        "summary": "긍정 리뷰에서 조작감과 전투 흐름이 반복 학습의 재미로 확인됩니다.",
    },
    "optimization": {
        "label": "쾌적한 실행과 안정성을 중시하는 플레이어",
        "summary": "긍정 리뷰에서 성능과 안정성이 플레이 흐름을 받쳐 준다는 반응이 확인됩니다.",
    },
    "content": {
        "label": "탐험과 긴 플레이 볼륨을 원하는 플레이어",
        "summary": "긍정 리뷰에서 탐험, 이야기, 즐길 거리의 밀도가 만족 요인으로 나타납니다.",
    },
    "price_value": {
        "label": "가격 대비 오래 즐길 게임을 찾는 플레이어",
        "summary": "긍정 리뷰에서 플레이 시간과 제공 경험 대비 만족감이 반복적으로 확인됩니다.",
    },
}

_SORT_TIEBREAK = {
    "content": 5,
    "controls": 4,
    "graphics": 3,
    "price_value": 2,
    "optimization": 1,
}

@dataclass
class _CategoryStat:
    evidence_count: int = 0
    helpful_weight: int = 0


def _is_positive(review: ExternalReview) -> bool:
    if review.is_recommended is True:
        return True
    if review.normalized_score_100 is not None and float(review.normalized_score_100) >= 75:
        return True
    return False


def _positive_categories(review: ExternalReview) -> set[str]:
    cats = review.review_categories_json
    if not isinstance(cats, list):
        return set()

    result: set[str] = set()
    for item in cats:
        raw_category = None
        sentiment = None
        if isinstance(item, dict):
            raw_category = item.get("category")
            sentiment = item.get("sentiment")
        elif isinstance(item, str):
            raw_category = item

        if sentiment is not None and str(sentiment).lower() != "positive":
            continue

        key = _CATEGORY_ALIASES.get(str(raw_category or "").strip().lower())
        if key in _RECOMMENDATION_COPY:
            result.add(key)

    return result


async def _build_recommendation_targets(
    game_id: int,
    limit: int,
    db: AsyncSession,
) -> dict:
    """추천 대상 유형 반환.

    1순위: AI 요약이 생성한 game별 recommended_for(플레이어 유형 + 근거)를 그대로 서빙.
    폴백: recommended_for 미생성(구버전 요약)이면 긍정 리뷰 카테고리 추론을 사용.
    """
    stored = (await db.execute(
        select(GameReviewSummary.recommended_for_json).where(
            and_(
                GameReviewSummary.game_id == game_id,
                GameReviewSummary.summary_type == "unified",
                GameReviewSummary.review_language.is_(None),
                GameReviewSummary.is_current == True,
            )
        )
    )).scalar_one_or_none()
    if isinstance(stored, list) and stored:
        stored = sanitize_player_targets(stored, limit=limit)
        recommendations = [
            {
                "type": "recommended",
                "label": (item.get("label") or "").strip(),
                "category": None,
                "basis_categories": [],
                "summary": (item.get("reason") or "").strip(),
                "evidence_count": 0,
            }
            for item in stored
            if isinstance(item, dict) and (item.get("label") or "").strip()
        ][:limit]
        if recommendations:
            return {"game_id": game_id, "recommendations": recommendations}

    # ── 폴백: 카테고리 추론 ──────────────────────────────────────────────────────
    rows = (await db.execute(
        select(ExternalReview)
        .where(
            and_(
                ExternalReview.game_id == game_id,
                ExternalReview.is_deleted == False,
                ExternalReview.review_text_clean != None,
                ExternalReview.review_text_clean != "",
            )
        )
        .order_by(ExternalReview.helpful_count.desc(), ExternalReview.id.desc())
        .limit(2000)
    )).scalars().all()

    stats: dict[str, _CategoryStat] = defaultdict(_CategoryStat)
    for review in rows:
        if not _is_positive(review):
            continue
        for category in _positive_categories(review):
            stats[category].evidence_count += 1
            stats[category].helpful_weight += max(int(review.helpful_count or 0), 0) + 1

    ranked = sorted(
        stats.items(),
        key=lambda item: (
            item[1].evidence_count,
            item[1].helpful_weight,
            _SORT_TIEBREAK.get(item[0], 0),
        ),
        reverse=True,
    )[:limit]

    if not ranked:
        raise HTTPException(status_code=404, detail="추천 대상을 만들 수 있는 긍정 카테고리 데이터가 없습니다.")

    recommendations = [
        {
            "type": "recommended",
            "label": _RECOMMENDATION_COPY[category]["label"],
            "category": category,
            "basis_categories": [category],
            "summary": _RECOMMENDATION_COPY[category]["summary"],
            "evidence_count": stat.evidence_count,
        }
        for category, stat in ranked
    ]

    return {
        "game_id": game_id,
        "recommendations": recommendations,
    }


@router.get("/{game_id}/recommendation-targets")
async def get_recommendation_targets(
    game_id: int,
    limit: int = Query(4, ge=1, le=5),
    db: AsyncSession = Depends(get_db),
):
    """리뷰 근거 기반 추천 대상 유형을 반환."""
    return await _build_recommendation_targets(game_id, limit, db)
