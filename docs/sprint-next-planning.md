# 다음 스프린트 기획안

> 작성일: 2026-04-23
> 대상: AI 요약 파이프라인 고도화

---

## 개요

현재 데모 수준의 파이프라인을 실제 서비스 품질로 끌어올리기 위한 기능 추가 및 구조 변경 사항을 정리한다.
크게 일곱 가지 축으로 구성된다.

1. 요약 신뢰도 지표 (운영 모니터링용)
2. 카테고리별 감성 분석 근거 확보
3. 언어 파이프라인 구조 변경 (통합 요약 + 언어권별 시각)
4. 임베딩 기반 요약 품질 평가 (운영 모니터링용)
5. 운영 로깅 (토큰 사용량, 캐시 히트)
6. Gemini 자율 생성 항목 근거 확보
7. Gemini 출력 신뢰도 지표 (운영 모니터링용)

> **신뢰도 지표 (1, 4, 7)는 모두 운영 모니터링 전용이며 API로 사용자에게 노출하지 않는다.**

---

## 1. 요약 신뢰도 지표 (운영 모니터링용)

### 배경

현재 `sentiment_score`는 Gemini가 텍스트 인상만으로 판단한 값이다. Steam 추천율(`steam_recommend_ratio`), Metacritic 평균(`metacritic_critic_avg`, `metacritic_user_avg`)은 DB에 저장되어 있으나 신뢰도 평가에 활용되지 않는다. 파이프라인 실행마다 요약 품질을 정량적으로 기록하여 운영 모니터링에 활용한다.

### 목표

파이프라인 완료 시 신뢰도 수치를 DB에 기록한다. API 노출 대상이 아니다.

### 지표 정의

| 지표 | 계산 방식 | 데이터 출처 |
|------|----------|------------|
| `sentiment_alignment` | `1 - \|sentiment_score - steam_recommend_ratio\| / 100` | DB 기존 컬럼 |
| `coverage_ratio` | `source_review_count / total_reviews_in_db` | DB 집계 |
| `staleness_ratio` | `new_reviews_since_last_summary / total_reviews_in_db` | DB 집계 |

`staleness_ratio` 계산 쿼리:
```python
# GameSummaryCursor.last_summarized_review_id 기준
new_count = await db.scalar(
    select(func.count(ExternalReview.id)).where(
        ExternalReview.game_id == game_id,
        ExternalReview.id > cursor.last_summarized_review_id,
        ExternalReview.is_deleted == False,
    )
)
staleness_ratio = new_count / total_reviews_in_db if total_reviews_in_db else 0
```

> Section 3의 언어 파이프라인 변경으로 커서 구조가 `(game_id, summary_type, review_language)`로 바뀌면 이 쿼리도 함께 수정 필요.

### 변경 범위

- **`GameReviewSummary`**: `sentiment_alignment`, `coverage_ratio`, `staleness_ratio` 컬럼 추가
- **`ai_service.py`**: 파이프라인 완료 후 계산하여 저장
- **`summaries.py`**: 변경 없음 (API 노출 대상 아님)

---

## 2. 카테고리별 감성 분석 근거 확보

### 배경

현재 `aspect_scores`(그래픽, 조작감 등 5개 카테고리 점수)는 Gemini가 Map 요약 텍스트만 보고 자체 판단한다. 크롤러가 이미 12개 카테고리로 리뷰를 태깅하고 있지만(`review_categories_json`) 파이프라인에 전달되지 않는다. 또한 카테고리 태깅이 "언급 여부"만 기록하고 긍정/부정을 구분하지 않아, 예를 들어 "그래픽은 훌륭하지만 조작감이 아쉽다"는 리뷰에서 그래픽=긍정, 조작감=부정을 분리할 수 없다.

### 목표

카테고리 점수에 실제 리뷰 데이터 기반의 정량적 근거를 부여한다.

### 단계별 접근

#### Phase 1 — 크롤러 카테고리 태깅 확장

리뷰 전체 감성(추천/비추천)이 아닌 **문장 단위 카테고리별 감성**을 태깅한다.

```
현재: review_categories_json = ["그래픽", "조작감"]
변경: review_categories_json = [
    {"category": "그래픽", "sentiment": "positive"},
    {"category": "조작감", "sentiment": "negative"}
]
```

- 리뷰 텍스트를 문장 단위로 분리
- 각 문장에 카테고리 분류 + 감성 판단 적용
- sentence-transformer가 이미 크롤러에 로드되어 있어 추가 모델 불필요

