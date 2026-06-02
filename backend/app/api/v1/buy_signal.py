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

from fastapi import APIRouter, Depends, HTTPException, Query
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


async def _read_json_many(keys: list[str]) -> dict[str, dict | None]:
    if not keys:
        return {}
    try:
        values = await redis_db.mget(keys)
    except Exception:
        return {key: None for key in keys}

    result: dict[str, dict | None] = {}
    for key, raw in zip(keys, values):
        try:
            result[key] = json.loads(raw) if raw else None
        except Exception:
            result[key] = None
    return result


def _parse_ids(ids: str) -> list[int]:
    parsed: list[int] = []
    seen: set[int] = set()
    for part in ids.split(","):
        value = part.strip()
        if not value.isdigit():
            continue
        game_id = int(value)
        if game_id not in seen:
            parsed.append(game_id)
            seen.add(game_id)
    return parsed


@router.get("/buy-signals/bulk")
async def get_buy_signals_bulk(
    ids: str = Query("", description="쉼표로 구분한 game_id 목록"),
    db: AsyncSession = Depends(get_db),
):
    game_ids = _parse_ids(ids)
    if not game_ids:
        return {}
    if len(game_ids) > 200:
        raise HTTPException(status_code=400, detail="한 번에 최대 200개까지 조회할 수 있습니다.")

    result_keys = {game_id: f"buy_signal:result:{game_id}" for game_id in game_ids}
    cached_results = await _read_json_many(list(result_keys.values()))
    signals: dict[int, dict] = {}
    missing_ids: list[int] = []

    for game_id in game_ids:
        cached = cached_results.get(result_keys[game_id])
        if cached is not None:
            signals[game_id] = cached
        else:
            missing_ids.append(game_id)

    if missing_ids:
        platform_row = (await db.execute(
            select(Platform).where(Platform.code == "steam")
        )).scalar_one_or_none()
        appids: dict[int, str] = {}
        if platform_row:
            rows = (await db.execute(
                select(GamePlatformMap).where(
                    and_(
                        GamePlatformMap.platform_id == platform_row.id,
                        GamePlatformMap.game_id.in_(missing_ids),
                    )
                )
            )).scalars().all()
            appids = {row.game_id: row.external_game_id for row in rows}

        price_keys = {game_id: _PRICE_KEY.format(game_id) for game_id in missing_ids}
        sentiment_keys = {game_id: _SENTIMENT_KEY.format(game_id) for game_id in missing_ids}
        prices = await _read_json_many(list(price_keys.values()))
        sentiments = await _read_json_many(list(sentiment_keys.values()))

        for game_id in missing_ids:
            appid = appids.get(game_id)
            if not appid:
                continue
            price = prices.get(price_keys[game_id])
            sentiment = sentiments.get(sentiment_keys[game_id])
            store_url = (price or {}).get("store_url") \
                or f"https://store.steampowered.com/app/{appid}"
            signal = build_signal(price, sentiment, store_url)
            signals[game_id] = signal
            await set_json_cache(result_keys[game_id], signal, _CACHE_TTL)

    return {str(game_id): signals[game_id] for game_id in game_ids if game_id in signals}


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
