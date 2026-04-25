import sys

import os

import asyncio

import logging

from collections import Counter

from datetime import datetime

from sqlalchemy.future import select

from sqlalchemy import and_, func



from pathlib import Path



from app.models.domain import (

    ExternalReview, GameSummaryCursor, ReviewSummaryJob,

    GameReviewSummary, Platform, ReviewType,

)

from app.core.redis_client import invalidate_summary_cache, get_redis_cache

from app.core.database import AsyncSessionLocal



if os.path.exists("/workspace/ai-pipeline"):

    AI_PIPELINE_PATH = "/workspace/ai-pipeline"

else:

    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

    AI_PIPELINE_PATH = os.path.join(PROJECT_ROOT, "ai-pipeline")



if AI_PIPELINE_PATH not in sys.path:

    sys.path.append(AI_PIPELINE_PATH)



from ai_module.map_reduce.pipeline import run_hybrid_summary_pipeline

from ai_module.map_reduce.reduce_api import FinalSummary



try:

    from ai_module.evaluation.gemini_reliability import compute_gemini_reliability

    _HAS_GEMINI_RELIABILITY = True

except ImportError:

    _HAS_GEMINI_RELIABILITY = False



try:

    from ai_module.evaluation.semantic_similarity import compute_semantic_similarity

    _HAS_SEMANTIC_SIMILARITY = True

except ImportError:

    _HAS_SEMANTIC_SIMILARITY = False





logger = logging.getLogger(__name__)

logger.setLevel(logging.INFO)





async def get_pipeline_tasks(game_id: int, db) -> list[tuple[str, str | None]]:

    """해당 게임에 수집된 언어 목록을 조회해 실행할 파이프라인 작업 목록을 반환합니다.



    반환 형식: [("unified", None), ("regional", "en"), ("regional", "ko"), ...]

    """

    distinct_langs = (await db.scalars(

        select(ExternalReview.language_code).distinct().where(

            ExternalReview.game_id == game_id,

            ExternalReview.is_deleted == False,

        )

    )).all()



    tasks: list[tuple[str, str | None]] = [("unified", None)]

    tasks += [("regional", lang) for lang in distinct_langs if lang]

    return tasks