#### Phase 2 — 카테고리별 통계 집계 후 Reduce 전달

파이프라인 실행 전 DB에서 카테고리별 긍/부정 비율을 집계하여 Reduce 프롬프트에 포함한다.

```
[category_stats]
그래픽: 45 mentions, 84% positive
최적화: 89 mentions, 26% positive
조작감: 31 mentions, 90% positive
```

Gemini는 텍스트 추론과 이 수치를 함께 참고하여 점수를 산출한다.

#### Phase 3 (선택) — 점수 직접 계산, LLM은 설명만 생성

```
category_score = (positive_count / total_count) * 100
```

점수의 수치 근거를 완전히 데이터로 고정하고 LLM은 설명 텍스트만 담당한다.

### 변경 범위

| 위치 | 변경 내용 |
|------|----------|
| `steam_crawler.py` | 문장 단위 카테고리+감성 태깅 로직 추가 |
| `domain.py` | `review_categories_json` 스키마 변경 (문자열 배열 → 객체 배열) |
| `ai_service.py` | 카테고리 통계 집계 후 파이프라인 전달 |
| `reduce_api.py` | Reduce 프롬프트에 category_stats 입력 추가 |

---

## 3. 언어 파이프라인 구조 변경

### 배경

현재 `language_code` 하나가 "리뷰 필터 언어"와 "출력 언어" 두 역할을 동시에 수행한다. 크롤러는 영어 리뷰만 수집하므로 `language_code="ko"` 파이프라인 실행 시 리뷰가 0건이 된다. 서비스 목표에 맞게 **출력은 한국어로 고정**하되, 통합 요약과 언어권별 시각을 분리 제공한다.

### 목표 구조

```
게임 상세 페이지
  ├─ 통합 요약: 전체 리뷰(언어 무관) → 한국어 요약 (메인)
  └─ 언어권별 시각 (간략)
       ├─ 영어권 유저 반응: 영어 리뷰 → 한국어 2~3문장
       ├─ 한국어권 유저 반응: 한국어 리뷰 → 한국어 2~3문장
       └─ ...
```

### DB 스키마 변경

**`GameReviewSummary`**

```python
# 추가
summary_type    = Column(String(16))          # "unified" | "regional"
review_language = Column(String(10), nullable=True)  # unified=None, regional="en"/"ko"

# 제거
# language_code (output은 항상 ko로 고정)

# UniqueConstraint 변경
UniqueConstraint('game_id', 'summary_type', 'review_language', 'summary_version')
```

**`GameSummaryCursor`**

```python
# PK 변경: (game_id, language_code) → (game_id, summary_type, review_language)
```

### 파이프라인 변경

**통합 모드**
- 리뷰 언어 필터 제거 (전체 언어 리뷰 사용)
- Reduce 출력 언어: `"ko"` 고정
- 샘플러: **현행 유지** (Steam pos/neg + Metacritic low/mid/high 버킷, quality_score 기반 선별)
  - 언어별 버킷을 추가하지 않는다
  - 특정 언어권의 리뷰 테러나 편향이 통합 요약에 자연히 희석되는 효과를 의도한다
  - 언어권별 편향은 권역별 시각에서만 드러나도록 설계한다

**지역별 모드**
- 리뷰 언어 필터 유지 (`review_language="en"` 등)
- Reduce 출력 언어: `"ko"` 고정
- 간략 Reduce 프롬프트 사용 (2~3문장)
- 해당 언어권의 실제 반응을 그대로 반영 (편향 포함)

```python
REGIONAL_REDUCE_PROMPT = """
Briefly summarize how {region} players perceive this game in 2-3 sentences.
Focus on what makes their perspective distinctive compared to the general consensus.
Output in Korean.
"""
```

### API 변경

**트리거 엔드포인트**

현재 `POST /{game_id}/summarize?language=ko`는 단일 파이프라인을 직접 호출한다. 구조 변경 후:

```
# 현재
POST /api/v1/summaries/{game_id}/summarize?language=ko

# 변경
POST /api/v1/summaries/{game_id}/summarize
→ 통합 요약(unified) + 수집된 언어 기준 지역별 요약(regional) 모두 트리거
→ BackgroundTasks에 unified 1개 + regional N개 작업을 등록
→ language 파라미터 제거
```

