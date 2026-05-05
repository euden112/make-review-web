# 플레이타임별 여론 및 비평가 반응 분석 기획서

## 1. 배경 및 문제 정의

### 기존 Regional Pipeline 제거 배경

언어권별(한국어/영어/중국어) 여론 비교 파이프라인을 다음 이유로 폐기한다.

- **정보 가치 낮음**: 게임 구매자가 언어권별 여론을 별도로 확인하는 수요가 적음
- **비용 비효율**: 동일 게임에 대해 Reduce API를 3회 중복 호출
- **기술적으로도 불필요**: Map 단계에서 전 언어를 영어로 번역하므로 Reduce는 언어와 무관하게 동일한 입력을 받음 — 분리 호출의 실익 없음

### 새로운 방향

Regional Pipeline이 차지하던 자리를 게이머에게 실질적으로 유용한 두 가지 분석으로 대체한다.

> **"이 게임, 초반만 버티면 재밌어지나?"**
> **"출시 당시 전문가들은 이 게임을 어떻게 봤나?"**

---

## 2. 핵심 가치

### 2-1. 플레이타임별 여론

| 기존 unified summary | 플레이타임별 여론 |
|---------------------|-----------------|
| "전반적으로 좋은 게임" | "초반은 혹평, 30시간 이후 호평 전환" |
| 전체 평균 감성 | 진행 단계별 감성 변화 |
| 정적 요약 | "버텨야 하는 구간" 맥락 제공 |

게이머가 플레이 도중 "계속해야 하나?"를 고민할 때 직접적인 답을 제공한다.
장르·게임 유형 무관하게 거의 모든 게임에 적용 가능하다.

### 2-2. 비평가 반응 (독립 섹션)

unified summary는 Steam 유저 리뷰 중심(~91%)으로 구성된다.
비평가 반응은 유저 여론과 비교하는 것이 아닌 **독립된 정보**로 제공한다.

| 제공 정보 | 의미 |
|---------|------|
| 비평가 요약 · pros · cons | 출시 당시 전문가 시각 |
| 비평가 리뷰 수 | 신뢰도 판단 근거 |

비평가 리뷰는 출시 시점에 작성되고 이후 업데이트되지 않는다.
유저 여론(전 기간 누적)과 직접 비교하지 않으며, "출시 당시 전문가 평가"로 명확히 표현한다.

---

## 3. 사용 데이터 소스

### 3-1. 기존 ExternalReview DB

| 컬럼 | 용도 | 현재 상태 |
|------|------|----------|
| `playtime_hours` | 플레이타임별 버킷 분류 | **수정 필요** (`playtime_forever` → `playtime_at_review`) |
| `review_type` | critic / user 구분 | 정상 수집 중 |
| `review_text_clean` | Map 입력 | 정상 수집 중 |
| `normalized_score_100` | 감성 점수 보조 | 정상 수집 중 |
| `platform_id` | Metacritic=critic, Steam=user 매핑 | 정상 수집 중 |

### 3-2. 플랫폼-리뷰 유형 매핑

```
Metacritic critic → 비평가 반응 섹션
Metacritic user   → unified summary 포함
Steam user        → unified summary + 플레이타임 분석
```

---

## 4. 선행 작업: 크롤러 수정

### 4-1. playtime 필드 교체

`playtime_hours`가 현재 `playtime_forever`(누적 플레이타임)를 저장하고 있어
리뷰 작성 시점의 진행도를 반영하지 못한다.

**[steam_crawler.py:303](../crawling/steam/steam_crawler.py) 수정:**

```python
# 수정 전
"playtime_hours": round(author_info.get("playtime_forever", 0) / 60, 1),

# 수정 후
"playtime_hours": round(author_info.get("playtime_at_review", 0) / 60, 1),
```

### 4-2. Metacritic 비평가 수집 상한 조정

**[metacritic_crawler.py:32](../crawling/metacritic/metacritic_crawler.py) 수정:**

```python
# 수정 전
MAX_CRITIC_REVIEWS = 50

# 수정 후
MAX_CRITIC_REVIEWS = 100
```

