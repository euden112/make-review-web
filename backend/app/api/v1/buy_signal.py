"""
구매 타이밍 시그널 API
GET /api/v1/games/{game_id}/buy-signal

판정 로직:
  is_good_timing = 할인 중 AND (긍정 회복 OR 역대 최저 부정 비율)
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.core.database import get_db
from app.core.redis_client import get_json_cache, set_json_cache
from app.models.domain import GamePlatformMap, Platform

_CACHE_TTL = 6 * 3600  # 가격·여론은 일 단위 변동 → 6시간

# crawling/steam 은 PYTHONPATH에 포함됨 (docker-compose backend.environment).
# 기획서 3-2/6: histogram·appdetails 크롤러를 기능 A에서 재사용 (BUG-2 통합).
from appdetails_crawler import fetch_price_info, PriceInfo
from histogram_crawler import fetch_histogram

router = APIRouter()

_HISTOGRAM_THRESHOLD = 0.20
_MIN_MONTHLY_VOLUME = 20


async def _get_steam_appid(game_id: int, db: AsyncSession) -> str | None:
    platform_row = (await db.execute(
        select(Platform).where(Platform.code == "steam")
    )).scalar_one_or_none()
    if not platform_row:
        return None
    row = (await db.execute(
        select(GamePlatformMap).where(
            and_(
                GamePlatformMap.game_id == game_id,
                GamePlatformMap.platform_id == platform_row.id,
            )
        )
    )).scalar_one_or_none()
    return row.external_game_id if row else None


def _analyze_sentiment(monthly: list[dict]) -> dict:
    valid = [
        m for m in monthly
        if m["positive"] + m["negative"] >= _MIN_MONTHLY_VOLUME
    ]
    if not valid:
        return {"state": "unknown", "neg_ratio": None, "delta": None, "is_at_minimum": False}

    last = valid[-1]
    last_total = last["positive"] + last["negative"]
    last_neg_ratio = last["negative"] / last_total

    state = "stable"
    delta = None
    if len(valid) >= 2:
        prev = valid[-2]
        prev_total = prev["positive"] + prev["negative"]
        delta = last_neg_ratio - (prev["negative"] / prev_total)
        if delta <= -_HISTOGRAM_THRESHOLD:
            state = "positive_recovery"
        elif delta >= _HISTOGRAM_THRESHOLD:
            state = "negative_spike"

    min_neg = min(m["negative"] / (m["positive"] + m["negative"]) for m in valid)
    is_at_minimum = last_neg_ratio <= min_neg * 1.1

    return {
        "state": state,
        "neg_ratio": round(last_neg_ratio, 4),
        "delta": round(delta, 4) if delta is not None else None,
        "is_at_minimum": is_at_minimum,
    }


def _build_response(price: PriceInfo | None, sentiment: dict) -> dict:
    # price 금액은 appdetails_crawler가 이미 표시 단위로 환산함(BUG-1 //100).
    discount = price.discount_percent if price else 0
    is_on_sale = discount > 0
    state = sentiment["state"]
    is_positive = state == "positive_recovery" or sentiment["is_at_minimum"]
    is_good_timing = is_on_sale and is_positive

    reasons = []
    if is_on_sale:
        reasons.append(f"{discount}% 할인 중")
    if state == "positive_recovery":
        delta_pct = abs(sentiment["delta"] or 0) * 100
        reasons.append(f"최근 평가 긍정 회복 (+{delta_pct:.0f}%p 개선)")
    if sentiment["is_at_minimum"] and state != "positive_recovery":
        reasons.append("현재 부정 비율 역대 최저 구간")
    if not is_on_sale:
        reasons.append("현재 할인 없음")
    if state == "negative_spike":
        reasons.append("최근 부정 여론 급증 — 구매 전 확인 권장")

    return {
        "is_good_timing": is_good_timing,
        "discount_percent": discount,
        "original_price": price.original_price if price else None,
        "final_price": price.final_price if price else None,
        "sale_ends_at": price.sale_ends_at if price else None,
        "sentiment_state": state,
        "reasons": reasons,
    }


@router.get("/{game_id}/buy-signal")
async def get_buy_signal(game_id: int, db: AsyncSession = Depends(get_db)):
    cache_key = f"buy_signal:{game_id}"
    cached = await get_json_cache(cache_key)
    if cached is not None:
        return cached

    appid = await _get_steam_appid(game_id, db)
    if not appid:
        raise HTTPException(status_code=404, detail="Steam appid를 찾을 수 없음")

    # 동기 크롤러(requests 기반)를 스레드로 위임해 이벤트 루프 블로킹 방지
    price, monthly = await asyncio.gather(
        asyncio.to_thread(fetch_price_info, appid, "kr"),
        asyncio.to_thread(fetch_histogram, appid),
    )
    sentiment = _analyze_sentiment(monthly)
    result = _build_response(price, sentiment)
    await set_json_cache(cache_key, result, _CACHE_TTL)
    return result
