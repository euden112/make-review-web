import sys
import os
from pathlib import Path

if os.path.exists("/workspace/ai-pipeline"):
    AI_PIPELINE_PATH = "/workspace/ai-pipeline"
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
    AI_PIPELINE_PATH = os.path.join(PROJECT_ROOT, "ai-pipeline")

if AI_PIPELINE_PATH not in sys.path:
    sys.path.append(AI_PIPELINE_PATH)

import asyncio
import logging
from collections import Counter
from datetime import datetime

from sqlalchemy.future import select
from sqlalchemy import and_, func

from app.models.domain import (
    ExternalReview, GameSummaryCursor, ReviewSummaryJob,
    GameReviewSummary, Platform, ReviewType,
    PlaytimeAnalysis, CriticSummary, UserSummary,
)
from app.core.redis_client import (
    invalidate_summary_cache, invalidate_playtime_cache, invalidate_critic_cache,
    invalidate_user_summary_cache, get_redis_cache,
)
from ai_module.cache.redis_cache import RedisCache
from app.core.database import AsyncSessionLocal

from ai_module.map_reduce.pipeline import run_hybrid_summary_pipeline
from ai_module.map_reduce.reduce_api import FinalSummary

try:
    from ai_module.evaluation.reduce_reliability import compute_reduce_reliability
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


def _select_platform_representative_reviews(
    reviews,
    steam_pid,
    meta_pid,
    limit_per_platform: int = 3,
) -> list[dict[str, object]]:
    def _platform_score(review) -> tuple[float, float, float, int]:
        helpful_count = float(getattr(review, "helpful_count", 0) or 0)
        playtime_hours = float(getattr(review, "playtime_hours", 0) or 0)
        normalized_score = float(getattr(review, "normalized_score_100", 0) or 0)
        if getattr(review, "platform_id", None) == steam_pid:
            # helpful_count 주 기준, playtime은 보조(최대 200h 상한 적용)
            # 타이브레이커에서도 uncapped playtime 제거 — helpful_count가 동일하면 id로만 정렬
            playtime_capped = min(playtime_hours, 200.0)
            score = (1.5 * (helpful_count + 1.0) ** 0.5) + (0.3 * (playtime_capped + 1.0) ** 0.5)
            return (score, helpful_count, -int(getattr(review, "id", 0) or 0), 0)
        if getattr(review, "platform_id", None) == meta_pid:
            score = normalized_score + (0.1 * (helpful_count + 1.0) ** 0.5)
            return (score, normalized_score, helpful_count, -int(getattr(review, "id", 0) or 0))
        return (0.0, helpful_count, playtime_hours, -int(getattr(review, "id", 0) or 0))

    steam_candidates = sorted(
        [review for review in reviews if getattr(review, "platform_id", None) == steam_pid],
        key=_platform_score,
        reverse=True,
    )[:limit_per_platform]
    meta_candidates = sorted(
        [review for review in reviews if getattr(review, "platform_id", None) == meta_pid],
        key=_platform_score,
        reverse=True,
    )[:limit_per_platform]

    selected: list[dict[str, object]] = []

    for review in steam_candidates:
        selected.append({
            "source": "steam",
            "review_id": getattr(review, "id", None),
        })

    for review in meta_candidates:
        selected.append({
            "source": "metacritic",
            "review_id": getattr(review, "id", None),
        })

    return selected


async def get_pipeline_tasks(game_id: int, db) -> list[tuple[str, str | None]]:
    """unified 1회만 실행."""
    return [("unified", None)]


async def _upsert_playtime_analysis(db, game_id: int, ai_result: FinalSummary, buckets) -> None:
    """playtime_analyses 테이블에 upsert."""
    if buckets is None:
        return

    bucket_thresholds = {"early_max": buckets.early_max, "mid_max": buckets.mid_max}

    existing = (await db.execute(
        select(PlaytimeAnalysis).where(PlaytimeAnalysis.game_id == game_id)
    )).scalar_one_or_none()

    def bucket_fields(b, prefix: str) -> dict:
        if b is None:
            return {
                f"{prefix}_summary": None, f"{prefix}_sentiment": None,
                f"{prefix}_score": None, f"{prefix}_pros": None,
                f"{prefix}_cons": None, f"{prefix}_keywords": None,
            }
        return {
            f"{prefix}_summary": b.summary,
            f"{prefix}_sentiment": b.sentiment_overall,
            f"{prefix}_score": b.sentiment_score,
            f"{prefix}_pros": b.pros,
            f"{prefix}_cons": b.cons,
            f"{prefix}_keywords": b.keywords,
        }

    fields = {
        "game_id": game_id,
        "bucket_thresholds": bucket_thresholds,
        **bucket_fields(ai_result.playtime_early, "early"),
        **bucket_fields(ai_result.playtime_mid, "mid"),
        **bucket_fields(ai_result.playtime_late, "late"),
        "updated_at": datetime.utcnow(),
    }

    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        db.add(PlaytimeAnalysis(**fields))


