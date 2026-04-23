# 스프린트 기획 — Crawling

> 대상 파일: `crawling/steam/steam_crawler.py`

---

## 변경 사항 요약

| 변경 항목 | 관련 항목 |
|---------|---------|
| 다국어 수집 지원 (한/영/중) | 03 |
| 카테고리 태깅 → 문장 단위 감성 포함으로 확장 | 02 |
| 임베딩 모델 교체 (영어 전용 → 다국어) | 04 |

---

## 1. 다국어 수집 지원 (항목 03)

현재 영어 리뷰만 수집하므로 한국어/중국어 파이프라인 실행 시 리뷰 0건이 된다.

```python
# 현재
LANGUAGE      = "english"
ALLOWED_LANGS = ["en"]
MAX_USER_REVIEWS = 50  # 데모용

# 변경
LANGUAGES        = ["english", "korean", "schinese"]
ALLOWED_LANGS    = ["en", "ko", "zh"]
MAX_USER_REVIEWS = 1000  # 언어당 상한
```

Steam API는 `language` 파라미터로 언어별 독립 요청을 지원하므로 언어별로 별도 호출한다. `language=all` 파라미터는 미지원이므로 언어별 순차 호출이 필요하다.

### 수집량 구조

`MAX_USER_REVIEWS`는 **언어당** 적용된다.

```
게임 1개 기준:
  english  → 최대 1,000건
  korean   → 최대 1,000건
  schinese → 최대 1,000건
  합계: 최대 3,000건 / 게임
```

한국어·중국어 리뷰가 cap보다 적은 게임(소량 신작 등)은 Steam API가 있는 만큼만 반환하므로 자동 처리된다. 층화 추출은 크롤링 단계에서 하지 않고 AI 파이프라인의 `stratified_select_reviews()`에서 담당한다.

---

## 2. 카테고리 태깅 확장 (항목 02)

### 현재 방식

리뷰 전체를 카테고리 분류기에 입력하여 "언급된 카테고리" 목록만 기록한다.

```python
# 출력
review_categories_json = ["그래픽", "조작감"]
```

### 변경 방식

리뷰를 문장 단위로 분리한 후 각 문장에 카테고리 분류 + 감성 판단을 적용한다.

```python
# 출력
review_categories_json = [
    {"category": "그래픽", "sentiment": "positive"},
    {"category": "조작감", "sentiment": "negative"}
]
```

### 구현 포인트

- `sent_tokenize` 또는 `.split('.')` 등으로 문장 분리
- 문장별 카테고리 임베딩 유사도 계산 (기존 로직 유지)
- 감성 판단: 문장 내 부정 표현 키워드 감지 또는 감성 임베딩 비교
  - 간단 구현: `["not", "bad", "terrible", "awful", "poor", "broken", "hate", "disappointing"]` 등 부정 키워드 존재 시 `"negative"`, 없으면 `"positive"`
  - 정밀 구현: `positive` / `negative` 문장 예시 임베딩과 코사인 유사도 비교
- sentence-transformer가 이미 로드되어 있어 추가 모델 불필요

### 주의

기존에 수집된 리뷰의 `review_categories_json`은 문자열 배열 형태로 저장되어 있다. 스키마 변경(문자열 → 객체) 이후 기존 데이터를 재수집하거나 마이그레이션 스크립트로 변환해야 한다.

---

## 3. 키워드 임베딩 사전 계산 최적화

현재 `category_filter()`는 리뷰마다 12개 카테고리 × 키워드 수만큼 `model.encode(keywords)`를 반복 실행한다. 다국어 전환으로 수집량이 최대 3,000건/게임으로 증가하면 이 연산이 가장 큰 병목이 된다.

```python
# 현재 — 리뷰마다 반복
def category_filter(text: str) -> FilterResult:
    for category, keywords in GAME_CATEGORIES.items():
        keyword_embs = model.encode(keywords, convert_to_tensor=True)  # ← 매번 재계산
        ...

# 변경 — 시작 시 1회 사전 계산
_KEYWORD_EMBEDDINGS: dict | None = None

def get_keyword_embeddings() -> dict:
    global _KEYWORD_EMBEDDINGS
    if _KEYWORD_EMBEDDINGS is None:
        model = get_embed_model()
        _KEYWORD_EMBEDDINGS = {
            category: model.encode(keywords, convert_to_tensor=True)
            for category, keywords in GAME_CATEGORIES.items()
        }
    return _KEYWORD_EMBEDDINGS

def category_filter(text: str) -> FilterResult:
    model = get_embed_model()
    keyword_embeddings = get_keyword_embeddings()
    review_emb = model.encode(text, convert_to_tensor=True)
    for category, keyword_embs in keyword_embeddings.items():
        ...
```

