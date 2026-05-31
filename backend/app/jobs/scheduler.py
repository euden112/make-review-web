"""
일일 잡 스케줄러 (기획서 3-5b·9-3)

매일 17:05 UTC(Steam 가격 경계 직후)에 세 잡을 **한 타임라인에 직렬화**한다.

  1. 가격·여론 리프레셔 (price_refresher.refresh_once)
  2. Steam 증분 리뷰 크롤 (review_crawler_job.crawl_steam_incremental)
  3. AI 요약 배치 (run_ai_pipeline_task — 게임별 증분)

순서 의미: 크롤이 신규 리뷰를 DB에 적재한 뒤 AI 배치가 그 신규분만 요약하도록
크롤을 AI 배치 **앞**에 둔다. 크롤 중복분은 ingestion upsert가 흡수한다.

직렬 실행 이유: AI 요약은 CPU 추론으로 수십 분 소요되므로 API 프로세스와
격리된 잡 컨테이너에서 순차 처리해야 응답 지연·중복 실행이 없다.
가격 잡 중복 = 레이트리밋 폭증, AI 잡 중복 = Groq 한도 소진이므로
스케줄러는 이 컨테이너 단일 인스턴스로만 기동한다.

실행:
  python -m app.jobs.scheduler --once    # 1회 (가격→크롤→AI) 후 종료
  python -m app.jobs.scheduler --loop    # 매일 17:05 UTC 정렬 반복
"""

import argparse
import asyncio
import logging

from app.jobs.ai_batch import run_ai_batch
from app.jobs.review_crawler_job import crawl_steam_incremental
from app.jobs.price_refresher import refresh_once
from app.jobs.price_refresher import _seconds_until_next_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scheduler")


async def run_daily() -> dict:
    """가격·여론 리프레셔 → Steam 증분 크롤 → AI 요약 배치 (A안: 실패 격리).

    세 잡은 각각 독립 try로 감싸 한 잡 실패가 다음 잡을 스킵시키지 않게 한다.
    크롤이 실패해도 기존 신규 리뷰가 있으면 AI 배치가 처리할 수 있고,
    가격 잡 실패도 크롤·AI를 막지 않는다.
    """
    result: dict = {}
    logger.info("daily job start: price/sentiment refresh")
    try:
        result["refresh"] = await refresh_once()
    except Exception:
        logger.exception("price/sentiment refresh failed — crawl/AI proceed")
        result["refresh"] = {"error": "refresh failed"}

    logger.info("daily job: steam incremental crawl")
    try:
        result["crawl"] = await crawl_steam_incremental()
    except Exception:
        logger.exception("steam crawl failed — AI batch proceeds")
        result["crawl"] = {"error": "crawl failed"}

    logger.info("daily job: ai summary batch")
    try:
        result["ai"] = await run_ai_batch()
    except Exception:
        logger.exception("ai summary batch failed")
        result["ai"] = {"error": "ai batch failed"}

    return result


async def run_loop() -> None:
    while True:
        wait = _seconds_until_next_run()
        logger.info("next daily job in %.0fs (%.1fh)", wait, wait / 3600)
        await asyncio.sleep(wait)
        try:
            await run_daily()
        except Exception:
            logger.exception("daily job crashed — retry next day")


def main() -> None:
    parser = argparse.ArgumentParser(description="일일 잡 스케줄러")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="1회 실행 후 종료")
    g.add_argument("--loop", action="store_true", help="매일 17:05 UTC 정렬 반복")
    args = parser.parse_args()
    asyncio.run(run_daily() if args.once else run_loop())


if __name__ == "__main__":
    main()
