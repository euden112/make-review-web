from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.models.domain import GameReviewSummary
from app.services.recommendation_targets import sanitize_player_targets

router = APIRouter()


async def _build_recommendation_targets(
    game_id: int,
    limit: int,
    db: AsyncSession,
) -> dict:
    """AI 요약이 생성한 game별 recommended_for(플레이어 유형 + 근거)를 서빙."""
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

    return {"game_id": game_id, "recommendations": []}


@router.get("/{game_id}/recommendation-targets")
async def get_recommendation_targets(
    game_id: int,
    limit: int = Query(4, ge=1, le=5),
    db: AsyncSession = Depends(get_db),
):
    """리뷰 근거 기반 추천 대상 유형을 반환."""
    return await _build_recommendation_targets(game_id, limit, db)