지역별 요약은 별도 트리거 없이 통합 요약과 동시에 자동 실행된다.
수집된 언어 종류는 `ExternalReview.language_code`의 distinct 값으로 결정한다.

**조회 엔드포인트**

```
GET /api/v1/games/{game_id}/summary          → 통합 요약 반환
GET /api/v1/games/{game_id}/perspectives     → 언어권별 시각 목록 반환
```

### 크롤러 변경

```python
# 현재
LANGUAGE         = "english"
ALLOWED_LANGS    = ["en"]
MAX_USER_REVIEWS = 50  # 데모용

# 변경
LANGUAGES        = ["english", "korean", "schinese"]
ALLOWED_LANGS    = ["en", "ko", "zh"]
MAX_USER_REVIEWS = 1000  # 언어당 상한 (게임당 최대 3,000건)
```

Steam API는 `language=all`을 미지원하므로 언어별 순차 호출한다. 층화 추출은 크롤링 단계에서 하지 않고 AI 파이프라인의 `stratified_select_reviews()`에서 담당한다.

**langdetect 제거**: Steam API가 언어를 보장하므로 `language_filter()` 내 `langdetect` 탐지를 제거하고 API 파라미터를 신뢰한다. Metacritic은 영어 전용 플랫폼이므로 `language_filter()` 자체를 제거한다. (데모에서 `langdetect`가 영어 리뷰를 `ko`로 오분류하여 유효 리뷰가 탈락하는 버그 발생 확인)

**키워드 임베딩 사전 계산**: `category_filter()`가 리뷰마다 카테고리 12개 키워드 임베딩을 반복 계산하는 병목을 제거한다. 시작 시 1회 캐싱으로 cap 1,000 기준 ~7분 → ~1.5분으로 단축.

### 변경 범위

| 위치 | 변경 내용 | 수준 |
|------|----------|------|
| `steam_crawler.py` | 다국어 수집, cap 1,000, langdetect 제거, 임베딩 캐싱 | 낮음 |
| `metacritic_crawler.py` | `language_filter()` 제거, langdetect import 삭제 | 낮음 (긴급) |
| `domain.py` | 컬럼 추가/제거, UniqueConstraint 변경 | 중간 |
| `ai_service.py` | 통합/지역별 모드 분기, 리뷰 필터 분리 | 중간 |
| `reduce_api.py` | 지역별 간략 프롬프트 추가 | 낮음 |
| `summaries.py` | `/summary`, `/perspectives` 엔드포인트 분리 | 낮음 |
| DB 마이그레이션 | 컬럼 추가 + 커서 PK 변경 | 중간 |

---

## 4. 임베딩 기반 요약 품질 평가

### 배경

요약이 원본 리뷰를 얼마나 잘 반영하는지 측정하는 자동화 지표가 없다. 리뷰(영어)와 요약(한국어)의 언어가 달라 단순 텍스트 비교는 불가하다.

### 방식

다국어 sentence-transformer를 사용해 리뷰와 요약을 같은 벡터 공간에 임베딩 후 코사인 유사도를 계산한다.

```
샘플 리뷰 N개 → 임베딩 → 평균 벡터
요약 텍스트   → 임베딩
→ cosine_similarity = 0.0 ~ 1.0
```

### 모델 변경

```python
# 현재 (영어 전용, 크롤러에서 사용 중)
SentenceTransformer("all-MiniLM-L6-v2")

# 변경 (50개 언어 지원, 경량 118MB)
SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
```

크롤러의 카테고리 분류기도 같은 모델로 통일한다.

### 새 모듈

`ai-pipeline/ai_module/evaluation/semantic_similarity.py`

```python
def compute_semantic_similarity(
    review_texts: list[str],  # 파이프라인 투입 리뷰 (quality_score 상위 50개)
    summary_text: str,        # Reduce 출력 요약
) -> float:
    ...
```

> **주의**: sentence-transformer 연산은 동기 CPU 연산이다. `ai_service.py`에서 호출 시 반드시 `asyncio.get_event_loop().run_in_executor(None, compute_semantic_similarity, ...)` 형태로 감싸야 FastAPI 이벤트 루프 블로킹을 방지할 수 있다.

### 통합 위치

`ai_service.py`에서 파이프라인 완료 후, DB 저장 전에 계산하여 저장한다.

### DB 변경

```python
# GameReviewSummary에 추가
semantic_similarity_score = Column(Numeric(5, 4))  # 예: 0.7823
```