async def _upsert_critic_summary(db, game_id: int, ai_result: FinalSummary) -> None:
    """critic_summaries 테이블에 upsert."""
    if ai_result.critic is None:
        return

    existing = (await db.execute(
        select(CriticSummary).where(CriticSummary.game_id == game_id)
    )).scalar_one_or_none()

    fields = {
        "game_id": game_id,
        "summary": ai_result.critic.summary,
        "sentiment": ai_result.critic.sentiment_overall,
        "score": ai_result.critic.sentiment_score,
        "pros": ai_result.critic.pros,
        "cons": ai_result.critic.cons,
        "keywords": ai_result.critic.keywords,
        "updated_at": datetime.utcnow(),
    }

    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        db.add(CriticSummary(**fields))


async def _upsert_user_summary(db, game_id: int, ai_result: FinalSummary) -> None:
    """user_summaries 테이블에 upsert (B안)."""
    if ai_result.user is None:
        return

    existing = (await db.execute(
        select(UserSummary).where(UserSummary.game_id == game_id)
    )).scalar_one_or_none()

    fields = {
        "game_id": game_id,
        "summary": ai_result.user.summary,
        "sentiment": ai_result.user.sentiment_overall,
        "score": ai_result.user.sentiment_score,
        "pros": ai_result.user.pros,
        "cons": ai_result.user.cons,
        "keywords": ai_result.user.keywords,
        "updated_at": datetime.utcnow(),
    }

    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        db.add(UserSummary(**fields))