게임마다 실제 비평가 리뷰 수가 다르므로 상한을 올려도 수집량은 게임별로 다르다.
대형 AAA 기준 80~100건, 중소 타이틀은 20~40건, 인디는 10건 미만.

### 4-3. Steam 수집 전략 변경

**언어별 루프 제거**: 목표가 전반적 여론 요약이므로 언어 균등 샘플링 불필요.
`language=all`로 단일 호출한다.

**초기 수집과 증분 수집을 분리한다.**

헬프풀 정렬(`filter=all`) Pool은 새 리뷰가 헬프풀 점수를 쌓기 전까지 상위에 오르지 않으므로
증분 실행 시 기존 리뷰가 중복 수집되고 새 리뷰는 포착되지 않는다.
최신 정렬(`filter=recent`) Pool만 증분 수집에 유효하다.

#### 초기 수집 (게임 최초 등록 시)

역대 검증된 헬프풀 리뷰와 최신 리뷰를 함께 확보한다.
Steam API 첫 응답의 `query_summary`에 포함된 실제 긍/부정 비율로
Pool 1, 2 크기를 조정해 과대표집을 방지한다.

```python
# API 첫 호출에서 summary 수신
summary = fetch_first_page(appid)
pos_ratio = summary["total_positive"] / summary["total_reviews"]
neg_ratio = 1 - pos_ratio

helpful_budget = MAX_REVIEWS * 2 // 3  # 전체의 2/3을 헬프풀로
recent_budget  = MAX_REVIEWS - helpful_budget

# Pool 1: 헬프풀 긍정 → 실제 긍정 비율 반영, 중/후반 버킷 커버
fetch_reviews(appid, filter="all", review_type="positive",
              count=int(helpful_budget * pos_ratio))

# Pool 2: 헬프풀 부정 → 실제 부정 비율 반영, 초반 이탈자 + 번아웃 커버
fetch_reviews(appid, filter="all", review_type="negative",
              count=int(helpful_budget * neg_ratio))

# Pool 3: 최신 전체 → 시간적 다양성, 초반 버킷 추가 보완
fetch_reviews(appid, filter="recent", review_type="all",
              count=recent_budget)
```

**80% 긍정 게임 예시 (MAX_REVIEWS=1000):**

```
helpful_budget = 667
Pool 1 (긍정 헬프풀): 667 × 0.80 = 534건
Pool 2 (부정 헬프풀): 667 × 0.20 = 133건
Pool 3 (최신 전체):              = 333건
```

| Pool | 기여: 통합 요약 | 기여: 플레이타임 분석 |
|------|--------------|-------------------|
| 1 (헬프풀 긍정) | 실제 비율 반영, 품질 높은 긍정 | 중/후반 버킷 |
| 2 (헬프풀 부정) | 실제 비율 반영, 품질 높은 부정 | 초반 + 후반 버킷 |
| 3 (최신 전체) | 최신성 보완 | 초반 버킷 추가 보완 |

#### 증분 수집 (정기 스케줄)

Pool 3만 실행한다. 새 리뷰는 최신순 상위에 위치하므로 효율적으로 포착된다.
기존 deduplication 로직이 이미 수집된 리뷰를 자동 제외한다.

```python
# 증분: Pool 3만 실행 (API 호출 1회)
fetch_reviews(appid, filter="recent", review_type="all",
              count=MAX_REVIEWS // 3)
```

| | 초기 수집 | 증분 수집 |
|--|---------|---------|
| Pool 구성 | Pool 1 + 2 + 3 | Pool 3만 |
| API 호출 수 | 3회 | 1회 |
| 새 리뷰 포착 | 전체 커버리지 확보 | 최신 리뷰만 |

**참고**: `filter_offtopic_activity`는 Steam API 기본값(1)을 유지한다.
리뷰 폭탄이 API 레벨에서 자동 제외되므로 별도 필터 불필요.

