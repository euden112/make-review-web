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

    cursor_language_code = "unified" if mode == "unified" else language_code
    review_language = None if mode == "unified" else language_code

    logger.info(

        "run_ai_pipeline_task started: game_id=%s mode=%s language_code=%s",

        game_id, mode, language_code,

    )



    job = None

    async with AsyncSessionLocal() as db:

        try:

            # 1. 커서 확인
            # Sprint 3: Cursor 조회에 summary_type 필터 추가
            # 이유: 동일 game_id에서 unified vs regional 커서 혼동 방지
            #   - m001 마이그레이션으로 summary_type 컬럼 추가
            #   - (game_id, language_code) PK에 summary_type 메타 필드 추가
            #   - 예: (game_id=100, language_code="unified") unified 요약용
            #         (game_id=100, language_code="ko") 한국어 지역별 요약용
            # DB 구조: m005 이후 PK 재정의 예정, 현재는 보수적 유지

            cursor = (await db.execute(

                select(GameSummaryCursor).where(

                    and_(

                        GameSummaryCursor.game_id == game_id,

                        GameSummaryCursor.summary_type == mode,  # Sprint 3 추가
                        GameSummaryCursor.language_code == cursor_language_code,

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
                    # Sprint 3: 카테고리 포맷 호환성 처리
                    # 이유: 기존 리뷰는 문자열 배열 ["그래픽", "조작감"]이지만
                    #       신규 크롤러는 객체 배열 [{"category": "그래픽", "sentiment": "positive"}, ...] 형식
                    # 목표: 마이그레이션 기간 중 둘 다 지원
                    # TODO(m005 이후): isinstance(item, str) 조건 제거 가능 (신규 포맷만 지원)
                    if isinstance(item, dict):
                        category = item.get("category")
                    elif isinstance(item, str):
                        category = item
                    else:
                        category = None

                    if category:
                        category_freq[str(category)] += 1

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

                language_code=cursor_language_code,

                status="started",

                input_review_count=len(summary_reviews),

                from_review_id=batch_from_review_id,

                to_review_id=new_max_review_id,

            )

            db.add(job)

            await db.flush()

            logger.info("ai pipeline job created: job_id=%s game_id=%s mode=%s", job.id, game_id, mode)



            # 8. 기존 요약본 확인

            # Sprint 3: 요약 조회 로직 변경
            # 이유: 기존 language_code="unified" 워크어라운드 제거 (비표준)
            #       정식 필드 (summary_type, review_language)로 전환
            # 개선: 스키마 명확화 + 쿼리 의도 명시
            # 예시:
            #   - unified: summary_type="unified" AND review_language IS NULL
            #   - regional: summary_type="regional" AND review_language="ko"
            existing_summary = (await db.execute(

                select(GameReviewSummary).where(

                    and_(

                        GameReviewSummary.game_id == game_id,

                        GameReviewSummary.summary_type == mode,  # Sprint 3 추가
                        GameReviewSummary.review_language.is_(None) if review_language is None else GameReviewSummary.review_language == review_language,  # Sprint 3 추가

                        GameReviewSummary.is_current == True,

                    )

                )

            )).scalar_one_or_none()

            prior_summary_text = existing_summary.summary_text if existing_summary else None



            # 9. 하이브리드 파이프라인 실행

            reduce_language = "ko"

            logger.info("ai pipeline summary generation started: game_id=%s mode=%s job_id=%s", game_id, mode, job.id)



            map_results, ai_result = await run_hybrid_summary_pipeline(

                game_id=game_id,

                language_code=reduce_language,

                all_reviews=new_reviews,

                steam_ratio=(steam_pos, steam_neg),

                metacritic_ratio=(meta_pos, meta_mix, meta_neg),

                regional=(mode == "regional"),

                cache=get_redis_cache(),

                ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),

                local_model_name=os.getenv("LOCAL_MAP_MODEL", "gemma3:4b"),

                reduce_api_key=os.getenv("GROQ_API_KEY", ""),

                reduce_model_name=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),

                prior_summary_text=prior_summary_text,

                # Sprint 3: Gemini 자율 생성 항목 근거 제공
                score_anchors=score_anchors,              # Steam 추천율, Metacritic 점수 → sentiment_score 앵커링
                category_frequency=top_categories,        # 실제 리뷰 카테고리 빈도 → keywords 앵커링

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

            # Sprint 3: 토큰 사용량 기록
            # 이유: 운영 비용 추적 & 캐시 효율 모니터링 (기존 컬럼 재활용)
            job.map_output_tokens = sum(getattr(r, "output_tokens", 0) for r in map_results)

            job.reduce_input_tokens = getattr(ai_result, "input_tokens", 0)  # Gemini prompt_token_count

            job.reduce_output_tokens = getattr(ai_result, "output_tokens", 0)  # Gemini candidates_token_count



            # 11. DB 버전 결정

            latest_summary_version = (await db.execute(

                select(func.coalesce(func.max(GameReviewSummary.summary_version), 0)).where(

                    and_(

                        GameReviewSummary.game_id == game_id,

                        GameReviewSummary.summary_type == mode,
                        GameReviewSummary.review_language.is_(None) if review_language is None else GameReviewSummary.review_language == review_language,

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



            # Sprint 3: 요약 메타데이터 기록 변경
            # 이유: unified/regional 구분 명확화 + 향후 조회 용이
            # 변경:
            #   - language_code: 기존 유지 (레거시, m005 제거 예정)
            #   - summary_type: 신규 추가 (unified|regional 모드 명시)
            #   - review_language: 신규 추가 (unified→NULL, regional→언어코드)
            # 활용: 향후 조회시 summary_type + review_language로 구분
            new_summary = GameReviewSummary(

                game_id=game_id,

                language_code=cursor_language_code,      # 레거시 필드

                summary_type=mode,                        # Sprint 3 추가

                review_language=review_language,

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

                # Sprint 3: Gemini 출력 신뢰도 평가 (결정론적, 추가 LLM 호출 없음)
            # 이유: 파이프라인 실행마다 결과 품질을 정량화 → 운영 모니터링용
            # 지표:
            #   - schema_compliance: 9개 필드 채움 비율 (0.0~1.0)
            #   - hallucination_score: 인용된 review_id 실존 비율 (0.0~1.0)
            #   - sentiment_consistency: 레이블 vs 점수 범위 일치 (0|1)
            #   - anchor_deviation: |sentiment_score - steam_ratio| / 100
            # 활용: 운영 임계값 설정 (schema_compliance < 0.8 → 경고)
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

                loop = asyncio.get_running_loop()
                # Sprint 3: 의미 유사도 계산 (다국어 임베딩)
                # 이유: 요약이 원본 리뷰를 얼마나 잘 반영하는지 측정
                # 방식: 리뷰 평균 embedding vs 요약 embedding → cosine similarity (0.0~1.0)
                # 모델: paraphrase-multilingual-MiniLM-L12-v2 (50개 언어 지원)
                # 주의: 동기 CPU 연산이므로 run_in_executor로 감싸서 이벤트 루프 블로킹 방지
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
            # 수정됨(Sprint 3): 이 블록은 Sprint 3에서 업데이트되어
            # summary_type 메타필드가 커서/조회 파이프라인에 포함되도록 변경되었습니다.
            # Sprint 3: 커서 메타데이터 통합 (summary_type 기록)
            # 이유: 다음 파이프라인 실행 시 unified/regional 구분 명확화
            # 변경:
            #   - language_code: 기존 유지 (PK, "unified" 또는 언어코드)
            #   - summary_type: 신규 기록 ("unified" 또는 "regional")
            # 활용: Cursor 조회 시 (위 #1 참고) summary_type 필터로 구분

            if cursor:

                cursor.last_summarized_review_id = new_max_review_id

                cursor.last_summary_version = new_version

                cursor.updated_at = datetime.utcnow()

            else:

                db.add(GameSummaryCursor(

                    game_id=game_id,

                    language_code=cursor_language_code,
                    summary_type=mode,

                    last_summarized_review_id=new_max_review_id,

                    last_summary_version=new_version,

                ))



            job.status = "success"

            job.ended_at = datetime.utcnow()

            await db.commit()

            logger.info(

                "ai pipeline finished: game_id=%s mode=%s language=%s job_id=%s",

                game_id, mode, cursor_language_code, job.id,

            )



            # 17. Redis 캐시 무효화

            await invalidate_summary_cache(game_id, cursor_language_code)



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

                game_id, mode, cursor_language_code, e,

            )