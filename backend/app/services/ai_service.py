import sys
import os
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from pathlib import Path

from app.models.domain import ExternalReview, GameSummaryCursor, ReviewSummaryJob, GameReviewSummary, Platform
from app.core.redis_client import invalidate_summary_cache

# 🚀 핵심: 외부에 있는 ai-pipeline 모듈 경로 추가
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
AI_PIPELINE_PATH = os.path.join(PROJECT_ROOT, "ai-pipeline")

if AI_PIPELINE_PATH not in sys.path:
    sys.path.append(AI_PIPELINE_PATH)

try:
    # 👇 변경된 파이프라인 함수와 클래스 임포트
    from app.map_reduce.pipeline import run_hybrid_summary_pipeline
    from app.map_reduce.reduce_api import FinalSummary
except ImportError as e:
    print(f"[Warning] ai-pipeline 모듈을 찾을 수 없습니다. 경로를 확인하세요: {e}")

async def run_ai_pipeline_task(game_id: int, language_code: str, db: AsyncSession):
    job = None
    try:
        # 1. 커서 확인
        cursor = (await db.execute(select(GameSummaryCursor).where(
            and_(GameSummaryCursor.game_id == game_id, GameSummaryCursor.language_code == language_code)
        ))).scalar_one_or_none()
        last_review_id = cursor.last_summarized_review_id if cursor else 0

        # 2. 리뷰 원본 전체 확보 (샘플링은 파이프라인 내부에서 수행)
        query = select(ExternalReview).where(
            and_(ExternalReview.game_id == game_id, ExternalReview.id > last_review_id, ExternalReview.is_deleted == False)
        )
        raw_reviews = (await db.execute(query)).scalars().all()
        if not raw_reviews:
            print(f"[AI Service] 게임 {game_id}에 대한 새로운 리뷰가 없습니다.")
            return
            
        new_max_review_id = max(r.id for r in raw_reviews)

        # 3. 플랫폼 매핑 및 층화 추출용 비율 계산 로직
        platforms = (await db.execute(select(Platform))).scalars().all()
        steam_pid = next((p.id for p in platforms if p.code == 'steam'), None)
        meta_pid = next((p.id for p in platforms if p.code == 'metacritic'), None)

        steam_pos = sum(1 for r in raw_reviews if r.platform_id == steam_pid and r.is_recommended is True)
        steam_neg = sum(1 for r in raw_reviews if r.platform_id == steam_pid and r.is_recommended is False)
        
        meta_pos = sum(1 for r in raw_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 >= 75)
        meta_mix = sum(1 for r in raw_reviews if r.platform_id == meta_pid and r.normalized_score_100 and 50 <= r.normalized_score_100 < 75)
        meta_neg = sum(1 for r in raw_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 < 50)

        # 4. Job 시작 기록
        job = ReviewSummaryJob(
            game_id=game_id, 
            language_code=language_code, 
            status='started', 
            input_review_count=len(raw_reviews)
        )
        db.add(job)
        await db.flush()

        # 5. 기존 요약본 확인
        existing_summary = (await db.execute(select(GameReviewSummary).where(
            and_(GameReviewSummary.game_id == game_id, GameReviewSummary.language_code == language_code, GameReviewSummary.is_current == True)
        ))).scalar_one_or_none()

        # 6. 🚀 새로운 하이브리드 파이프라인 가동!
        # 환경변수에서 설정값을 가져오거나 기본값을 줍니다.
        ai_result: FinalSummary = await run_hybrid_summary_pipeline(
            game_id=game_id,
            language_code=language_code,
            all_reviews=raw_reviews,
            steam_ratio=(steam_pos, steam_neg),
            metacritic_ratio=(meta_pos, meta_mix, meta_neg),
            cache=None, # 현재 Redis 설정 방식에 맞게 조정 가능
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            local_model_name=os.getenv("LOCAL_MAP_MODEL", "llama3"),
            reduce_api_key=os.getenv("GEMINI_API_KEY", "your-api-key"), # 👈 필수 환경변수
            reduce_model_name="gemini-1.5-flash",
        )

        # (참고) 새 파이프라인에서 토큰 usage 반환이 사라졌으므로, 
        # DB 컬럼인 map_input_tokens 등은 0 기본값으로 유지됩니다.

        # 7. DB 결과 저장
        if existing_summary:
            existing_summary.is_current = False
            
        new_version = (cursor.last_summary_version + 1) if cursor else 1
            
        new_summary = GameReviewSummary(
            game_id=game_id, 
            language_code=language_code, 
            job_id=job.id,
            summary_version=new_version, 
            
            # 👇 FinalSummary 데이터클래스 속성으로 접근하여 매핑
            summary_text=f"**{ai_result.one_liner}**\n\n{ai_result.full_text}",
            sentiment_overall=None,
            aspect_sentiment_json=ai_result.aspect_scores,
            representative_reviews_json=ai_result.representative_reviews,
            
            # 파이프라인에서 삭제된 항목들은 빈 값 처리
            pros_json=[],
            cons_json=[],
            keywords_json=[],
            
            covered_from_review_id=last_review_id,      
            covered_to_review_id=new_max_review_id,
            is_current=True
        )
        db.add(new_summary)

        # 8. 커서 최신화
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

        # 9. 🧹 Redis 캐시 무효화
        await invalidate_summary_cache(game_id, language_code)
        print(f"[AI Pipeline] 게임 {game_id} ({language_code}) 롤링 업데이트 완료!")

    except Exception as e:
        await db.rollback()
        if job:
            job.status = 'failed'
            job.error_message = str(e)
            await db.commit()
        print(f"[AI Service Error] {e}")