이 변경으로 cap 1,000 기준 예상 소요 시간이 **~7분 → ~1.5분** 수준으로 단축된다.

---

## 5. 임베딩 모델 교체 (항목 04)

현재 영어 전용 모델을 사용해 한국어/중국어 리뷰의 카테고리 분류 정확도가 떨어진다.

```python
# 현재 (영어 전용, 크롤러 카테고리 분류기 + ai-pipeline 평가 모듈에서 사용)
SentenceTransformer("all-MiniLM-L6-v2")

# 변경 (50개 언어 지원, 경량 118MB)
SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
```

크롤러와 `ai-pipeline/ai_module/evaluation/` 모듈 모두 같은 모델로 통일한다.

---

## 4. langdetect 오분류 버그 수정 (긴급)

### 현상

데모 테스트에서 `langdetect`가 영어 리뷰를 `ko`로 오분류하여 `language_filter`가 유효한 영어 리뷰를 탈락시키는 문제가 발생했다. `steam_crawler.py`와 `metacritic_crawler.py` 모두 동일한 구조로 영향을 받는다.

### 원인

- `langdetect`는 짧은 텍스트나 게이밍 용어(OP, GG, BGM 등)가 섞인 영어 리뷰를 `ko`로 오분류하는 구조적 문제가 있다.
- `DetectorFactory.seed = 0`은 재현성만 보장할 뿐 정확도를 개선하지 않는다.
- `parse_review()`에서 `"language": "en"`으로 이미 하드코딩되어 있어 탐지 결과 자체도 사용되지 않는다.

---

### steam_crawler.py 수정 방향

**Steam API 언어 파라미터 지원 확인 결과**: `language=english`, `language=korean`, `language=schinese` 등 29개 언어를 개별 지원한다. `language=all` 파라미터는 미지원이므로 언어별 별도 호출이 필요하다.

Steam API가 언어별로 리뷰를 분리하여 반환하므로 `langdetect`가 불필요하다. **API 파라미터를 신뢰하고 언어를 하드코딩**한다.

```python
# 언어별 독립 호출 (항목 03 다국어 지원과 통합)
LANGUAGES = ["english", "korean", "schinese"]
LANG_CODE_MAP = {"english": "en", "korean": "ko", "schinese": "zh"}

# language_filter 교체
def language_filter(text: str, api_language: str) -> FilterResult:
    lang = LANG_CODE_MAP.get(api_language, "en")
    return FilterResult(True, "lang", "pass", lang=lang)
```

각 언어 호출은 독립 파이프라인으로 실행하고 결과를 언어 코드와 함께 저장한다.

---

### metacritic_crawler.py 수정 방향

Metacritic은 영어 전용 플랫폼이다. 플랫폼 특성상 비영어 리뷰가 존재하지 않으므로 `langdetect` 자체가 불필요하다. `language_filter()`를 제거하고 `"en"`을 하드코딩한다.

```python
# language_filter() 함수 및 호출 제거
# run_filter_pipeline()에서 language_filter 단계 삭제
# parse_review()에서 "language": "en" 하드코딩 유지

def run_filter_pipeline(text: str) -> FilterResult:
    result = rule_based_filter(text)
    if not result.passed:
        return result
    # language_filter 제거 — Metacritic은 영어 전용
    result = category_filter(text)
    return result
```

`langdetect` import 및 `DetectorFactory.seed = 0` 라인도 함께 제거한다.

---

## 변경 범위

| 위치 | 변경 내용 | 난이도 |
|------|----------|--------|
| `steam_crawler.py` — `LANGUAGE` / `ALLOWED_LANGS` / `MAX_USER_REVIEWS` | 다국어 상수 변경 + cap 1,000 설정 | 낮음 |
| `steam_crawler.py` — `fetch_raw_reviews()` | 언어별 순차 호출로 변경 | 낮음 |
| `steam_crawler.py` — `category_filter()` + `get_keyword_embeddings()` | 키워드 임베딩 사전 계산 캐싱 | 낮음 |
| `steam_crawler.py` — `category_filter()` | 문장 단위 분리 + 감성 판단 로직 추가 | 중간 |
| `steam_crawler.py` — `parse_review()` | `review_categories` 반환 형태 변경 | 낮음 |
| `steam_crawler.py` — `get_embed_model()` | 모델명 교체 | 낮음 |
| `steam_crawler.py` — `language_filter()` | langdetect 제거, API 파라미터 신뢰 방식으로 교체 | 낮음 (긴급) |
| `metacritic_crawler.py` — `language_filter()` | 함수 및 호출 제거, langdetect import 삭제 | 낮음 (긴급) |
