import sys
import os
import logging
from datetime import datetime
from sqlalchemy.future import select
from sqlalchemy import and_, func

from pathlib import Path

from app.models.domain import ExternalReview, GameSummaryCursor, ReviewSummaryJob, GameReviewSummary, Platform, ReviewType
from app.core.redis_client import invalidate_summary_cache
from app.core.database import AsyncSessionLocal

# ai-pipeline 루트를 import path에 추가해 ai_module 패키지를 직접 임포트합니다.
if os.path.exists("/workspace/ai-pipeline"):
    AI_PIPELINE_PATH = "/workspace/ai-pipeline"
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
    AI_PIPELINE_PATH = os.path.join(PROJECT_ROOT, "ai-pipeline")

if AI_PIPELINE_PATH not in sys.path:
    sys.path.append(AI_PIPELINE_PATH)

from ai_module.map_reduce.pipeline import run_hybrid_summary_pipeline
from ai_module.map_reduce.reduce_api import FinalSummary


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

async def run_ai_pipeline_task(game_id: int, language_code: str):
    logger.info("run_ai_pipeline_task started: game_id=%s language_code=%s", game_id, language_code)
    job = None
    async with AsyncSessionLocal() as db:
        try:
            logger.info("ai pipeline db session opened: game_id=%s language_code=%s", game_id, language_code)
            # 1. 커서 확인
            cursor = (await db.execute(select(GameSummaryCursor).where(
                and_(GameSummaryCursor.game_id == game_id, GameSummaryCursor.language_code == language_code)
            ))).scalar_one_or_none()
            last_review_id = cursor.last_summarized_review_id if cursor else 0
            logger.info("ai pipeline cursor loaded: game_id=%s language_code=%s last_review_id=%s", game_id, language_code, last_review_id)

            # 2. 새 리뷰(증분) 확인: 새 리뷰가 없으면 파이프라인 실행을 건너뜁니다.
            new_reviews_query = select(ExternalReview).where(
                and_(
                    ExternalReview.game_id == game_id,
                    ExternalReview.language_code == language_code,
                    ExternalReview.id > last_review_id,
                    ExternalReview.is_deleted == False,
                )
            )
            new_reviews = (await db.execute(new_reviews_query)).scalars().all()
            logger.info("ai pipeline new reviews loaded: game_id=%s language_code=%s count=%s", game_id, language_code, len(new_reviews))
            if not new_reviews:
                logger.info("ai pipeline skipped: no new reviews for game_id=%s language_code=%s", game_id, language_code)
                return

            batch_from_review_id = min(r.id for r in new_reviews)
            new_max_review_id = max(r.id for r in new_reviews)

            # 3. 누적 지표 계산용 전체 리뷰 확보: 같은 game_id/language_code의 모든 활성 리뷰를 대상으로 집계합니다.
            summary_reviews_query = select(ExternalReview).where(
                and_(
                    ExternalReview.game_id == game_id,
                    ExternalReview.language_code == language_code,
                    ExternalReview.is_deleted == False,
                )
            )
            summary_reviews = (await db.execute(summary_reviews_query)).scalars().all()
            logger.info(
                "ai pipeline summary reviews loaded: game_id=%s language_code=%s total_count=%s",
                game_id,
                language_code,
                len(summary_reviews),
            )
            if not summary_reviews:
                logger.info("ai pipeline skipped: no summary reviews for game_id=%s language_code=%s", game_id, language_code)
                return

            covered_from_review_id = min(r.id for r in summary_reviews)
            covered_to_review_id = max(r.id for r in summary_reviews)

            # 4. 플랫폼 매핑 및 층화 추출용 비율 계산 로직
            platforms = (await db.execute(select(Platform))).scalars().all()
            steam_pid = next((p.id for p in platforms if p.code == 'steam'), None)
            meta_pid = next((p.id for p in platforms if p.code == 'metacritic'), None)
            review_types = (await db.execute(select(ReviewType))).scalars().all()
            critic_tid = next((rt.id for rt in review_types if rt.type_code == 'critic'), None)
            user_tid = next((rt.id for rt in review_types if rt.type_code == 'user'), None)

            steam_pos = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is True)
            steam_neg = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is False)

            meta_pos = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 >= 75)
            meta_mix = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and 50 <= r.normalized_score_100 < 75)
            meta_neg = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 < 50)

            logger.info(
                "ai pipeline ratios prepared: game_id=%s language_code=%s steam_pos=%s steam_neg=%s meta_pos=%s meta_mix=%s meta_neg=%s",
                game_id,
                language_code,
                steam_pos,
                steam_neg,
                meta_pos,
                meta_mix,
                meta_neg,
            )

            steam_total = steam_pos + steam_neg
            steam_recommend_ratio = round((steam_pos / steam_total) * 100, 2) if steam_total > 0 else None

            meta_critic_scores = [
                float(r.normalized_score_100)
                for r in summary_reviews
                if r.platform_id == meta_pid and r.review_type_id == critic_tid and r.normalized_score_100 is not None
            ]
            meta_user_scores = [
                float(r.normalized_score_100)
                for r in summary_reviews
                if r.platform_id == meta_pid and r.review_type_id == user_tid and r.normalized_score_100 is not None
            ]

            metacritic_critic_avg = round(sum(meta_critic_scores) / len(meta_critic_scores), 2) if meta_critic_scores else None
            metacritic_user_avg = round(sum(meta_user_scores) / len(meta_user_scores), 2) if meta_user_scores else None
            source_review_count = len(summary_reviews)

            # 5. Job 시작 기록
            logger.info("ai pipeline job creation started: game_id=%s language_code=%s", game_id, language_code)
            job = ReviewSummaryJob(
                game_id=game_id,
                language_code=language_code,
                status='started',
                input_review_count=len(summary_reviews),
                from_review_id=batch_from_review_id,
                to_review_id=new_max_review_id,
            )
            db.add(job)
            await db.flush()
            logger.info("ai pipeline job created: job_id=%s game_id=%s language_code=%s", job.id, game_id, language_code)

            # 6. 기존 요약본 확인
            existing_summary = (await db.execute(select(GameReviewSummary).where(
                and_(GameReviewSummary.game_id == game_id, GameReviewSummary.language_code == language_code, GameReviewSummary.is_current == True)
            ))).scalar_one_or_none()
            prior_summary_text = existing_summary.summary_text if existing_summary else None

            # 7. 하이브리드 파이프라인 실행
            logger.info("ai pipeline summary generation started: game_id=%s language_code=%s job_id=%s", game_id, language_code, job.id)
            map_results, ai_result = await run_hybrid_summary_pipeline(
                game_id=game_id,
                language_code=language_code,
                all_reviews=new_reviews,
                steam_ratio=(steam_pos, steam_neg),
                metacritic_ratio=(meta_pos, meta_mix, meta_neg),
                cache=None,
                ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                local_model_name=os.getenv("LOCAL_MAP_MODEL", "gemma4"),
                reduce_api_key=os.getenv("GEMINI_API_KEY", ""),
                reduce_model_name="gemini-2.5-flash-lite",
                prior_summary_text=prior_summary_text,
            )
            
            # Log map stage results
            logger.info("map stage completed: game_id=%s language_code=%s total_chunks=%s", game_id, language_code, len(map_results))
            for result in map_results:
                logger.info("  chunk %d: %d chars, cached=%s", result.chunk_no, len(result.summary), result.cached)

            logger.info(
                "ai pipeline summary generation finished: game_id=%s language_code=%s job_id=%s error_code=%s retryable=%s",
                game_id,
                language_code,
                job.id,
                ai_result.error_code,
                ai_result.is_retryable,
            )

            # 8. DB 결과 저장

            latest_summary_version = (await db.execute(
                select(func.coalesce(func.max(GameReviewSummary.summary_version), 0)).where(
                    and_(
                        GameReviewSummary.game_id == game_id,
                        GameReviewSummary.language_code == language_code,
                    )
                )
            )).scalar_one()
            cursor_version = cursor.last_summary_version if cursor else 0
            new_version = max(cursor_version, latest_summary_version) + 1
            logger.info(
                "ai pipeline summary version resolved: game_id=%s language_code=%s cursor_version=%s latest_version=%s next_version=%s",
                game_id,
                language_code,
                cursor_version,
                latest_summary_version,
                new_version,
            )

            if existing_summary:
                await db.delete(existing_summary)
                await db.flush()
                logger.info(
                    "ai pipeline previous current summary deleted: game_id=%s language_code=%s previous_summary_id=%s",
                    game_id,
                    language_code,
                    existing_summary.id,
                )

            new_summary = GameReviewSummary(
                game_id=game_id,
                language_code=language_code,
                job_id=job.id,
                summary_version=new_version,
                summary_text=f"**{ai_result.one_liner}**\n\n{ai_result.full_text}",
                sentiment_overall=ai_result.sentiment_overall,
                sentiment_score=ai_result.sentiment_score,
                aspect_sentiment_json=ai_result.aspect_scores,
                representative_reviews_json=ai_result.representative_reviews,
                pros_json=ai_result.pros,
                cons_json=ai_result.cons,
                keywords_json=ai_result.keywords,
                steam_recommend_ratio=steam_recommend_ratio,
                metacritic_critic_avg=metacritic_critic_avg,
                metacritic_user_avg=metacritic_user_avg,
                source_review_count=source_review_count,
                covered_from_review_id=covered_from_review_id,
                covered_to_review_id=covered_to_review_id,
                is_current=True
            )
            db.add(new_summary)
            logger.info("ai pipeline summary row prepared: game_id=%s language_code=%s job_id=%s version=%s", game_id, language_code, job.id, new_version)

            # 9. 커서 최신화
            if cursor:
                cursor.last_summarized_review_id = new_max_review_id
                cursor.last_summary_version = new_version
                cursor.updated_at = datetime.utcnow()
            else:
                new_cursor = GameSummaryCursor(
                    game_id=game_id,
                    language_code=language_code,
                    last_summarized_review_id=new_max_review_id,
                    last_summary_version=new_version
                )
                db.add(new_cursor)

            job.status = 'success'
            job.ended_at = datetime.utcnow()
            await db.commit()
            logger.info("ai pipeline db commit complete: game_id=%s language_code=%s job_id=%s", game_id, language_code, job.id)

            # 10. Redis 캐시 무효화
            await invalidate_summary_cache(game_id, language_code)
            logger.info("ai pipeline finished successfully: game_id=%s language_code=%s job_id=%s", game_id, language_code, job.id)

        except Exception as e:
            await db.rollback()
            if job:
                job.status = 'failed'
                job.error_message = str(e)
                await db.commit()
            logger.exception("ai pipeline failed: game_id=%s language_code=%s error=%s", game_id, language_code, e)