### 운영 참고

- 점수 절대 기준이 없으므로 초기 여러 게임 실측 후 서비스 기준선 수립 필요
- LLM-as-judge는 상시 측정 대신 주기적 배치 품질 모니터링 용도로 별도 검토

### 변경 범위

| 위치 | 변경 내용 | 수준 |
|------|----------|------|
| `steam_crawler.py` | 모델 교체 (`all-MiniLM` → `multilingual-MiniLM`) | 낮음 |
| `ai_module/evaluation/` | 신규 모듈 생성 | 낮음 |
| `ai_service.py` | 파이프라인 완료 후 유사도 계산 및 저장 | 낮음 |
| `domain.py` | `semantic_similarity_score` 컬럼 추가 | 낮음 |

---

## 5. 운영 로깅

### 배경

`ReviewSummaryJob` 테이블에 토큰 사용량 및 캐시 히트 컬럼이 이미 설계되어 있으나, `ai_service.py`에서 한 번도 값을 채우지 않아 전부 기본값 0으로 방치된 상태다. 요약 API의 Redis 캐시 히트도 `print()`로만 출력하고 있어 운영 가시성이 없다.

### 현재 상태

| 항목 | DB 컬럼 | 실제 기록 여부 |
|------|---------|--------------|
| Map 캐시 히트 수 | `map_cache_hit` | ❌ 항상 0 |
| Map 캐시 미스 수 | `map_cache_miss` | ❌ 항상 0 |
| Map 입력 토큰 | `map_input_tokens` | ❌ 항상 0 |
| Map 출력 토큰 | `map_output_tokens` | ❌ 항상 0 |
| Reduce 입력 토큰 | `reduce_input_tokens` | ❌ 항상 0 |
| Reduce 출력 토큰 | `reduce_output_tokens` | ❌ 항상 0 |
| 청크 수 | `chunk_count` | ❌ 항상 0 |
| 요약 API 캐시 히트 | (없음) | `print()`만 출력 |

### 선행 조건 — Map 캐시 실제 연결

현재 `ai_service.py:156`에서 `cache=None`으로 전달되어 내부적으로 `_NullAsyncCache`(항상 미스, 저장 안 함)가 사용된다. 캐시 히트 로깅을 추가하기 전에 실제 Redis 캐시 인스턴스를 연결해야 한다.

```python
# ai_service.py — 현재
map_results, ai_result = await run_hybrid_summary_pipeline(
    ...
    cache=None,   # ← _NullAsyncCache 사용, 캐시 전혀 동작 안 함
    ...
)

# 변경
from app.core.redis_client import get_redis_cache  # 기존 Redis 연결 활용
map_results, ai_result = await run_hybrid_summary_pipeline(
    ...
    cache=get_redis_cache(),
    ...
)
```

이 변경 없이는 `map_cache_hit`가 항상 0으로 기록된다.

### 목표

파이프라인 실행마다 토큰 비용과 캐시 효율을 DB에 기록하고, 운영 모니터링에 활용한다.

### 수집 항목 및 수집 시점

**Map 단계 (Ollama)**

`MapResult`에 토큰 카운트 필드를 추가하고 Ollama 응답에서 추출한다.

```python
# Ollama /api/chat 응답에 포함된 토큰 정보
data["prompt_eval_count"]  # 입력 토큰
data["eval_count"]         # 출력 토큰
```

청크별로 집계 후 `ReviewSummaryJob`에 합산 저장:

```python
job.chunk_count      = len(map_results)
job.map_cache_hit    = sum(1 for r in map_results if r.cached)
job.map_cache_miss   = sum(1 for r in map_results if not r.cached)
job.map_input_tokens = sum(r.input_tokens for r in map_results)
job.map_output_tokens= sum(r.output_tokens for r in map_results)
```

**Reduce 단계 (Gemini)**

Gemini 응답의 `usage_metadata`에서 추출한다.

```python
response.usage_metadata.prompt_token_count      # 입력 토큰
response.usage_metadata.candidates_token_count  # 출력 토큰
```

```python
job.reduce_input_tokens  = response.usage_metadata.prompt_token_count
job.reduce_output_tokens = response.usage_metadata.candidates_token_count
```

**요약 API Redis 캐시**

현재 `print()`를 `logger.info()`로 교체하고 구조화된 포맷으로 통일한다.

```python
logger.info("cache_hit game_id=%s language=%s", game_id, language)
logger.info("cache_miss game_id=%s language=%s", game_id, language)
```