수정 후 Steam 리뷰 전체 재수집이 필요하다.
Metacritic은 비평가 상한 조정 후 재수집한다.

---

## 5. 시스템 구조

Map은 1회만 실행하고, 그 출력물을 그룹핑해서 Reduce에 넘긴다.
Reduce도 1회 호출로 세 가지 분석을 동시에 생성한다.

```
ExternalReview DB
       │
       ▼
  sampler.py  ← 샘플링 시점에 태그 부착
  각 리뷰에:
    playtime_bucket: early / mid / late / unknown  (Steam 리뷰만)
    reviewer_type:   critic / user
       │
       ▼
  Map 파이프라인  ← 1회 실행 (기존과 동일)
  리뷰별 구조화 출력 생성
       │
       ▼
  [Map 출력물 그룹핑]  ← 신규, API 호출 없음
  {
    all:    [전체 리뷰],           → unified summary용
    early:  [playtime=early],      → 플레이타임 분석용
    mid:    [playtime=mid],
    late:   [playtime=late],
    critic: [reviewer_type=critic], → 비평가 반응용
  }
       │
       ▼
  Reduce 1회 호출  ← 그룹핑된 Map 출력물 전달
  출력: unified + playtime + critic 통합 JSON
       │
       ▼
  DB 저장 → API 제공
```

**Reduce 호출 횟수 비교:**

| 방식 | Map 실행 | Reduce 호출 |
|------|---------|------------|
| Regional Pipeline (구) | 1회 | 3회 (언어별) |
| 현재 unified only | 1회 | 1회 |
| 신규 (그룹핑 재활용) | 1회 | 1회 |

---

## 6. 플레이타임 버킷 계산

게임마다 평균 플레이타임이 다르므로 고정 시간 기준이 아닌
**게임별 리뷰어 플레이타임 분포의 퍼센타일**을 기준으로 한다.

```python
def compute_playtime_buckets(game_id: int, session) -> dict:
    playtimes = session.execute(
        select(ExternalReview.playtime_hours)
        .where(
            ExternalReview.game_id == game_id,
            ExternalReview.playtime_hours.isnot(None),
            ExternalReview.playtime_hours > 0,
            ExternalReview.platform_id == STEAM_PLATFORM_ID,
        )
    ).scalars().all()

    if len(playtimes) < 30:
        return None  # 데이터 부족

    p33 = percentile(playtimes, 33)
    p66 = percentile(playtimes, 66)

    return {
        "early_max": p33,   # 0 ~ p33
        "mid_max":   p66,   # p33 ~ p66
        # late: p66+
    }
```

**버킷 분류 예시:**

| 게임 | early | mid | late |
|------|-------|-----|------|
| GTA V | 0 ~ 25h | 25 ~ 120h | 120h+ |
| 엘든링 | 0 ~ 40h | 40 ~ 90h | 90h+ |
| 6시간 인디 | 0 ~ 2h | 2 ~ 4h | 4h+ |

`playtime_hours`가 null인 리뷰는 `unknown`으로 분류하며
플레이타임 분석에서 제외하고 unified summary에만 포함한다.

`playtime_hours=0` 리뷰는 임계값 계산(`playtime_hours > 0` 조건으로 제외)에서는 빠지지만,
태그 부착 시 early 버킷(0 ≤ p33)으로 분류한다.

---

## 7. 최소 데이터 게이트

```python
MIN_REVIEWS_PER_BUCKET = 30  # 플레이타임 버킷별
MIN_CRITIC_REVIEWS     = 10  # 비평가 섹션 생성 최소 기준
```

| 조건 | 처리 |
|------|------|
| 버킷 리뷰 ≥ 30 | 해당 버킷 요약 생성 |
| 버킷 리뷰 < 30 | `null` 반환, 프론트에서 "데이터 부족 (N건)" 표시 |
| critic 리뷰 ≥ 10 | 비평가 반응 섹션 생성 |
| critic 리뷰 < 10 | 비평가 반응 미생성 |

---

## 8. DB 스키마 추가

### PlaytimeAnalysis 테이블