async def run_ai_pipeline_task(game_id: int, mode: str, language_code: str | None = None):

    """AI 요약 파이프라인 실행.



    mode="unified"  : 전체 리뷰 대상, Reduce 출력 언어 "ko" 고정

    mode="regional" : language_code 기준으로 리뷰 필터링

    """

    db_lang_key = "unified" if mode == "unified" else language_code

    logger.info(

        "run_ai_pipeline_task started: game_id=%s mode=%s language_code=%s",

        game_id, mode, language_code,

    )



    job = None

    async with AsyncSessionLocal() as db:

        try:

            # 1. 커서 확인

            cursor = (await db.execute(

                select(GameSummaryCursor).where(

                    and_(

                        GameSummaryCursor.game_id == game_id,

                        GameSummaryCursor.language_code == db_lang_key,

                    )

                )

            )).scalar_one_or_none()

            last_review_id = cursor.last_summarized_review_id if cursor else 0

            logger.info("ai pipeline cursor loaded: game_id=%s mode=%s last_review_id=%s", game_id, mode, last_review_id)



            # 2. 새 리뷰(증분) 조회 — 모드별 필터

            incremental_filters = [

                ExternalReview.game_id == game_id,

                ExternalReview.id > last_review_id,

                ExternalReview.is_deleted == False,

            ]

            if mode == "regional":

                incremental_filters.append(ExternalReview.language_code == language_code)



            new_reviews = (await db.execute(

                select(ExternalReview).where(and_(*incremental_filters))

            )).scalars().all()



            logger.info("ai pipeline new reviews: game_id=%s mode=%s count=%s", game_id, mode, len(new_reviews))

            if not new_reviews:

                logger.info("ai pipeline skipped: no new reviews for game_id=%s mode=%s", game_id, mode)

                return



            # 3. 누적 리뷰 (집계·신뢰도 계산용)

            accumulated_filters = [

                ExternalReview.game_id == game_id,

                ExternalReview.is_deleted == False,

            ]

            if mode == "regional":

                accumulated_filters.append(ExternalReview.language_code == language_code)



            summary_reviews = (await db.execute(

                select(ExternalReview).where(and_(*accumulated_filters))

            )).scalars().all()



            if not summary_reviews:

                return



            # 4. 신뢰도 지표용: DB 전체 리뷰 수 / 커서 이후 신규 수

            total_reviews_in_db = await db.scalar(

                select(func.count(ExternalReview.id)).where(

                    ExternalReview.game_id == game_id,

                    ExternalReview.is_deleted == False,

                )

            )

            new_count_since_last = await db.scalar(

                select(func.count(ExternalReview.id)).where(

                    ExternalReview.game_id == game_id,

                    ExternalReview.id > last_review_id,

                    ExternalReview.is_deleted == False,

                )

            )



            batch_from_review_id = min(r.id for r in new_reviews)

            new_max_review_id = max(r.id for r in new_reviews)

            covered_from_review_id = min(r.id for r in summary_reviews)

            covered_to_review_id = max(r.id for r in summary_reviews)



            # 5. 플랫폼·리뷰타입 매핑 및 비율 계산

            platforms = (await db.execute(select(Platform))).scalars().all()

            steam_pid = next((p.id for p in platforms if p.code == "steam"), None)

            meta_pid = next((p.id for p in platforms if p.code == "metacritic"), None)

            review_types = (await db.execute(select(ReviewType))).scalars().all()

            critic_tid = next((rt.id for rt in review_types if rt.type_code == "critic"), None)

            user_tid = next((rt.id for rt in review_types if rt.type_code == "user"), None)



            steam_pos = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is True)

            steam_neg = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is False)

            meta_pos = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 >= 75)

            meta_mix = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and 50 <= r.normalized_score_100 < 75)

            meta_neg = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 < 50)



            logger.info(

                "ai pipeline ratios: game_id=%s mode=%s steam_pos=%s steam_neg=%s meta_pos=%s meta_mix=%s meta_neg=%s",

                game_id, mode, steam_pos, steam_neg, meta_pos, meta_mix, meta_neg,

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



            # 6. 카테고리 빈도 집계 (항목 06 앵커링 — pipeline 전달은 ai-pipeline 업데이트 후 적용)

            category_freq: Counter = Counter()

            for review in summary_reviews:

                for item in (review.review_categories_json or []):

                    if isinstance(item, dict) and "category" in item:

                        category_freq[item["category"]] += 1

            top_categories = category_freq.most_common(8)

            if top_categories:

                logger.info("ai pipeline category anchors: game_id=%s %s", game_id, top_categories)



            # 항목 06 — 점수 앵커 준비 (pipeline 전달은 ai-pipeline 업데이트 후 적용)

            score_anchors = {

                "steam_recommend_ratio": steam_recommend_ratio,

                "metacritic_critic_avg": metacritic_critic_avg,

                "metacritic_user_avg": metacritic_user_avg,

            }

            logger.info("ai pipeline score anchors: game_id=%s %s", game_id, score_anchors)



            # 7. Job 시작 기록

            job = ReviewSummaryJob(

                game_id=game_id,

                language_code=db_lang_key,

                status="started",

                input_review_count=len(summary_reviews),

                from_review_id=batch_from_review_id,

                to_review_id=new_max_review_id,

            )

            db.add(job)

            await db.flush()

            logger.info("ai pipeline job created: job_id=%s game_id=%s mode=%s", job.id, game_id, mode)



            # 8. 기존 요약본 확인

            existing_summary = (await db.execute(

                select(GameReviewSummary).where(

                    and_(

                        GameReviewSummary.game_id == game_id,

                        GameReviewSummary.language_code == db_lang_key,

                        GameReviewSummary.is_current == True,

                    )

                )

            )).scalar_one_or_none()

            prior_summary_text = existing_summary.summary_text if existing_summary else None



            # 9. 하이브리드 파이프라인 실행

            reduce_language = "ko" if mode == "unified" else language_code

            logger.info("ai pipeline summary generation started: game_id=%s mode=%s job_id=%s", game_id, mode, job.id)



            map_results, ai_result = await run_hybrid_summary_pipeline(

                game_id=game_id,

                language_code=reduce_language,

                all_reviews=new_reviews,

                steam_ratio=(steam_pos, steam_neg),

                metacritic_ratio=(meta_pos, meta_mix, meta_neg),

                cache=get_redis_cache(),

                ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),

                local_model_name=os.getenv("LOCAL_MAP_MODEL", "gemma3:4b"),

                reduce_api_key=os.getenv("GEMINI_API_KEY", ""),

                reduce_model_name="gemini-2.5-flash-lite",

                prior_summary_text=prior_summary_text,

            )



            logger.info(

                "ai pipeline generation finished: game_id=%s mode=%s job_id=%s error_code=%s",

                game_id, mode, job.id, ai_result.error_code,

            )

            for result in map_results:

                logger.info("  chunk %d: %d chars, cached=%s", result.chunk_no, len(result.summary), result.cached)



            # 10. Job 토큰/캐시 기록 (getattr: ai-pipeline에 필드 추가 후 자동 반영)

            job.chunk_count = len(map_results)

            job.map_cache_hit = sum(1 for r in map_results if r.cached)

            job.map_cache_miss = sum(1 for r in map_results if not r.cached)

            job.map_input_tokens = sum(getattr(r, "input_tokens", 0) for r in map_results)

            job.map_output_tokens = sum(getattr(r, "output_tokens", 0) for r in map_results)

            job.reduce_input_tokens = getattr(ai_result, "reduce_input_tokens", 0)

            job.reduce_output_tokens = getattr(ai_result, "reduce_output_tokens", 0)



            # 11. DB 버전 결정

            latest_summary_version = (await db.execute(

                select(func.coalesce(func.max(GameReviewSummary.summary_version), 0)).where(

                    and_(

                        GameReviewSummary.game_id == game_id,

                        GameReviewSummary.language_code == db_lang_key,

                    )

                )

            )).scalar_one()

            cursor_version = cursor.last_summary_version if cursor else 0

            new_version = max(cursor_version, latest_summary_version) + 1

            logger.info(

                "ai pipeline version resolved: game_id=%s mode=%s next_version=%s",

                game_id, mode, new_version,

            )



            if existing_summary:

                await db.delete(existing_summary)

                await db.flush()

                logger.info(

                    "ai pipeline previous summary deleted: game_id=%s mode=%s previous_id=%s",

                    game_id, mode, existing_summary.id,

                )



            # 12. 요약 신뢰도 지표 계산

            coverage_ratio = source_review_count / total_reviews_in_db if total_reviews_in_db else None

            staleness_ratio = new_count_since_last / total_reviews_in_db if total_reviews_in_db else None

            sentiment_alignment = (

                1 - abs(float(ai_result.sentiment_score) - steam_recommend_ratio) / 100

                if ai_result.sentiment_score is not None and steam_recommend_ratio is not None

                else None

            )



            new_summary = GameReviewSummary(

                game_id=game_id,

                language_code=db_lang_key,

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

                sentiment_alignment=sentiment_alignment,

                coverage_ratio=coverage_ratio,

                staleness_ratio=staleness_ratio,

                is_current=True,

            )

            db.add(new_summary)

            logger.info("ai pipeline summary row prepared: game_id=%s mode=%s job_id=%s version=%s", game_id, mode, job.id, new_version)



            # 13. Gemini 출력 신뢰도 계산 및 저장 (항목 07)

            if _HAS_GEMINI_RELIABILITY:

                reliability = compute_gemini_reliability(

                    ai_result=ai_result,

                    input_reviews=new_reviews,

                    steam_recommend_ratio=steam_recommend_ratio,

                )

                job.schema_compliance = reliability.schema_compliance

                job.hallucination_score = reliability.hallucination_score

                job.sentiment_consistency = reliability.sentiment_consistency

                job.anchor_deviation = reliability.anchor_deviation

            else:

                logger.debug("ai_module.evaluation.gemini_reliability not available; skipping reliability metrics")



            # 14. 임베딩 유사도 계산 및 저장 (항목 04)

            # sentence-transformer는 동기 CPU 연산 → run_in_executor로 이벤트 루프 블로킹 방지

            if _HAS_SEMANTIC_SIMILARITY:

                selected_texts = [r.review_text_clean for r in summary_reviews[:50] if r.review_text_clean]

                loop = asyncio.get_event_loop()

                similarity = await loop.run_in_executor(

                    None,

                    compute_semantic_similarity,

                    selected_texts,

                    ai_result.full_text,

                )

                new_summary.semantic_similarity_score = similarity

            else:

                logger.debug("ai_module.evaluation.semantic_similarity not available; skipping similarity score")



            # 16. 커서 최신화

            if cursor:

                cursor.last_summarized_review_id = new_max_review_id

                cursor.last_summary_version = new_version

                cursor.updated_at = datetime.utcnow()

            else:

                db.add(GameSummaryCursor(

                    game_id=game_id,

                    language_code=db_lang_key,

                    last_summarized_review_id=new_max_review_id,

                    last_summary_version=new_version,

                ))



            job.status = "success"

            job.ended_at = datetime.utcnow()

            await db.commit()

            logger.info(

                "ai pipeline finished: game_id=%s mode=%s language=%s job_id=%s",

                game_id, mode, db_lang_key, job.id,

            )



            # 17. Redis 캐시 무효화

            await invalidate_summary_cache(game_id, db_lang_key)



        except Exception as e:

            await db.rollback()

            if job:

                job.status = "failed"

                job.error_message = str(e)

                try:

                    await db.commit()

                except Exception:

                    pass

            logger.exception(

                "ai pipeline failed: game_id=%s mode=%s language=%s error=%s",

                game_id, mode, db_lang_key, e,

            )