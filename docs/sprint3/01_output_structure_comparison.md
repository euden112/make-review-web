# Sprint3 출력 구조 비교: 기획 vs 현재 데모

> 기준: `docs/plan-*.md` 기획 문서 vs `demo.py` 데모 + `steam_crawler.py` 크롤러 + `backend/app/api/v1/summaries.py` 응답

---

## 1. 크롤러 산출물 구조

### 기획 (plan-crawling.md)

크롤러는 **raw 수집 + 규칙 기반 필터만** 수행. NLP(임베딩 분류)는 백엔드로 이관.

```json
{
  "meta": {
    "game_id": "570",
    "platform_code": "steam",
    "schema_version": "1.0",
    "collected_at": "20260430T123000Z",
    "record_count": 120,
    "fetched_count": 500,
    "filtered_out_count": 380,
    "lang_policy": "en_ko_zh",
    "lang_breakdown": {
      "en": 80,
      "ko": 30,
      "zh": 10
    }
  },
  "reviews": [
    {
      "author_id": "76561198...",
      "is_recommended": true,
      "review_text_raw": "원문 전체 (절단 없음, 최대 8000자)",
      "review_text_clean": "전처리된 본문",
      "playtime_hours": 125.5,
      "date_posted": "2026-04-28",
      "language": "en",
      "filter_stage": "passed_rule",
      "rule_score": {
        "length_check": "pass",
        "spam_url": "pass",
        "repeated_chars": "pass"
      }
    }
  ]
}
```

**특징**:
- `review_categories_json`: **기획에서는 제거** (백엔드에서 계산)
- `review_text_raw`: 원문 전체 보존 (절단 제거)
- `filter_stage`: 1단계 규칙 필터만 수행
- 언어별 독립 수집 (en/ko/zh 각 최대 1000건)

### 현재 데모 (steam_crawler.py)

```json
{
  "meta": {
    "game_id": "271590",
    "platform_code": "steam",
    "schema_version": "1.0",
    "collected_at": "20260430T103000Z",
    "record_count": 42,
    "total_positive": 1234,
    "total_negative": 456,
    "crawled_at": "2026-04-30T10:30:00",
    "lang_policy": "ko_en_zh",
    "lang_breakdown": {
      "en": 42
    }
  },
  "reviews": [
    {
      "author_id": "76561198...",
      "is_recommended": true,
      "review_text": "절단된 본문 (최대 500자)",
      "playtime_hours": 125.5,
      "date_posted": "2026-04-28",
      "language": "en",
      "review_categories": ["그래픽", "조작감"]
    }
  ]
}
```

**차이점**:
- `review_text`: 절단됨 (최대 500자) ← **기획에서 제거 예정**
- `review_categories`: 이미 임베딩 분류됨 ← **기획에서는 백엔드로 이관**
- 한국어/중국어: 수집 안 함 (ALLOWED_LANGS = ["en"]) ← **기획에서는 3언어 지원**
- `review_text_raw` 필드: 없음

---

## 2. 백엔드 요약 응답 구조

### 기획 (plan-backend.md)

**Unified 요약** (전체 리뷰 기준):

```json
{
  "game_id": 1,
  "summary_type": "unified",
  "review_language": null,
  "language_code": "unified",
  "version": 1,
  "summary_text": "**Grand Theft Auto V는 오픈월드 게임의 정점입니다.**\n\n스케일, 자유도, 그래픽이 압도적으로 우수하며 스토리도 몰입감 있습니다. 다만 최적화 문제와 일부 버그는 아쉬운 부분입니다. 장시간 플레이를 즐기는 플레이어에게 강력하게 추천합니다.",
  "pros": [
    "거대한 오픈월드 환경",
    "뛰어난 그래픽 표현",
    "풍부한 스토리라인"
  ],
  "cons": [
    "높은 시스템 요구사항",
    "가끔 발생하는 버그",
    "높은 가격대"
  ],
  "keywords": ["오픈월드", "액션", "스토리", "그래픽", "자유도", "멀티플레이"],
  "representative_reviews": [
    {
      "source": "steam",
      "review_id": 12345,
      "quote": "This game is absolutely amazing. The graphics are stunning and the gameplay is incredibly smooth.",
      "reason": "우수한 그래픽과 조작감을 대표하는 평가"
    }
  ],
  "sentiment_overall": "positive",
  "sentiment_score": 88.5,
  "aspect_sentiment": {
    "graphics": { "label": "우수함", "score": 9.2 },
    "controls": { "label": "우수함", "score": 8.5 },
    "optimization": { "label": "보통", "score": 6.3 },
    "content": { "label": "우수함", "score": 8.8 },
    "price_value": { "label": "보통", "score": 6.5 }
  },
  "sentiment_alignment": 0.8850,
  "coverage_ratio": 0.2100,
  "staleness_ratio": 0.0500,
  "semantic_similarity_score": 0.7234,
  "updated_at": "2026-04-30T10:30:00"
}
```

**Regional 요약** (언어별):

```json
{
  "game_id": 1,
  "summary_type": "regional",
  "review_language": "en",
  "language_code": "en",
  "version": 1,
  "summary_text": "GTA V is a masterpiece of open-world design. Players praise exceptional graphics and freedom, though optimization concerns and bugs remain.",
  "pros": ["Vast world", "Great graphics"],
  "cons": ["High system demands"],
  "keywords": ["open-world", "action", "graphics"],
  "representative_reviews": [...],
  "sentiment_overall": "positive",
  "sentiment_score": 86.0,
  "aspect_sentiment": {...},
  "updated_at": "2026-04-30T10:30:00"
}
```

