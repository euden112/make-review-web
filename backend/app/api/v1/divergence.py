"""
기능 D — 유저/평론 괴리 지표 API
GET /api/v1/games/{game_id}/divergence

기획서 8: 통합 점수의 오도 방지. 저장된 user/critic 요약에서 괴리를
재산출(파이프라인 재실행 없음 — 8-2 "재배치 중심").

8-4 동적·비대칭 노출:
  1. one_liner는 항상 괴리 인지형
  2. show_dual_track은 괴리 임계 초과 시에만 (대부분 게임은 잉여 방지)
  3. user↑critic↓ → 숨은 호평작(2차 전환) / critic↑user↓ → 구매 주의
  4. 톤 가드: 점수 차·근거 기반 사실 서술만 (편향적 단정 금지)
"""

from fastapi import APIRouter, Depends
from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.core.auth import require_api_key
from app.models.domain import GameReviewSummary, CriticSummary

router = APIRouter()

# 0~100 점수축 기준. 이 차이 이상이면 2트랙 강조 (8-4 #2)
_DIVERGENCE_THRESHOLD = 15.0


@router.get("/{game_id}/divergence", dependencies=[Depends(require_api_key)])
async def get_divergence(game_id: int, db: AsyncSession = Depends(get_db)):
    user_row = (await db.execute(
        select(GameReviewSummary).where(
            and_(
                GameReviewSummary.game_id == game_id,
                GameReviewSummary.summary_type == "unified",
                GameReviewSummary.review_language.is_(None),
                GameReviewSummary.is_current == True,
            )
        )
    )).scalar_one_or_none()
    critic_row = (await db.execute(
        select(CriticSummary).where(CriticSummary.game_id == game_id)
    )).scalar_one_or_none()

    user_score = float(user_row.sentiment_score) if user_row and user_row.sentiment_score is not None else None
    critic_score = float(critic_row.score) if critic_row and critic_row.score is not None else None

    # 한쪽이라도 없으면 괴리 판정 불가 — 잉여 노출 방지 (8-4 #2)
    if user_score is None or critic_score is None:
        return {
            "has_divergence_data": False,
            "user_score": user_score,
            "critic_score": critic_score,
            "divergence": None,
            "divergence_type": "insufficient",
            "one_liner": None,
            "show_dual_track": False,
        }

    divergence = round(user_score - critic_score, 1)  # 부호: 양수=유저가 더 후함
    abs_div = abs(divergence)
    u, c, d = round(user_score), round(critic_score), round(abs_div)

    if divergence >= _DIVERGENCE_THRESHOLD:
        # 유저↑ 평론↓ → 숨은 호평작 (2차 전환 레버, 8-4 #3)
        dtype = "user_favors"
        one_liner = f"평론 {c}점보다 유저 평가 {u}점이 {d}점 높은 '숨은 호평작' — 평단과 취향이 갈리는 작품"
    elif divergence <= -_DIVERGENCE_THRESHOLD:
        # 평론↑ 유저↓ → 구매 주의 (8-4 #3)
        dtype = "critic_favors"
        one_liner = f"평론은 {c}점으로 호평이나 유저 평가가 {u}점으로 {d}점 낮음 — 구매 전 유저 의견 확인 권장"
    else:
        dtype = "aligned"
        one_liner = f"평론 {c}점과 유저 {u}점 평가가 대체로 일치 — 호불호 적음"

    return {
        "has_divergence_data": True,
        "user_score": user_score,
        "critic_score": critic_score,
        "divergence": divergence,
        "divergence_type": dtype,
        "one_liner": one_liner,
        "show_dual_track": abs_div >= _DIVERGENCE_THRESHOLD,
    }
