# 플레이타임 기반 리뷰 품질 개선

## 담당 역할

AI 요약 파이프라인에서 **플레이타임(playtime) 데이터를 활용한 리뷰 품질 점수 개선** 및 **리뷰 샘플링 로직 고도화**를 담당했습니다.

---

## 관련 파일

- `ai-pipeline/ai_module/map_reduce/sampler.py`

---

## 기존 문제점

기존 `quality_score` 함수는 플레이타임에 고정 가중치(`1.8`)를 적용했습니다.

```python
# 기존 코드
def quality_score(row: ReviewRow) -> float:
    playtime = float(row.playtime_hours or 0.0)
    helpful = float(row.helpful_count or 0)
    return (1.8 * (playtime + 1.0) ** 0.5) + (1.2 * (helpful + 1.0) ** 0.5)
```

**문제점:**
- 플레이타임 1시간과 1000시간에 동일한 가중치 공식 적용
- 비정상적으로 높은 플레이타임(예: 10,000시간)에 대한 이상치 처리 없음
- 게임마다 플레이타임 분포가 다름에도 고정값으로만 판단
- 플레이타임이 없는 리뷰(Metacritic 등)와의 불균형

---

## 개선 내용

### 1. 이상치 처리 (Outlier Capping)

플레이타임이 비정상적으로 높은 경우(500시간 초과)를 캡핑 처리합니다.

```python
playtime = min(playtime, 500.0)
```

**이유:** 수천 시간을 플레이한 리뷰어는 극소수이며, 이들의 리뷰가 과도하게 높은 점수를 받아 샘플링에서 편향이 생기는 것을 방지합니다.

---

### 2. 퍼센타일 기반 동적 버킷 계산 (`compute_playtime_buckets`)

게임마다 플레이타임 분포가 다르기 때문에 고정 구간 대신 **p33/p66 퍼센타일**로 버킷 경계를 동적으로 계산합니다.

```python
def compute_playtime_buckets(rows: Sequence[ReviewRow]) -> PlaytimeBuckets | None:
    # Steam 리뷰의 플레이타임 데이터로 p33, p66 계산
    early_max = round(pct(33), 1)  # 하위 33% 경계
    mid_max = round(pct(66), 1)    # 하위 66% 경계
    return PlaytimeBuckets(early_max=early_max, mid_max=mid_max)
```

| 버킷 | 의미 | 예시 |
|------|------|------|
| `early` | 초반 플레이어 (하위 33%) | 입문 단계 리뷰 |
| `mid` | 중반 플레이어 (33~66%) | 핵심 게임플레이 경험 |
| `late` | 장기 플레이어 (상위 33%) | 심층 분석 리뷰 |
| `unknown` | 플레이타임 없음 | Metacritic 등 |

---

### 3. 버킷 기반 가중치 차등 적용 (`quality_score`)

버킷 태그를 기반으로 가중치를 다르게 적용합니다.

```python
def quality_score(row: ReviewRow) -> float:
    playtime = float(row.playtime_hours or 0.0)
    helpful = float(row.helpful_count or 0)

    # 이상치 처리
    playtime = min(playtime, 500.0)

    # playtime_bucket 태그 기반 가중치
    bucket = row.playtime_bucket
    if bucket == "early":
        playtime_score = 0.4 * (playtime + 1.0) ** 0.5
    elif bucket == "mid":
        playtime_score = 0.7 * (playtime + 1.0) ** 0.5
    elif bucket == "late":
        playtime_score = 0.5 * (playtime + 1.0) ** 0.5
    else:  # unknown
        playtime_score = 0.3 * (playtime + 1.0) ** 0.5

    return playtime_score + (1.2 * (helpful + 1.0) ** 0.5)
```

**mid 버킷에 가장 높은 가중치를 준 이유:**
- 너무 짧게 플레이한 early는 게임 전체를 파악하지 못할 가능성이 높음
- 너무 오래 플레이한 late는 일반 유저와 관점이 달라질 수 있음
- mid 구간이 가장 균형 잡힌 게임 경험을 반영한다고 판단

---

### 4. 리뷰 태깅 및 버킷별 균형 샘플링

`tag_reviews()`로 각 리뷰에 버킷 태그를 부착하고, `stratified_select_reviews()` 에서 버킷별로 균형 있게 리뷰를 선택합니다.

```python
# 버킷 계산 후 태그 부착
buckets = compute_playtime_buckets(filtered_rows)
filtered_rows = tag_reviews(filtered_rows, buckets)

# 버킷별로 나눠서 균형 있게 선택
for bucket in ["early", "mid", "late"]:
    rows_in = [r for r in steam_buckets_map[bucket] if r.is_recommended is True]
    steam_pos.extend(sorted(rows_in, key=quality_score, reverse=True)[:target])
```

---

## 차별점 요약

| 항목 | 기존 | 개선 후 |
|------|------|---------|
| 가중치 방식 | 고정 가중치 | 퍼센타일 기반 동적 버킷 |
| 이상치 처리 | 없음 | 500시간 캡핑 |
| 버킷 경계 | 없음 | 게임별 p33/p66 동적 계산 |
| 샘플링 | 점수 순 정렬 | 버킷별 균형 샘플링 |
| 플레이타임 없는 리뷰 | 0으로 처리 | unknown 버킷으로 별도 처리 |

---

## 한계 및 향후 개선 방향

- 현재 `MIN_REVIEWS_PER_BUCKET = 30` 기준 미만이면 버킷 계산 없이 기존 방식으로 폴백
- 플레이타임 데이터가 없는 Metacritic 리뷰는 `unknown` 버킷으로만 처리됨
- 향후 게임 장르별로 다른 버킷 기준을 적용하는 방향으로 개선 가능
