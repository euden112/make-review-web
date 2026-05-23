"""
가격·여론 리프레셔 잡 (기획서 3-5b·9-3 BUG-3)

일 AI 배치와 분리된 경량 잡. Steam 할인은 거의 전부 17:00 UTC 경계에서
일 1회 토글되므로 **매일 17:05 UTC(전파 버퍼)에 1회 패스**한다.
가격은 멀티 appid 배치로 조회(호출량 ~20배 절감), 여론(histogram)은
appid별로 같은 패스에서 일단위 갱신. 1차 실패분만 2차 패스에서 재시도.

핵심 원칙: 사용자 요청은 Steam을 직접 호출하지 않고 이 잡이 채운
Redis 스냅샷만 읽는다 → 사용자 트래픽과 외부 호출량이 독립
(레이트리밋 노출 0, graceful degrade).

실행:
  python -m app.jobs.price_refresher --once    # 1회 패스 후 종료
  python -m app.jobs.price_refresher --loop    # 매일 17:05 UTC 정렬 반복

PYTHONPATH에 crawling/steam 포함 필요 (docker-compose backend.environment).
"""

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.future import select

from app.core.database import AsyncSessionLocal
from app.core.redis_client import redis_db
from app.models.domain import GamePlatformMap, Platform
from app.services.buy_signal_logic import analyze_sentiment

from appdetails_crawler import fetch_price_info_batch
from histogram_crawler import fetch_histogram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("price_refresher")

# Steam 가격 변경 경계(≈17:00 UTC) + 전파 버퍼. 일 1회만 의미 있는 변동.
_REFRESH_HOUR_UTC = int(os.getenv("PRICE_REFRESH_HOUR_UTC", "17"))
_REFRESH_MINUTE_UTC = int(os.getenv("PRICE_REFRESH_MINUTE_UTC", "5"))
# 여론 appid별 호출 스로틀 (배치 불가 — 한도 내 완만 순회).
_SENTIMENT_THROTTLE = float(os.getenv("SENTIMENT_THROTTLE", "1.0"))
# 2차 재시도 전 대기 (1차 일시 오류·레이트리밋 회복 여유).
_RETRY_DELAY = float(os.getenv("PRICE_RETRY_DELAY", "60"))
_PRICE_TTL = 30 * 3600         # 잡 중단 시 다음 패스(>24h) 전 만료 → API가 stale degrade
_SENTIMENT_TTL = 8 * 24 * 3600

PRICE_KEY = "buy_signal:price:{}"
SENTIMENT_KEY = "buy_signal:sentiment:{}"


def _store_url(appid: str) -> str:
    return f"https://store.steampowered.com/app/{appid}"


def _seconds_until_next_run(now: datetime | None = None) -> float:
    """다음 17:05 UTC까지 남은 초."""
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=_REFRESH_HOUR_UTC, minute=_REFRESH_MINUTE_UTC,
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _steam_catalog() -> list[tuple[int, str]]:
    """(game_id, steam_appid) 목록."""
    async with AsyncSessionLocal() as db:
        platform = (await db.execute(
            select(Platform).where(Platform.code == "steam")
        )).scalar_one_or_none()
        if not platform:
            return []
        rows = (await db.execute(
            select(GamePlatformMap).where(GamePlatformMap.platform_id == platform.id)
        )).scalars().all()
        return [(r.game_id, r.external_game_id) for r in rows if r.external_game_id]


async def _store_price(game_id: int, appid: str, price) -> None:
    await redis_db.set(
        PRICE_KEY.format(game_id),
        json.dumps({
            "discount_percent": price.discount_percent,
            "original_price": price.original_price,
            "final_price": price.final_price,
            "is_on_sale": price.is_on_sale,
            "price_as_of": price.fetched_at,
            "store_url": _store_url(appid),
        }),
        ex=_PRICE_TTL,
    )


async def _refresh_prices(catalog: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """가격 배치 갱신. 반환: 스냅샷을 못 받은 (game_id, appid) — 2차 재시도 대상."""
    appid_to_game = {appid: gid for gid, appid in catalog}
    appids = list(appid_to_game.keys())
    prices = await asyncio.to_thread(fetch_price_info_batch, appids, "kr")
    failed: list[tuple[int, str]] = []
    for appid, game_id in appid_to_game.items():
        price = prices.get(appid)
        if price is None:
            # last-known 유지 (덮어쓰지 않음) — graceful degrade
            failed.append((game_id, appid))
            continue
        await _store_price(game_id, appid, price)
    return failed


async def _refresh_sentiment(catalog: list[tuple[int, str]]) -> int:
    """여론 일단위 갱신 (appid별, 배치 불가). 실패 game은 last-known 유지."""
    ok = 0
    for game_id, appid in catalog:
        try:
            monthly = await asyncio.to_thread(fetch_histogram, appid)
            if not monthly:
                continue
            snap = analyze_sentiment(monthly)
            snap["as_of"] = datetime.now(timezone.utc).replace(
                microsecond=0).isoformat()
            await redis_db.set(SENTIMENT_KEY.format(game_id),
                               json.dumps(snap), ex=_SENTIMENT_TTL)
            ok += 1
        except Exception as e:  # 한 게임 실패가 패스를 멈추지 않게
            logger.warning("sentiment refresh failed game=%s: %s", game_id, e)
        await asyncio.sleep(_SENTIMENT_THROTTLE)
    return ok


async def refresh_once() -> dict:
    """일일 패스: 가격 배치 → 실패분만 2차 재시도 → 여론 일단위 갱신."""
    catalog = await _steam_catalog()
    if not catalog:
        logger.warning("steam catalog empty — nothing to refresh")
        return {"price_ok": 0, "price_fail": 0, "sentiment_ok": 0, "total": 0}

    failed = await _refresh_prices(catalog)
    if failed:
        logger.info("price 1st pass: %d failed — retry after %.0fs",
                    len(failed), _RETRY_DELAY)
        await asyncio.sleep(_RETRY_DELAY)
        failed = await _refresh_prices(failed)  # 실패분만 재시도

    sentiment_ok = await _refresh_sentiment(catalog)

    result = {
        "price_ok": len(catalog) - len(failed),
        "price_fail": len(failed),
        "sentiment_ok": sentiment_ok,
        "total": len(catalog),
    }
    logger.info("refresh pass done: %s", result)
    return result


async def run_loop() -> None:
    while True:
        wait = _seconds_until_next_run()
        logger.info("next refresh in %.0fs (%.1fh) @ %02d:%02d UTC",
                    wait, wait / 3600, _REFRESH_HOUR_UTC, _REFRESH_MINUTE_UTC)
        await asyncio.sleep(wait)
        try:
            await refresh_once()
        except Exception:
            logger.exception("refresh pass crashed — retry next day")


def main() -> None:
    parser = argparse.ArgumentParser(description="가격·여론 리프레셔 잡")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="1회 패스 후 종료")
    g.add_argument("--loop", action="store_true", help="매일 17:05 UTC 정렬 반복")
    args = parser.parse_args()
    asyncio.run(refresh_once() if args.once else run_loop())


if __name__ == "__main__":
    main()