**신뢰도 지표**:
- `sentiment_alignment`: 1 - |sentiment_score - steam_ratio| / 100
- `coverage_ratio`: 요약한 리뷰 수 / 총 리뷰 수
- `staleness_ratio`: 신규 리뷰 수 / 총 리뷰 수
- `semantic_similarity_score`: 리뷰 평균 임베딩 vs 요약 임베딩

### 현재 데모 (demo.py 렌더링)

```json
{
  "game_id": 1,
  "summary_type": "unified",
  "review_language": null,
  "language_code": "unified",
  "version": 1,
  "summary_text": "**Grand Theft Auto V is a masterpiece...**\n\n...",
  "pros": [list],
  "cons": [list],
  "keywords": [list],
  "representative_reviews": [list],
  "sentiment_overall": "positive",
  "sentiment_score": 88.5,
  "aspect_sentiment": {...},
  "updated_at": "2026-04-30T10:30:00"
}
```

**데모에서 렌더링하는 필드**:
```python
summary_text: str = data.get("summary_text") or ""
aspects: dict = data.get("aspect_sentiment") or {}
pros: list = data.get("pros") or []
cons: list = data.get("cons") or []
keywords: list = data.get("keywords") or []
rep_reviews: list = data.get("representative_reviews") or []
```

**렌더링하지 않는 필드** (응답에 있지만 무시됨):
- `sentiment_alignment` ✗
- `coverage_ratio` ✗
- `staleness_ratio` ✗
- `semantic_similarity_score` ✗
- `summary_type` ✗
- `review_language` ✗
- `version` ✗
- `sentiment_overall` ✗
- `sentiment_score` ✗

---

## 3. 데모 콘솔 출력 구조

```
▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
  GRAND THEFT AUTO V
▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓

  Grand Theft Auto V는 오픈월드 게임의 정점입니다.

  스케일, 자유도, 그래픽이 압도적으로 우수하며 스토리도
  몰입감 있습니다. 다만 최적화 문제와 일부 버그는 아쉬운
  부분입니다.

  항목별 평점
  그래픽         ██████████░░░░░░░░    9.2  우수함
  조작감         █████████░░░░░░░░░░    8.5  우수함
  최적화         ██████░░░░░░░░░░░░░░    6.3  보통
  콘텐츠 양      ████████░░░░░░░░░░░░    8.8  우수함
  가성비         ██████░░░░░░░░░░░░░░    6.5  보통

  장점
    + 거대한 오픈월드 환경
    + 뛰어난 그래픽 표현
    + 풍부한 스토리라인

  단점
    − 높은 시스템 요구사항
    − 가끔 발생하는 버그
    − 높은 가격대

  근거 리뷰
  [steam] This game is absolutely amazing. The graphics are stunning...
    → 우수한 그래픽과 조작감을 대표하는 평가

  #오픈월드  #액션  #스토리  #그래픽  #자유도  #멀티플레이

──────────────────────────────────────────────────────────────────
```

**렌더링 구성 요소**:
1. 헤더: 게임명
2. 한 줄 요약 (`summary_text` 첫 줄)
3. 본문 (`summary_text` 2줄 이후)
4. 항목별 점수 바 (`aspect_sentiment`)
5. 장단점 (`pros`, `cons`)
6. 근거 리뷰 (`representative_reviews`)
7. 키워드 (`keywords`)

---

## 4. 핵심 차이점 요약

| 항목 | 기획 기준 | 현재 데모 | 상태 |
|------|---------|---------|------|
| **크롤러 NLP** | 백엔드로 이관 (raw만 수집) | 크롤러에서 임베딩 분류 수행 | ❌ 미완 |
| **원문 보존** | 절단 제거 (최대 8000자) | 절단됨 (최대 500자) | ❌ 미완 |
| **다국어 수집** | 한/영/중 3언어 | 영어만 수집 | ❌ 미완 |
| **카테고리 필드** | {category, sentiment} 객체 배열 | ["문자열"] 배열 | ❌ 미완 |
| **요약 응답** | unified + regional 분리 | unified만 렌더링 | ✓ 부분 |
| **신뢰도 지표** | 4개 필드 추가 (alignment, coverage, staleness, similarity) | 계산/저장 안 함 | ❌ 미완 |
| **콘솔 렌더링** | 요약 데이터를 예쁜 카드로 표시 | 기획 기준 구현됨 | ✓ 완료 |

---

## 5. 다음 단계

### 우선순위 높음 (영향도 큼)
1. **크롤러 리팩토링**: raw 수집만, NLP 제거 → plan-crawling.md § 2
2. **원문 보존**: 절단 제거 → plan-crawling.md § 4-1
3. **다국어 지원**: 한/영/중 독립 수집 → plan-crawling.md § 1
4. **백엔드 캐시**: Redis 연결 → plan-backend.md § 2
5. **신뢰도 지표**: 계산 및 저장 → plan-backend.md § 5~7

### 우선순위 중간 (안정화)
6. 카테고리 스키마 변경 (문자열 → 객체)
7. 파이프라인 분기 (unified/regional)
8. 데이터 마이그레이션 스크립트

### 우선순위 낮음 (보완)
9. 콘솔 렌더링 개선 (이미 주요 기능 완료)
10. regional 요약 렌더링 추가
