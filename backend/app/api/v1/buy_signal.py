"""
구매 타이밍 시그널 API
GET /api/v1/games/{game_id}/buy-signal

판정 로직:
  is_good_timing = 할인 중 AND (긍정 회복 OR 역대 최저 부정 비율)
"""

import asyncio
from datetime import date

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.core.database import get_db
from app.models.domain import GamePlatformMap, Platform

router = APIRouter()

_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
_HISTOGRAM_URL = "https://store.steampowered.com/appreviewhistogram/{appid}?l=en"
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


async def _fetch_price(appid: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                _APPDETAILS_URL,
                params={"appids": appid, "cc": "kr", "filters": "price_overview"},
            )
            r.raise_for_status()
            data = r.json()
            game = data.get(str(appid), {})
            if not game.get("success"):
                return None
            return (game.get("data") or {}).get("price_overview")
        except Exception:
            return None


async def _fetch_histogram(appid: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(_HISTOGRAM_URL.format(appid=appid))
            r.raise_for_status()
            data = r.json()
            if data.get("success") != 1:
                return []
            results = data.get("results") or {}
            rollups = results.get("rollups") or results.get("recent") or []
            monthly = []
            for entry in rollups:
                ts = entry.get("date")
                if ts is None:
                    continue
                try:
                    month_start = date.fromtimestamp(int(ts))
                except (OSError, ValueError):
                    continue
                monthly.append({
                    "month_start": month_start,
                    "positive": int(entry.get("recommendations_up", 0)),
                    "negative": int(entry.get("recommendations_down", 0)),
                })
            monthly.sort(key=lambda x: x["month_start"])
            return monthly
        except Exception:
            return []


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


def _build_response(price: dict | None, sentiment: dict) -> dict:
    discount = int((price or {}).get("discount_percent", 0))
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

    original = (price or {}).get("initial")
    final = (price or {}).get("final")

    return {
        "is_good_timing": is_good_timing,
        "discount_percent": discount,
        "original_price": int(original) if original is not None else None,
        "final_price": int(final) if final is not None else None,
        "sale_ends_at": None,
        "sentiment_state": state,
        "reasons": reasons,
    }


@router.get("/{game_id}/buy-signal")
async def get_buy_signal(game_id: int, db: AsyncSession = Depends(get_db)):
    appid = await _get_steam_appid(game_id, db)
    if not appid:
        raise HTTPException(status_code=404, detail="Steam appid를 찾을 수 없음")

    price, monthly = await asyncio.gather(
        _fetch_price(appid),
        _fetch_histogram(appid),
    )
    sentiment = _analyze_sentiment(monthly)
    return _build_response(price, sentiment)