```sql
CREATE TABLE playtime_analyses (
    id                  SERIAL PRIMARY KEY,
    game_id             INTEGER REFERENCES games(id),
    bucket_thresholds   JSONB NOT NULL,
    -- {"early_max": 25.0, "mid_max": 120.0}

    early_summary       TEXT,
    early_sentiment     VARCHAR(16),
    early_score         FLOAT,
    early_pros          JSONB,
    early_cons          JSONB,
    early_keywords      JSONB,
    early_review_count  INTEGER,

    mid_summary         TEXT,
    mid_sentiment       VARCHAR(16),
    mid_score           FLOAT,
    mid_pros            JSONB,
    mid_cons            JSONB,
    mid_keywords        JSONB,
    mid_review_count    INTEGER,

    late_summary        TEXT,
    late_sentiment      VARCHAR(16),
    late_score          FLOAT,
    late_pros           JSONB,
    late_cons           JSONB,
    late_keywords       JSONB,
    late_review_count   INTEGER,

    created_at          TIMESTAMP DEFAULT now(),
    updated_at          TIMESTAMP DEFAULT now(),
    UNIQUE (game_id)
);
```

### CriticSummary 테이블

```sql
CREATE TABLE critic_summaries (
    id              SERIAL PRIMARY KEY,
    game_id         INTEGER REFERENCES games(id),

    summary         TEXT,
    sentiment       VARCHAR(16),
    score           FLOAT,
    pros            JSONB,
    cons            JSONB,
    keywords        JSONB,
    review_count    INTEGER,

    created_at      TIMESTAMP DEFAULT now(),
    updated_at      TIMESTAMP DEFAULT now(),
    UNIQUE (game_id)
);
```

---

## 9. Reduce 프롬프트 설계 방향

Map 출력물을 그룹별로 묶어 단일 Reduce 호출에 전달한다.

### 입력 구조

```json
{
  "all":    [ ...전체 Map 출력 리뷰... ],
  "early":  [ ...playtime_bucket=early 리뷰... ],
  "mid":    [ ...playtime_bucket=mid 리뷰... ],
  "late":   [ ...playtime_bucket=late 리뷰... ],
  "critic": [ ...reviewer_type=critic 리뷰... ]
}
```

그룹에 리뷰가 없거나 최소 수 미달이면 해당 키를 빈 배열로 전달하고,
Reduce는 해당 세그먼트를 `null`로 반환한다.

> **토큰 중복 주의**: `all`은 early/mid/late/critic의 상위 집합이므로 각 리뷰는 `all`에 한 번, 해당 그룹에 한 번 중복 포함된다. Reduce 1회 호출 유지로 Regional Pipeline 대비 전체 비용이 낮으며, 이 중복은 허용된 트레이드오프다.

### 출력 스키마

```json
{
  "unified": {
    "summary", "sentiment_overall", "sentiment_score", "pros", "cons", "keywords"
  },
  "playtime": {
    "early":  { "summary", "sentiment_overall", "sentiment_score", "pros", "cons", "keywords" } | null,
    "mid":    { ... } | null,
    "late":   { ... } | null
  },
  "critic": {
    "summary", "sentiment_overall", "sentiment_score", "pros", "cons", "keywords"
  } | null
}
```

### 프롬프트 지시사항

- `unified`는 전체 리뷰(`all`) 기반으로 작성
- `playtime` 각 버킷은 독립적으로 요약하되, 버킷 간 여론 추이를 `summary`에 자연스럽게 언급
- `critic`은 비평가 리뷰만 보고 독립적으로 요약 — 유저 여론과 비교하거나 차이를 언급하지 않을 것
- 데이터 없는 세그먼트는 `null` 반환

---

## 10. API 명세

### 플레이타임별 여론

