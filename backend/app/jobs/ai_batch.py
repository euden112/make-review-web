"""
AI 요약 증분 배치 잡 (기획서 9-3 A안)

스케줄러에서 추출한 독립 모듈. 가격·여론 리프레셔와 **데이터 의존이 없어**
(AI는 크롤된 리뷰만 의존, Redis 가격 스냅샷 미참조) 별도로 실행 가능하다.
독립화 목적: 가격 잡 실패가 AI 배치를 통째로 스킵시키던 실패 전파 결함 제거
및 독립 재실행·관측 분리.

전체 게임을 순회하며 게임·태스크를 **순차 실행**한다. AI 요약은 CPU 추론으로
수십 분 소요되므로 동시 실행 시 CPU/Groq 한도를 침해한다.

실행:
  python -m app.jobs.ai_batch --once    # 1회 배치 후 종료
  python -m app.jobs.ai_batch --loop    # 매일 17:05 UTC 정렬 반복
"""

import argparse
import asyncio
import logging

from sqlalchemy.future import select

from app.core.database import AsyncSessionLocal
from app.models.domain import Game
from app.services.ai_service import get_pipeline_tasks, run_ai_pipeline_task
from app.jobs.price_refresher import _seconds_until_next_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai_batch")


async def run_ai_batch() -> dict:
    """전체 게임 AI 요약 증분 배치. 게임·태스크 순차 실행 (CPU/Groq 보호).

    한 게임 실패가 배치를 멈추지 않도록 게임 단위로 예외를 격리한다.
    """
    async with AsyncSessionLocal() as db:
        game_ids = (await db.execute(select(Game.id))).scalars().all()

    ok = fail = 0
    for game_id in game_ids:
        try:
            async with AsyncSessionLocal() as db:
                tasks = await get_pipeline_tasks(game_id, db)
            for mode, lang in tasks:
                await run_ai_pipeline_task(game_id, mode, lang, force=False)
            ok += 1
        except Exception as e:  # 한 게임 실패가 배치를 멈추지 않게
            fail += 1
            logger.warning("ai pipeline failed game=%s: %s", game_id, e)
    result = {"ai_ok": ok, "ai_fail": fail, "total": len(game_ids)}
    logger.info("ai summary batch done: %s", result)
    return result


async def run_loop() -> None:
    while True:
        wait = _seconds_until_next_run()
        logger.info("next ai batch in %.0fs (%.1fh)", wait, wait / 3600)
        await asyncio.sleep(wait)
        try:
            await run_ai_batch()
        except Exception:
            logger.exception("ai batch crashed — retry next day")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 요약 증분 배치 잡")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="1회 배치 후 종료")
    g.add_argument("--loop", action="store_true", help="매일 17:05 UTC 정렬 반복")
    args = parser.parse_args()
    asyncio.run(run_ai_batch() if args.once else run_loop())


if __name__ == "__main__":
    main()
