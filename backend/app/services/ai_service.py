import sys
import os
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, and_

from app.models.domain import ExternalReview, GameSummaryCursor, ReviewSummaryJob, GameReviewSummary
from app.core.redis_client import invalidate_summary_cache

# 🚀 핵심: 외부에 있는 ai-pipeline 모듈을 파이썬 경로에 추가하여 가져옵니다.
AI_PIPELINE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../ai-pipeline"))
sys.path.append(AI_PIPELINE_PATH)

# ai-pipeline 폴더 내의 모듈들을 import (해당 파일들이 존재한다고 가정)
try:
    from app.map_reduce.pipeline import run_map_reduce
    from app.map_reduce.sampler import StratifiedSampler
except ImportError as e:
    print(f"[Warning] ai-pipeline 모듈을 찾을 수 없습니다. 경로를 확인하세요: {e}")

async def run_ai_pipeline_task(game_id: int, language_code: str, db: AsyncSession):
    """
    ai-pipeline 폴더의 Map-Reduce 로직을 실행하는 백그라운드 태스크
    """
    job = None
    try:
        # 1. 커서 확인
        cursor = (await db.execute(select(GameSummaryCursor).where(
            and_(GameSummaryCursor.game_id == game_id, GameSummaryCursor.language_code == language_code)
        ))).scalar_one_or_none()
        last_review_id = cursor.last_summarized_review_id if cursor else 0

        # 2. 층화 추출 (ai-pipeline의 Sampler 활용)
        query = select(ExternalReview).where(
            and_(ExternalReview.game_id == game_id, ExternalReview.id > last_review_id, ExternalReview.is_deleted == False)
        ).order_by(desc(ExternalReview.helpful_count)).limit(1000) # 일단 후보군 확보
        
        raw_reviews = (await db.execute(query)).scalars().all()
        if not raw_reviews:
            return

        # ai-pipeline의 샘플러를 통해 최적의 300개만 필터링
        sampled_reviews = StratifiedSampler.sample(raw_reviews, target_size=300)
        review_texts = [r.review_text_clean for r in sampled_reviews]
        new_max_review_id = max(r.id for r in sampled_reviews)

        # 3. 기존 요약본 확인 (Rolling Update 여부 판별)
        existing_summary = (await db.execute(select(GameReviewSummary).where(
            and_(GameReviewSummary.game_id == game_id, GameReviewSummary.is_current == True)
        ))).scalar_one_or_none()

       # 4. Job 시작 기록 (language_code 추가)
        job = ReviewSummaryJob(
            game_id=game_id, 
            language_code=language_code, # 👈 누락되었던 언어 코드 추가
            status='started', 
            input_review_count=len(sampled_reviews)
        )
        db.add(job)
        await db.flush()

        # 5. 🚀 ai-pipeline의 Map-Reduce 엔진 가동!
        ai_result = await run_map_reduce(
            reviews=review_texts, 
            existing_summary_text=existing_summary.summary_text if existing_summary else None
        )

        # 6. DB 결과 저장
        if existing_summary:
            existing_summary.is_current = False
            
        # 🚀 에러 원인 해결: new_version 변수를 명시적으로 먼저 선언합니다.
        new_version = (cursor.last_summary_version + 1) if cursor else 1
            
        new_summary = GameReviewSummary(
            game_id=game_id, language_code=language_code, job_id=job.id,
            summary_version=new_version, # 👈 선언한 변수 사용
            summary_text=ai_result.get("summary_text"),
            pros_json=ai_result.get("pros_json", []),
            cons_json=ai_result.get("cons_json", []),
            keywords_json=ai_result.get("keywords_json", []),
            covered_from_review_id=last_review_id,      
            covered_to_review_id=new_max_review_id,
            is_current=True
        )
        db.add(new_summary)

        # 🚀 커서 최신화 로직 수정
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

        # Job 완료 처리
        job.status = 'success'
        job.ended_at = datetime.utcnow()
        await db.commit()

        # 7. 🧹 Redis 캐시 무효화
        await invalidate_summary_cache(game_id, language_code)
        print(f"[AI Pipeline] 게임 {game_id} ({language_code}) 롤링 업데이트 및 Redis 캐시 갱신 완료!")

    except Exception as e:
        await db.rollback()
        if job:
            job.status = 'failed'
            job.error_message = str(e)
            await db.commit()
        print(f"[AI Service Error] {e}")