### 운영 활용 방안

`ReviewSummaryJob` 테이블을 집계하면 다음 지표를 산출할 수 있다.

| 지표 | 계산 방식 | 용도 |
|------|----------|------|
| 게임당 총 토큰 비용 | `map_input + map_output + reduce_input + reduce_output` | 비용 추적 |
| Map 캐시 히트율 | `map_cache_hit / (map_cache_hit + map_cache_miss)` | Map 캐시 효율 |
| 평균 청크당 토큰 | `map_input_tokens / chunk_count` | 청크 크기 조정 기준 |
| 파이프라인 소요 시간 | `ended_at - started_at` | 성능 모니터링 |

### 변경 범위

| 위치 | 변경 내용 | 수준 |
|------|----------|------|
| `map_local.py` | `MapResult`에 `input_tokens`, `output_tokens` 필드 추가 | 낮음 |
| `reduce_api.py` | `FinalSummary`에 토큰 필드 추가, `usage_metadata` 추출 | 낮음 |
| `ai_service.py` | 파이프라인 완료 후 `ReviewSummaryJob` 토큰/캐시 필드 저장 | 낮음 |
| `summaries.py` | `print()` → `logger.info()` 교체 | 낮음 |

---

## 6. Gemini 자율 생성 항목 근거 확보

### 배경

Reduce 단계에서 Gemini에게 요청하는 항목 중 수치 근거 없이 텍스트 인상만으로 자율 생성되는 항목이 다수 존재한다. 실행마다 결과가 달라질 수 있고 실제 리뷰 데이터와 괴리될 위험이 있다.

### 자율 생성 항목 현황

| 항목 | 현재 방식 | 문제 |
|------|----------|------|
| `keywords` | 프롬프트에 `keywords: [string]` 한 줄만 명시 | 개수·형태·근거 기준 없음 |
| `sentiment_score` | 텍스트 인상으로 0-100 수치 산출 | Steam 추천율·Metacritic 점수 미반영 |
| `aspect_scores` | 고정 5개 카테고리 점수를 텍스트 추론으로 산출 | Section 2에서 `review_categories_json` 기반 수정 예정 |
| `pros` / `cons` | 임의 선별, 빈도 근거 없음 | 실제로 많이 언급된 항목인지 불명확 |
| `representative_reviews` | Gemini가 임의 선택 | 실제 유용한 리뷰(helpful_count 높은)와 무관할 수 있음 |

### 항목별 개선 방안

#### 6-1. `keywords` — 카테고리 빈도 기반

`ai_service.py`에서 Reduce 호출 전 `review_categories_json` 빈도를 집계하여 상위 항목을 프롬프트에 전달한다.

```
[category_frequency]
버그/안정성: 38회
스토리/세계관: 31회
최적화: 27회
...
→ keywords에 상위 빈도 카테고리를 반드시 포함할 것
```

#### 6-2. `sentiment_score` — 데이터 앵커링

Steam 추천율과 Metacritic 평균을 Reduce 프롬프트에 포함하여 수치 근거로 사용한다.

```
[score_anchors]
steam_recommend_ratio: 87.3%
metacritic_critic_avg: 88.0
metacritic_user_avg: 79.0
→ sentiment_score는 위 수치를 참고하여 산출할 것
```

현재 `ai_service.py`에서 이미 계산되어 DB에 저장되는 값이므로 프롬프트에 전달만 추가하면 된다.

#### 6-3. `pros` / `cons` — 빈도 상위 카테고리에서 도출

Section 2 카테고리별 감성 분석이 완료된 이후 적용 가능하다. 빈도 상위 긍정 카테고리 → pros, 부정 카테고리 → cons로 유도한다.

```
[category_stats]
그래픽: 45 mentions, 84% positive   → pros 후보
최적화: 89 mentions, 26% positive   → cons 후보
```

#### 6-4. `representative_reviews` — 선택 기준 명시

현재 선택 기준이 프롬프트에 없다. 다음 기준을 명시한다.

```
representative_reviews 선택 기준:
1. helpful_count 높은 리뷰 우선
2. playtime_hours 10시간 이상 리뷰 우선
3. 긍정/부정 균형 (각 1~2개)
4. 직접 인용 가능한 길이 (50-200자)
```

Reduce 프롬프트에 위 기준을 추가하고, 샘플러에서 선별된 리뷰 메타데이터(helpful_count, playtime_hours)를 Map 청크에 포함시킨다.

