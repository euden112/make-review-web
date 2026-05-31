"""Steam 증분 리뷰 크롤 잡 (스케줄러용)

매일 AI 요약 배치 직전에, 등록된 Steam 게임을 **최신순(recent)으로 얕게** 크롤해
백엔드 ingestion(`POST /api/v1/reviews/steam`)으로 전송한다.

증분 정확성은 크롤 단계가 아니라 DB 계층이 보장한다:
  - ingestion이 (platform_id, game_id, source_review_key) 유니크키로 upsert →
    이미 있는 리뷰는 UPDATE되고 중복 행이 생기지 않는다.
  - 신규 리뷰만 새 external_reviews.id를 얻고, summary 커서가 그 id만 요약한다.
따라서 매일 같은 recent 윈도우를 재전송해도 안전하며, "지난번 이후"만 요약된다.
recent 윈도우 깊이(CRAWL_RECENT_PER_LANG)는 하루치 신규 리뷰를 덮을 만큼이면 충분하다.

Metacritic은 playwright(브라우저) 비용이 크고 평론가 리뷰가 거의 고정이라 제외한다(수동 유지).

게임별 meta는 DB의 platform_meta_json(커버/태그 등)을 그대로 재전송해 보존한다.
빈 meta를 보내면 ingestion이 platform_meta_json을 덮어써 커버·태그가 유실되기 때문이다.

실행:
  python -m app.jobs.review_crawler_job --once    # 1회 크롤 후 종료
  python -m app.jobs.review_crawler_job --loop    # 매일 17:05 UTC 정렬 반복
"""

import argparse
import asyncio
import logging
import os
from datetime import datetime

import httpx
from sqlalchemy.future import select

from app.core.database import AsyncSessionLocal
from app.models.domain import Game, GamePlatformMap, Platform
from app.jobs.price_refresher import _seconds_until_next_run

# steam_crawler는 crawling/steam 에 있고 PYTHONPATH에 포함된다(docker-compose).
from steam_crawler import fetch_raw_reviews, parse_and_dedup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("review_crawler_job")

# 언어당 최신순 수집 깊이. 하루치 신규 리뷰를 덮을 정도면 충분(중복은 upsert가 흡수).
RECENT_PER_LANG = int(os.getenv("CRAWL_RECENT_PER_LANG", "100"))
_LANGS = ("koreana", "english")


async def _steam_catalog() -> list[tuple[int, str, str, dict]]:
    """(game_id, steam_appid, slug, platform_meta_json) 목록."""
    async with AsyncSessionLocal() as db:
        platform = (await db.execute(
            select(Platform).where(Platform.code == "steam")
        )).scalar_one_or_none()
        if not platform:
            return []

        rows = (await db.execute(
            select(GamePlatformMap).where(GamePlatformMap.platform_id == platform.id)
        )).scalars().all()
        games = {g.id: g for g in (await db.execute(select(Game))).scalars().all()}

        catalog: list[tuple[int, str, str, dict]] = []
        for r in rows:
            if not r.external_game_id:
                continue
            g = games.get(r.game_id)
            slug = (g.normalized_title if g and g.normalized_title else str(r.external_game_id))
            catalog.append((r.game_id, r.external_game_id, slug, dict(r.platform_meta_json or {})))
        return catalog


def _collect_recent(app_id: str) -> list[dict]:
    """언어별 최신순 리뷰를 수집·파싱·중복제거(블로킹 — requests 기반)."""
    seen: set[str] = set()
    collected: list[dict] = []
    for lang in _LANGS:
        raw, _ = fetch_raw_reviews(
            app_id,
            max_count=RECENT_PER_LANG,
            filter_type="recent",
            review_type="all",
            language=lang,
        )
        collected.extend(parse_and_dedup(raw, seen, f"{lang} recent", str(app_id)))
    return collected


async def crawl_steam_incremental() -> dict:
    """등록된 Steam 게임을 최신순으로 얕게 크롤해 백엔드 ingestion에 전송.

    한 게임 실패가 배치를 멈추지 않도록 게임 단위로 예외를 격리한다.
    """
    catalog = await _steam_catalog()
    if not catalog:
        logger.warning("steam catalog empty — nothing to crawl")
        return {"crawl_ok": 0, "crawl_fail": 0, "total": 0, "reviews_sent": 0}

    api_base = os.getenv("INTERNAL_API_BASE", "http://backend:8000").rstrip("/")
    api_url = f"{api_base}/api/v1/reviews/steam"
    api_key = os.getenv("API_SECRET_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else {}

    ok = fail = sent = 0
    async with httpx.AsyncClient(timeout=90) as client:
        for game_id, app_id, slug, meta in catalog:
            try:
                reviews = await asyncio.to_thread(_collect_recent, app_id)
                if not reviews:
                    logger.info("steam crawl: no reviews game=%s app=%s", game_id, app_id)
                    continue

                # 기존 meta 보존(커버·태그 유실 방지) + 필수 필드 보정
                meta = dict(meta)
                meta.setdefault("game_id", str(app_id))
                meta["crawled_at"] = datetime.now().isoformat()

                payload = {slug: {"meta": meta, "reviews": reviews}}
                resp = await client.post(api_url, json=payload, headers=headers)
                resp.raise_for_status()
                ok += 1
                sent += len(reviews)
                logger.info("steam crawl sent game=%s app=%s reviews=%d", game_id, app_id, len(reviews))
            except Exception as e:  # 한 게임 실패가 배치를 멈추지 않게
                fail += 1
                logger.warning("steam crawl failed game=%s app=%s: %s", game_id, app_id, e)

    result = {"crawl_ok": ok, "crawl_fail": fail, "total": len(catalog), "reviews_sent": sent}
    logger.info("steam incremental crawl done: %s", result)
    return result


async def run_loop() -> None:
    while True:
        wait = _seconds_until_next_run()
        logger.info("next steam crawl in %.0fs (%.1fh)", wait, wait / 3600)
        await asyncio.sleep(wait)
        try:
            await crawl_steam_incremental()
        except Exception:
            logger.exception("steam crawl crashed — retry next day")


def main() -> None:
    parser = argparse.ArgumentParser(description="Steam 증분 리뷰 크롤 잡")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="1회 크롤 후 종료")
    g.add_argument("--loop", action="store_true", help="매일 17:05 UTC 정렬 반복")
    args = parser.parse_args()
    asyncio.run(crawl_steam_incremental() if args.once else run_loop())


if __name__ == "__main__":
    main()
