"""
구매 타이밍 시그널 API (read-only) — 기획서 3-4·3-5b·9-3 BUG-3

판정 로직:
  is_good_timing = 할인 중 AND 긍정 비율 유의미 상승
                   AND 가격 스냅샷이 신선함 (신선도 게이팅)

핵심 원칙: 본 엔드포인트는 Steam을 직접 호출하지 않는다.
가격 전용 리프레셔 잡(app.jobs.price_refresher)이 채운 Redis 스냅샷만
읽으므로 사용자 트래픽이 외부 레이트리밋에 노출되지 않는다.
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.core.database import get_db
from app.core.redis_client import redis_db, get_json_cache, set_json_cache
from app.models.domain import GamePlatformMap, Platform
from app.services.buy_signal_logic import build_signal

router = APIRouter()

_CACHE_TTL = 600  # 조합 결과 경량 캐시 (스냅샷 자체는 리프레셔가 관리)
_PRICE_KEY = "buy_signal:price:{}"
_SENTIMENT_KEY = "buy_signal:sentiment:{}"


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


async def _read_snapshot(key: str) -> dict | None:
    try:
        raw = await redis_db.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


@router.get("/{game_id}/buy-signal")
async def get_buy_signal(game_id: int, db: AsyncSession = Depends(get_db)):
    cache_key = f"buy_signal:result:{game_id}"
    cached = await get_json_cache(cache_key)
    if cached is not None:
        return cached

    appid = await _get_steam_appid(game_id, db)
    if not appid:
        raise HTTPException(status_code=404, detail="Steam appid를 찾을 수 없음")

    # Redis 스냅샷만 읽음 — Steam 직접 호출 없음 (레이트리밋 노출 0)
    price = await _read_snapshot(_PRICE_KEY.format(game_id))
    sentiment = await _read_snapshot(_SENTIMENT_KEY.format(game_id))
    store_url = (price or {}).get("store_url") \
        or f"https://store.steampowered.com/app/{appid}"

    result = build_signal(price, sentiment, store_url)
    await set_json_cache(cache_key, result, _CACHE_TTL)
    return result