```
GET /api/v1/games/{game_id}/playtime-analysis

Response:
{
  "game_id": 123,
  "bucket_thresholds": { "early_max": 25.0, "mid_max": 120.0 },
  "buckets": {
    "early": {
      "label": "초반 (~25시간)",
      "sentiment_overall": "negative",
      "sentiment_score": 42.1,
      "pros": ["전투 메커니즘"],
      "cons": ["초반 튜토리얼 길이", "어려운 난이도 곡선"],
      "summary": "초반 25시간은 학습 곡선이 가파르다는 혹평이 많으나...",
      "review_count": 143
    },
    "mid": { ... },
    "late": {
      "sentiment_overall": "positive",
      "sentiment_score": 81.4,
      ...
    }
  }
}
```

> `label` 필드(`"초반 (~25시간)"` 등)는 DB 저장 값이 아니며, `bucket_thresholds.early_max`를 포맷해 서비스 레이어에서 동적으로 생성한다.

### 비평가 반응

```
GET /api/v1/games/{game_id}/critic-summary

Response:
{
  "game_id": 123,
  "review_count": 84,
  "sentiment_overall": "positive",
  "sentiment_score": 79.3,
  "pros": ["연출", "스토리", "세계관"],
  "cons": ["반복적인 전투", "최적화"],
  "summary": "출시 당시 비평가들은 뛰어난 연출과 스토리를 높이 평가했으나..."
}
```

---

## 11. 프론트엔드 표현 원칙

- 데이터 부족 시 섹션을 숨기지 않고 "리뷰 수 부족 (N건)" 문구로 표시 — 플레이타임 버킷, 비평가 섹션 모두 동일하게 적용
- 비평가 반응 섹션에 "출시 당시 전문가 N명 기준" 명시 — 유저 여론(전 기간)과 시점이 다름을 사용자가 인지하게 함
- `bucket_thresholds`를 UI에 노출해 "초반 기준이 25시간"임을 사용자가 확인할 수 있게 함
- 비평가와 유저 여론의 수치 비교 UI 제공하지 않음 — 각각 독립 섹션으로만 표시

---

## 12. 기존 시스템과의 관계

```
unified summary   →  게임 전체 종합 평가 (유지, 변경 없음)
playtime analysis →  진행 단계별 여론 변화 (신규)
critic summary    →  출시 당시 비평가 반응 (신규)
regional summary  →  언어권별 여론 비교 (제거)
```

---

## 13. 구현 순서

| 순서 | 대상 | 작업 |
|------|------|------|
| 1 | `crawling/steam/steam_crawler.py` | `playtime_at_review` 교체, 언어 루프 제거, 3-pool 전략 적용 |
| 2 | `crawling/metacritic/metacritic_crawler.py` | `MAX_CRITIC_REVIEWS = 100` 조정 |
| 3 | Steam + Metacritic 재수집 | 수정된 크롤러로 전체 재수집 |
| 4 | `database/08_migration_sprint4.sql` | `playtime_analyses`, `critic_summaries` 테이블 생성 |
| 5 | `backend/app/models/domain.py` | `PlaytimeAnalysis`, `CriticSummary` 모델 추가 |
| 6 | `ai-pipeline/ai_module/map_reduce/sampler.py` | `compute_playtime_buckets()` 호출 후 결과를 각 리뷰에 `playtime_bucket` 태그로 부착 |
| 7 | `ai-pipeline/ai_module/map_reduce/reduce_api.py` | 통합 Reduce 프롬프트 및 출력 스키마 수정 |
| 8 | `backend/app/services/ai_service.py` | 파이프라인 오케스트레이션 통합 |
| 9 | `backend/app/api/v1/analysis.py` | 두 엔드포인트 구현 |
| 10 | `frontend/` | 플레이타임 타임라인 + 비평가 반응 컴포넌트 |

### 실행 전략

기존 unified summary 스케줄링 파이프라인에 포함시킨다.

```
기존 스케줄 실행 흐름:
  Map 파이프라인 실행 (1회)
  Map 출력물 그룹핑 (메모리, API 호출 없음)
    → Reduce 1회: unified + playtime + critic 통합 출력
    → DB 저장
```

별도 on-demand 트리거 없이 사용자는 항상 즉시 결과를 조회할 수 있다.