### 변경 범위

| 위치 | 변경 내용 | 수준 |
|------|----------|------|
| `ai_service.py` | 카테고리 빈도 집계 + score_anchors 준비 | 낮음 |
| `reduce_api.py` | 프롬프트에 category_frequency, score_anchors, representative 선택 기준 추가 | 낮음 |
| `map_local.py` | 청크 텍스트에 helpful_count, playtime 메타 포함 | 낮음 |

> `aspect_scores` 근거는 Section 2(카테고리별 감성 분석)와 통합 처리한다.

---

## 7. Gemini 출력 신뢰도 지표 (운영 평가용)

### 배경

Gemini가 생성한 결과물이 실제로 신뢰할 수 있는지 판단할 기준이 없다. 추가 LLM 호출 없이 **입력 데이터와 출력 결과를 결정론적으로 비교**하는 방식으로 4개 지표를 정의한다. 파이프라인 완료 후 즉시 계산하여 `ReviewSummaryJob`에 저장하고 운영 모니터링에 활용한다.

### 지표 정의

#### 7-1. `schema_compliance` (구조 검증)

Gemini 응답이 요구 스키마를 얼마나 충족했는지 0.0~1.0으로 표현한다.

```
검사 항목 (각 1점):
- one_liner: 비어있지 않은 문자열
- sentiment_overall: "positive" | "mixed" | "negative" 중 하나
- sentiment_score: 0~100 범위의 숫자
- aspect_scores: 최소 1개 이상의 항목 포함
- pros: 비어있지 않은 리스트
- cons: 비어있지 않은 리스트
- keywords: 비어있지 않은 리스트
- representative_reviews: 비어있지 않은 리스트
- full_text: 비어있지 않은 문자열

schema_compliance = 통과 항목 수 / 9
```

#### 7-2. `hallucination_score` (환각 탐지)

`representative_reviews`에 인용된 `review_id`가 실제 파이프라인 입력 리뷰에 존재하는 비율. 1.0이면 환각 없음.

```python
input_ids = {r.id for r in all_reviews}
cited_ids = [r["review_id"] for r in representative_reviews if "review_id" in r]

if not cited_ids:
    hallucination_score = None  # 인용 없음, 측정 불가
else:
    hallucination_score = sum(1 for rid in cited_ids if rid in input_ids) / len(cited_ids)
```

점수가 1.0 미만이면 Gemini가 존재하지 않는 리뷰 ID를 생성한 것이다.

#### 7-3. `sentiment_consistency` (내부 일관성)

`sentiment_overall` 레이블과 `sentiment_score` 수치가 내부적으로 일치하는지 검사한다.

```
일치 기준:
- sentiment_overall = "positive"  → sentiment_score >= 65
- sentiment_overall = "mixed"     → 35 <= sentiment_score < 65
- sentiment_overall = "negative"  → sentiment_score < 35

sentiment_consistency = 1 (일치) | 0 (불일치)
```

불일치 시 Gemini가 텍스트 추론과 수치 판단을 일관되게 수행하지 못한 것이다.

#### 7-4. `anchor_deviation` (외부 데이터 이탈도)

`sentiment_score`와 Steam 추천율(`steam_recommend_ratio`)의 편차. 낮을수록 신뢰도 높음.

```python
# steam_recommend_ratio가 있을 때만 계산
anchor_deviation = abs(sentiment_score - steam_recommend_ratio) / 100
# 0.0 (완전 일치) ~ 1.0 (완전 반전)
```

> Section 1의 `sentiment_alignment`와 동일 계산식이나, 저장 위치가 다르다.
> `sentiment_alignment`는 `GameReviewSummary`에 저장 (요약 단위),
> `anchor_deviation`은 `ReviewSummaryJob`에 저장 (실행 단위).
> 둘 다 운영 모니터링 전용이며 API로 노출하지 않는다.

### DB 변경

`ReviewSummaryJob`에 신뢰도 지표 컬럼 4개를 추가한다.

```python
# ReviewSummaryJob에 추가
schema_compliance      = Column(Numeric(4, 3))   # 0.000 ~ 1.000
hallucination_score    = Column(Numeric(4, 3), nullable=True)  # cited_ids 없으면 NULL
sentiment_consistency  = Column(SmallInteger)    # 0 또는 1
anchor_deviation       = Column(Numeric(4, 3), nullable=True)  # steam_ratio 없으면 NULL
```

