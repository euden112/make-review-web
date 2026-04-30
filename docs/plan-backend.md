# 스프린트 기획 — Backend

> 대상 파일: `backend/app/services/ai_service.py`, `backend/app/api/v1/summaries.py`

---

## 변경 사항 요약

| 파일 | 변경 항목 |
|------|---------|
| `ai_service.py` | 언어 파이프라인 모드 분기, Map 캐시 연결, 신뢰도/품질 지표 계산 및 저장 |
| `summaries.py` | 엔드포인트 분리, print → logger 교체 |

---

## ai_service.py

### 1. 언어 파이프라인 모드 분기 (항목 03)

현재 `run_ai_pipeline_task(game_id, language_code)` 단일 함수가 하나의 파이프라인만 실행한다.

변경 후 `POST /summarize` 트리거 시 통합(unified) 1개 + 지역별(regional) N개 작업을 BackgroundTasks에 등록한다.

```python
# 트리거 시 실행할 작업 목록 구성
async def get_pipeline_tasks(game_id: int) -> list[tuple]:
    # DB에서 해당 게임에 수집된 언어 종류 조회
    distinct_langs = await db.scalars(
        select(ExternalReview.language_code).distinct().where(
            ExternalReview.game_id == game_id,
            ExternalReview.is_deleted == False,
        )
    )
    tasks = [("unified", None)]  # 통합 요약
    tasks += [("regional", lang) for lang in distinct_langs]
    return tasks
```

**통합 모드**: 리뷰 언어 필터 없이 전체 리뷰 사용, Reduce 출력 언어 `"ko"` 고정.

**지역별 모드**: `review_language` 기준으로 리뷰 필터링, 간략 Reduce 프롬프트 사용 (2~3문장).

### 2. Map 캐시 연결 (항목 05 선행 조건)

현재 `cache=None`으로 `_NullAsyncCache`(항상 미스)가 사용된다. 캐시 히트 로깅 전에 반드시 수정.

```python
# 현재
map_results, ai_result = await run_hybrid_summary_pipeline(
    ...
    cache=None,
    ...
)

# 변경
from app.core.redis_client import get_redis_cache
map_results, ai_result = await run_hybrid_summary_pipeline(
    ...
    cache=get_redis_cache(),
    ...
)
```

### 3. ReviewSummaryJob 토큰/캐시 기록 (항목 05)

파이프라인 완료 후 `map_results`와 `ai_result`에서 값을 추출하여 저장한다.

```python
job.chunk_count       = len(map_results)
job.map_cache_hit     = sum(1 for r in map_results if r.cached)
job.map_cache_miss    = sum(1 for r in map_results if not r.cached)
job.map_input_tokens  = sum(r.input_tokens for r in map_results)
job.map_output_tokens = sum(r.output_tokens for r in map_results)
job.reduce_input_tokens  = ai_result.reduce_input_tokens
job.reduce_output_tokens = ai_result.reduce_output_tokens
```

### 4. Gemini 자율 생성 항목 앵커링 데이터 준비 (항목 06)

Reduce 호출 전 두 가지 데이터를 집계하여 `run_reduce_stage`에 전달한다.

**카테고리 빈도 집계** (`keywords` 앵커링):
```python
from collections import Counter

category_freq: Counter = Counter()
for review in summary_reviews:
    for item in (review.review_categories_json or []):
        category_freq[item["category"]] += 1
# 상위 8개 → reduce 프롬프트에 전달
top_categories = category_freq.most_common(8)
```

**점수 앵커 준비** (`sentiment_score` 앵커링):
```python
score_anchors = {
    "steam_recommend_ratio": steam_recommend_ratio,
    "metacritic_critic_avg": metacritic_critic_avg,
    "metacritic_user_avg": metacritic_user_avg,
}
# ai_service.py에서 이미 계산된 값이므로 전달만 추가
```

### 5. 요약 신뢰도 지표 계산 및 저장 (항목 01)

```python
total_reviews_in_db = await db.scalar(
    select(func.count(ExternalReview.id)).where(
        ExternalReview.game_id == game_id,
        ExternalReview.is_deleted == False,
    )
)
new_count = await db.scalar(
    select(func.count(ExternalReview.id)).where(
        ExternalReview.game_id == game_id,
        ExternalReview.id > cursor.last_summarized_review_id,
        ExternalReview.is_deleted == False,
    )
)

new_summary.sentiment_alignment = (
    1 - abs(float(ai_result.sentiment_score) - steam_recommend_ratio) / 100
    if ai_result.sentiment_score is not None and steam_recommend_ratio is not None
    else None
)
new_summary.coverage_ratio  = source_review_count / total_reviews_in_db if total_reviews_in_db else None
new_summary.staleness_ratio = new_count / total_reviews_in_db if total_reviews_in_db else None
```

### 6. Gemini 출력 신뢰도 지표 계산 및 저장 (항목 07)

```python
from ai_module.evaluation.gemini_reliability import compute_gemini_reliability

reliability = compute_gemini_reliability(
    ai_result=ai_result,
    input_reviews=new_reviews,
    steam_recommend_ratio=steam_recommend_ratio,
)
job.schema_compliance     = reliability.schema_compliance
job.hallucination_score   = reliability.hallucination_score
job.sentiment_consistency = reliability.sentiment_consistency
job.anchor_deviation      = reliability.anchor_deviation
```

### 7. 임베딩 유사도 계산 및 저장 (항목 04)

sentence-transformer는 동기 CPU 연산이므로 `run_in_executor`로 감싸야 이벤트 루프 블로킹을 방지한다.

```python
from ai_module.evaluation.semantic_similarity import compute_semantic_similarity
import asyncio

loop = asyncio.get_event_loop()
similarity = await loop.run_in_executor(
    None,
    compute_semantic_similarity,
    [r.review_text_clean for r in selected_reviews[:50]],
    ai_result.full_text,
)
new_summary.semantic_similarity_score = similarity
```

---

## summaries.py

### 엔드포인트 분리 (항목 03)

```python
# 현재
GET  /api/v1/summaries/{game_id}            → 단일 요약 반환
POST /api/v1/summaries/{game_id}/summarize  → language 파라미터 수신

# 변경
GET  /api/v1/games/{game_id}/summary        → 통합 요약 반환
GET  /api/v1/games/{game_id}/perspectives   → 언어권별 시각 목록 반환
POST /api/v1/summaries/{game_id}/summarize  → language 파라미터 제거, unified+regional 일괄 트리거
```

### Redis 캐시 로깅 교체 (항목 05)

```python
# 현재
print(f"⚡ [Redis Cache Hit] 게임 {game_id} 요약본 즉시 반환")
print(f"💾 [Redis Cache Set] 게임 {game_id} 요약본 DB 조회 후 캐싱 완료")

# 변경
logger.info("cache_hit game_id=%s summary_type=%s", game_id, summary_type)
logger.info("cache_miss game_id=%s summary_type=%s", game_id, summary_type)
```
