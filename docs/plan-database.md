# 스프린트 기획 — Database

> 대상 파일: `backend/app/models/domain.py`, Alembic 마이그레이션

---

## 변경 사항 요약

| 테이블 | 변경 유형 | 관련 항목 |
|--------|---------|---------|
| `GameReviewSummary` | 컬럼 추가 5개 + 제거 1개 | 01, 03, 04 |
| `GameSummaryCursor` | PK 변경 | 03 |
| `ReviewSummaryJob` | 컬럼 추가 4개 | 07 |
| `ExternalReview` | `review_categories_json` 스키마 변경 | 02 |

---

## GameReviewSummary

### 추가 컬럼

**언어 파이프라인 구조 변경 (항목 03)**

```python
summary_type    = Column(String(16), nullable=False)
# "unified" | "regional"

review_language = Column(String(10), nullable=True)
# unified → NULL, regional → "en" / "ko" / "zh"
```

**요약 신뢰도 지표 (항목 01)**

```python
sentiment_alignment = Column(Numeric(5, 4), nullable=True)
# 1 - |sentiment_score - steam_recommend_ratio| / 100

coverage_ratio      = Column(Numeric(5, 4), nullable=True)
# source_review_count / total_reviews_in_db

staleness_ratio     = Column(Numeric(5, 4), nullable=True)
# new_reviews_since_last_summary / total_reviews_in_db
```

**임베딩 기반 품질 평가 (항목 04)**

```python
semantic_similarity_score = Column(Numeric(5, 4), nullable=True)
# 리뷰 임베딩 평균 vs 요약 임베딩 코사인 유사도 (0.0 ~ 1.0)
```

### 제거 컬럼

```python
# language_code 제거 (항목 03)
# 출력은 항상 한국어로 고정되므로 불필요
# review_language 컬럼으로 역할 분리
```

### UniqueConstraint 변경

```python
# 현재
UniqueConstraint('game_id', 'language_code', 'summary_version')

# 변경
UniqueConstraint('game_id', 'summary_type', 'review_language', 'summary_version')
```

---

## GameSummaryCursor

### PK 변경 (항목 03)

```python
# 현재
PrimaryKeyConstraint('game_id', 'language_code')

# 변경
PrimaryKeyConstraint('game_id', 'summary_type', 'review_language')
```

`review_language`는 unified 모드에서 NULL이 될 수 있으므로 PK 컬럼 nullable 처리 방식 확인 필요 (DB별 NULL PK 허용 여부).

---

## ReviewSummaryJob

### 추가 컬럼 (항목 07 — Gemini 출력 신뢰도)

```python
schema_compliance     = Column(Numeric(4, 3), nullable=True)
# Gemini 응답 필수 9개 항목 통과율 (0.000 ~ 1.000)

hallucination_score   = Column(Numeric(4, 3), nullable=True)
# representative_reviews 인용 review_id 실존 비율
# cited_ids 없으면 NULL

sentiment_consistency = Column(SmallInteger, nullable=True)
# sentiment_overall 레이블 vs sentiment_score 수치 일치 여부 (0 or 1)

anchor_deviation      = Column(Numeric(4, 3), nullable=True)
# |sentiment_score - steam_recommend_ratio| / 100
# steam_ratio 없으면 NULL
```

> 기존 토큰/캐시 컬럼 (`map_cache_hit`, `map_input_tokens` 등 7개)은 이미 스키마에 존재한다. 항목 05에서 값만 채운다 — 스키마 변경 불필요.

---

## ExternalReview

### review_categories_json 스키마 변경 (항목 02)

```python
# 현재: 문자열 배열
review_categories_json = ["그래픽", "조작감"]

# 변경: 카테고리+감성 객체 배열
review_categories_json = [
    {"category": "그래픽", "sentiment": "positive"},
    {"category": "조작감", "sentiment": "negative"}
]
```

컬럼 타입(`JSONB`)은 그대로 유지, 내부 구조만 변경. 기존 데이터는 항목 02 크롤러 재수집 시 덮어쓰거나 별도 마이그레이션 스크립트로 변환.

---

## Alembic 마이그레이션 순서

| 순서 | 파일명 | 대상 | 내용 | 선행 항목 |
|------|--------|------|------|---------|
| 1 | `m001_language_pipeline` | `GameReviewSummary`, `GameSummaryCursor` | `summary_type`, `review_language` 추가 / `language_code` 제거 / UniqueConstraint 변경 / 커서 PK 변경 | 03 |
| 2 | `m002_gemini_reliability` | `ReviewSummaryJob` | 신뢰도 컬럼 4개 추가 | 07 |
| 3 | `m003_summary_reliability` | `GameReviewSummary` | `sentiment_alignment`, `coverage_ratio`, `staleness_ratio` 추가 | 01 |
| 4 | `m004_semantic_similarity` | `GameReviewSummary` | `semantic_similarity_score` 추가 | 04 |

### m001 데이터 처리 방침

`language_code` 컬럼 제거 전 기존 행을 다음과 같이 변환한다.

```sql
UPDATE game_review_summary
SET summary_type = 'unified', review_language = NULL
WHERE summary_type IS NULL;

ALTER TABLE game_review_summary DROP COLUMN language_code;
```