### 계산 위치

`ai_service.py`에서 `run_hybrid_summary_pipeline()` 완료 직후, DB 저장 전에 계산한다.

```python
# ai_service.py — 파이프라인 완료 후
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

### 새 모듈

`ai-pipeline/ai_module/evaluation/gemini_reliability.py`

```python
@dataclass
class GeminiReliabilityResult:
    schema_compliance: float
    hallucination_score: float | None
    sentiment_consistency: int
    anchor_deviation: float | None
```

### 운영 활용 방안

| 지표 | 임계 기준 (예시) | 조치 |
|------|----------------|------|
| `schema_compliance` | < 0.8 | 파이프라인 재실행 또는 알림 |
| `hallucination_score` | < 1.0 | 대표 리뷰 인용 신뢰 불가, 경고 태깅 |
| `sentiment_consistency` | = 0 | Gemini 수치 판단 불안정 — 재실행 후 비교 |
| `anchor_deviation` | > 0.3 | Steam 추천율과 AI 점수 크게 이탈 — 수동 검토 |

임계 기준은 초기 여러 게임 실측 후 조정한다.

### 변경 범위

| 위치 | 변경 내용 | 수준 |
|------|----------|------|
| `ai_module/evaluation/gemini_reliability.py` | 신규 모듈 생성 | 낮음 |
| `ai_service.py` | 파이프라인 완료 후 신뢰도 계산 및 저장 | 낮음 |
| `domain.py` | `ReviewSummaryJob`에 신뢰도 컬럼 4개 추가 | 낮음 |

---

## 변경 우선순위 요약

| 순위 | 항목 | 이유 |
|------|------|------|
| 1 | 언어 파이프라인 구조 변경 | 현재 `language_code="ko"` 실행 시 리뷰 0건 — 기능 자체가 동작 안 함 |
| 2 | 운영 로깅 + Map 캐시 연결 | DB 컬럼 이미 존재 + `cache=None` 수정 선행 필요 — 묶음 처리 |
| 3 | Gemini 자율 생성 근거 확보 (`keywords`, `sentiment_score`, `representative_reviews`) | 프롬프트 수정 + ai_service 집계 추가 — 범위 작음, 즉시 품질 향상 |
| 4 | Gemini 출력 신뢰도 지표 (운영 평가) | 신규 모듈 + 컬럼 4개 추가 — 범위 작음, 운영 품질 가시성 확보 |
| 5 | 요약 신뢰도 지표 | DB 데이터 이미 존재, 집계 쿼리 추가로 즉시 가치 창출 |
| 6 | 임베딩 기반 요약 품질 평가 | 모델 교체 + 신규 모듈, run_in_executor 처리 필요 |
| 7 | 카테고리별 감성 분석 | 크롤러 재수집 필요, 범위 가장 큼 |
| 7 완료 후 | Gemini pros/cons 근거 확보 (Section 6-3) | 카테고리 감성 분석(7) Phase 1 완료 이후 적용 가능 |

### DB 마이그레이션 통합 계획

모든 섹션의 스키마 변경을 Alembic 마이그레이션 단위로 정리한다.

| 마이그레이션 | 대상 테이블 | 변경 내용 | 선행 섹션 |
|---|---|---|---|
| `m001_language_pipeline` | `GameReviewSummary`, `GameSummaryCursor` | `summary_type`, `review_language` 추가 / `language_code` 제거 / UniqueConstraint 변경 / 커서 PK 변경 | Section 3 |
| `m002_operational_logging` | `ReviewSummaryJob` | 토큰/캐시 컬럼 (기존 컬럼, 값만 채움 — 마이그레이션 불필요) | Section 5 |
| `m003_gemini_reliability` | `ReviewSummaryJob` | `schema_compliance`, `hallucination_score`, `sentiment_consistency`, `anchor_deviation` 추가 | Section 7 |
| `m004_summary_reliability` | `GameReviewSummary` | `sentiment_alignment`, `coverage_ratio`, `staleness_ratio` 추가 | Section 1 |
| `m005_semantic_similarity` | `GameReviewSummary` | `semantic_similarity_score` 추가 | Section 4 |

> `language_code` 컬럼 제거(`m001`) 시 기존 데이터 처리 방침: 마이그레이션 전 `summary_type='unified'`, `review_language=NULL`로 일괄 변환 후 컬럼 삭제.