async def run_ai_pipeline_task(game_id: int, mode: str, language_code: str | None = None, force: bool = False):
    """AI 요약 파이프라인 실행 (unified 전용)."""
    cursor_language_code = "unified"
    review_language = None

    logger.info(
        "run_ai_pipeline_task started: game_id=%s mode=%s",
        game_id, mode,
    )

    job = None
    async with AsyncSessionLocal() as db:
        try:
            # 1. 커서 확인
            cursor = (await db.execute(
                select(GameSummaryCursor).where(
                    and_(
                        GameSummaryCursor.game_id == game_id,
                        GameSummaryCursor.summary_type == mode,
                        GameSummaryCursor.language_code == cursor_language_code,
                    )
                )
            )).scalar_one_or_none()

            last_review_id = cursor.last_summarized_review_id if cursor else 0
            if force and last_review_id:
                logger.info("ai pipeline force mode: resetting cursor game_id=%s", game_id)
                last_review_id = 0

            # 2. 새 리뷰(증분) 조회
            incremental_filters = [
                ExternalReview.game_id == game_id,
                ExternalReview.id > last_review_id,
                ExternalReview.is_deleted == False,
            ]

            new_reviews = (await db.execute(
                select(ExternalReview).where(and_(*incremental_filters))
            )).scalars().all()

            logger.info("ai pipeline new reviews: game_id=%s count=%s", game_id, len(new_reviews))

            if not new_reviews:
                has_current_summary = (await db.execute(
                    select(GameReviewSummary.id).where(
                        and_(
                            GameReviewSummary.game_id == game_id,
                            GameReviewSummary.summary_type == mode,
                            GameReviewSummary.review_language.is_(None),
                            GameReviewSummary.is_current == True,
                        )
                    )
                )).scalar_one_or_none()

                if has_current_summary:
                    logger.info("ai pipeline skipped: no new reviews for game_id=%s", game_id)
                    return

                logger.info("ai pipeline auto-recovery: reprocessing all reviews game_id=%s", game_id)
                last_review_id = 0
                new_reviews = (await db.execute(
                    select(ExternalReview).where(and_(
                        ExternalReview.game_id == game_id,
                        ExternalReview.id > 0,
                        ExternalReview.is_deleted == False,
                    ))
                )).scalars().all()

                if not new_reviews:
                    logger.info("ai pipeline skipped: truly no reviews for game_id=%s", game_id)
                    return

            # 3. 누적 리뷰 (집계용)
            summary_reviews = (await db.execute(
                select(ExternalReview).where(
                    and_(
                        ExternalReview.game_id == game_id,
                        ExternalReview.is_deleted == False,
                    )
                )
            )).scalars().all()

            if not summary_reviews:
                return

            # 4. 신뢰도 지표용 — 이미 로드된 리스트에서 계산 (별도 COUNT 쿼리 불필요)
            total_reviews_in_db = len(summary_reviews)
            new_count_since_last = len(new_reviews)

            batch_from_review_id   = min(r.id for r in new_reviews)
            new_max_review_id      = max(r.id for r in new_reviews)
            covered_from_review_id = min(r.id for r in summary_reviews)
            covered_to_review_id   = max(r.id for r in summary_reviews)

            # 5. 플랫폼·리뷰타입 매핑 및 비율 계산
            platforms    = (await db.execute(select(Platform))).scalars().all()
            steam_pid    = next((p.id for p in platforms if p.code == "steam"), None)
            meta_pid     = next((p.id for p in platforms if p.code == "metacritic"), None)
            review_types = (await db.execute(select(ReviewType))).scalars().all()
            critic_tid   = next((rt.id for rt in review_types if rt.type_code == "critic"), None)
            user_tid     = next((rt.id for rt in review_types if rt.type_code == "user"), None)

            steam_pos = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is True)
            steam_neg = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is False)
            meta_pos  = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 >= 75)
            meta_mix  = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and 50 <= r.normalized_score_100 < 75)
            meta_neg  = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 < 50)

            steam_total           = steam_pos + steam_neg
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
            metacritic_user_avg   = round(sum(meta_user_scores) / len(meta_user_scores), 2) if meta_user_scores else None
            source_review_count   = len(summary_reviews)

            # 6. 카테고리별 긍/부정 비율 집계
            category_total:    Counter = Counter()
            category_positive: Counter = Counter()

            for review in summary_reviews:
                for item in (review.review_categories_json or []):
                    if isinstance(item, dict):
                        category  = item.get("category")
                        sentiment = item.get("sentiment")
                    elif isinstance(item, str):
                        category, sentiment = item, None
                    else:
                        category = None

                    if category:
                        category_total[str(category)] += 1
                        if sentiment == "positive":
                            category_positive[str(category)] += 1

            top_categories = [
                (cat, total, round(category_positive[cat] / total, 3))
                for cat, total in category_total.most_common(8)
            ]

            score_anchors = {
                "steam_recommend_ratio": steam_recommend_ratio,
                "metacritic_critic_avg": metacritic_critic_avg,
                "metacritic_user_avg": metacritic_user_avg,
            }

            # 7. Job 시작 기록
            job = ReviewSummaryJob(
                game_id=game_id,
                status="started",
                input_review_count=len(summary_reviews),
                from_review_id=batch_from_review_id,
                to_review_id=new_max_review_id,
            )
            db.add(job)
            await db.flush()

            # 8. 기존 요약본 확인
            existing_summary = (await db.execute(
                select(GameReviewSummary).where(
                    and_(
                        GameReviewSummary.game_id == game_id,
                        GameReviewSummary.summary_type == mode,
                        GameReviewSummary.review_language.is_(None),
                        GameReviewSummary.is_current == True,
                    )
                )
            )).scalar_one_or_none()

            prior_summary_text = existing_summary.summary_text if existing_summary else None

            # 9. 파이프라인 실행 (Sprint 4: 단일 unified 실행)
            map_results, ai_result, playtime_buckets = await run_hybrid_summary_pipeline(
                game_id=game_id,
                language_code="ko",
                all_reviews=new_reviews,
                steam_ratio=(steam_pos, steam_neg),
                metacritic_ratio=(meta_pos, meta_mix, meta_neg),
                cache=RedisCache(get_redis_cache()),
                ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                local_model_name=os.getenv("LOCAL_MAP_MODEL", "qwen2.5:1.5b"),
                reduce_api_key=os.getenv("GROQ_API_KEY", ""),
                reduce_model_name=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
                prior_summary_text=prior_summary_text,
                score_anchors=score_anchors,
                category_frequency=top_categories,
            )

            # 10. Job 토큰/캐시 기록
            job.chunk_count         = len(map_results)
            job.map_cache_hit       = sum(1 for r in map_results if r.cached)
            job.map_cache_miss      = sum(1 for r in map_results if not r.cached)
            job.map_input_tokens    = sum(getattr(r, "input_tokens", 0) for r in map_results)
            job.map_output_tokens   = sum(getattr(r, "output_tokens", 0) for r in map_results)
            job.reduce_input_tokens  = getattr(ai_result, "input_tokens", 0)
            job.reduce_output_tokens = getattr(ai_result, "output_tokens", 0)
            # Chunk별 실패 통계 — run_map_stage가 첫 번째 결과에 부착
            if map_results and hasattr(map_results[0], "failure_stats"):
                job.failure_reasons_json = map_results[0].failure_stats

            # 11. DB 버전 결정
            latest_summary_version = (await db.execute(
                select(func.coalesce(func.max(GameReviewSummary.summary_version), 0)).where(
                    and_(
                        GameReviewSummary.game_id == game_id,
                        GameReviewSummary.summary_type == mode,
                        GameReviewSummary.review_language.is_(None),
                    )
                )
            )).scalar_one()

            cursor_version = cursor.last_summary_version if cursor else 0
            new_version = max(cursor_version, latest_summary_version) + 1

            if existing_summary:
                await db.delete(existing_summary)
                await db.flush()

            # 12. 신뢰도 지표 계산
            coverage_ratio     = source_review_count / total_reviews_in_db if total_reviews_in_db else None
            staleness_ratio    = new_count_since_last / total_reviews_in_db if total_reviews_in_db else None
            sentiment_alignment = (
                1 - abs(float(ai_result.sentiment_score) - steam_recommend_ratio) / 100
                if ai_result.sentiment_score is not None and steam_recommend_ratio is not None
                else None
            )

            # B안: unified 본문 폐지 — summary_text는 None으로 저장.
            # 본문은 user_summaries.summary / critic_summaries.summary로 분리.
            representative_reviews = _select_platform_representative_reviews(
                summary_reviews,
                steam_pid,
                meta_pid,
                limit_per_platform=3,
            )

            new_summary = GameReviewSummary(
                game_id=game_id,
                summary_type=mode,
                review_language=review_language,
                job_id=job.id,
                summary_version=new_version,
                summary_text=None,
                one_liner=ai_result.one_liner,
                sentiment_overall=ai_result.sentiment_overall,
                sentiment_score=ai_result.sentiment_score,
                aspect_sentiment_json=ai_result.aspect_scores,
                representative_reviews_json=representative_reviews,
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

            # 13. Sprint 4: playtime_analyses / critic_summaries 저장
            await _upsert_playtime_analysis(db, game_id, ai_result, playtime_buckets)
            await _upsert_critic_summary(db, game_id, ai_result)
            await _upsert_user_summary(db, game_id, ai_result)

            # 14. 신뢰도 평가
            if _HAS_GEMINI_RELIABILITY:
                reliability = compute_reduce_reliability(
                    ai_result=ai_result,
                    input_reviews=new_reviews,
                    steam_recommend_ratio=steam_recommend_ratio,
                )
                job.schema_compliance    = reliability.schema_compliance
                job.hallucination_score  = reliability.hallucination_score
                job.sentiment_consistency = reliability.sentiment_consistency
                job.anchor_deviation     = reliability.anchor_deviation

            # 15. 임베딩 유사도
            if _HAS_SEMANTIC_SIMILARITY:
                selected_texts = [r.review_text_clean for r in summary_reviews[:50] if r.review_text_clean]
                synthesized_summary = "\n".join(
                    part for part in [
                        ai_result.one_liner,
                        "\n".join(ai_result.pros or []),
                        "\n".join(ai_result.cons or []),
                        ai_result.user.summary if ai_result.user else "",
                        ai_result.critic.summary if ai_result.critic else "",
                    ]
                    if part
                )
                loop = asyncio.get_running_loop()
                similarity = await loop.run_in_executor(
                    None, compute_semantic_similarity, selected_texts, synthesized_summary,
                )
                new_summary.semantic_similarity_score = similarity

            # 16. 커서 최신화
            if cursor:
                cursor.last_summarized_review_id = new_max_review_id
                cursor.last_summary_version      = new_version
                cursor.updated_at                = datetime.utcnow()
            else:
                db.add(GameSummaryCursor(
                    game_id=game_id,
                    language_code=cursor_language_code,
                    summary_type=mode,
                    last_summarized_review_id=new_max_review_id,
                    last_summary_version=new_version,
                ))

            job.status   = "success"
            job.ended_at = datetime.utcnow()
            await db.commit()

            logger.info("ai pipeline finished: game_id=%s job_id=%s", game_id, job.id)

            await invalidate_summary_cache(game_id, cursor_language_code)
            await invalidate_playtime_cache(game_id)
            await invalidate_critic_cache(game_id)
            await invalidate_user_summary_cache(game_id)

        except Exception as e:
            await db.rollback()
            if job:
                job.status        = "failed"
                job.error_message = str(e)
                try:
                    await db.commit()
                except Exception:
                    pass
            logger.exception("ai pipeline failed: game_id=%s error=%s", game_id, e)
