# 이슈 트래킹: Steam Histogram → AppReviews API 전환 제안

## 문서 목적

현재 이슈 트래킹 파이프라인은 Steam의 월별 히스토그램 API를 기반으로 감성 변곡점을 탐지하고, 별도로 Steam News API에서 원인 뉴스를 매칭하는 구조다. 본 문서는 이를 `/appreviews` API 기반으로 전환했을 때의 차이점, 기대 효과, 고려사항을 정리한다.

---

## 1. 현재 구조 (Histogram 기반)

### 흐름

```
Steam Histogram API (월별 집계)
  → detect_inflection_points() : ±20%p 변화 탐지
  → Steam News API : 변곡점 ±30일 이내 뉴스 매칭
  → GameEvent 저장 (원인 = 뉴스 제목)
  → EventSummary AI 요약 (현재 미구현)
```

### 한계

- **탐지 세밀도가 월 단위**: 이벤트 발생 날짜를 최대 한 달 오차로만 특정 가능
- **원인 파악이 간접적**: 리뷰 텍스트 없이 뉴스 제목으로만 원인을 추측하며, 매칭 실패 시 `unknown`으로 처리
- **AI 요약 연결 불가**: 변곡점 주변의 리뷰 텍스트가 없으므로 EventSummary 생성 경로가 없음
- **뉴스 매칭 실패율 존재**: 뉴스가 없거나 분류가 모호한 경우 이벤트 원인이 공백으로 남음

---

## 2. 전환 후 구조 (AppReviews 기반)

### 흐름

```
Steam AppReviews API (리뷰 단위, timestamp 포함)
  → 일별/주별로 집계 → 부정 리뷰 비율 계산
  → detect_inflection_points() : 동일 알고리즘, 더 세밀한 단위
  → 변곡점 주변 리뷰 텍스트 추출
  → AI 요약 파이프라인 투입 → EventSummary 생성
  → GameEvent + EventSummary 저장
```

### 달라지는 점

| 항목 | Histogram (현재) | AppReviews (전환 후) |
|---|---|---|
| 탐지 세밀도 | 월별 | 일별 / 주별 |
| 원인 파악 방식 | Steam News 별도 매칭 | 리뷰 텍스트 직접 분석 |
| AI 요약 연결 | 불가 (텍스트 없음) | 자연스럽게 연결 가능 |
| 데이터 수집 비용 | 1회 API 호출 | 리뷰 수에 비례한 페이지네이션 |
| 과거 데이터 가용 여부 | Steam이 이미 집계 | 리뷰 전량 수집 필요 |
| Steam News API 의존 | 있음 | 제거 가능 |

---

## 3. AppReviews API 스펙

**엔드포인트:**
```
GET https://store.steampowered.com/appreviews/{appid}?json=1
```

**주요 파라미터:**

| 파라미터 | 설명 |
|---|---|
| `filter` | `recent` / `updated` / `all` |
| `language` | `english`, `koreana`, `all` 등 |
| `num_per_page` | 1~100 |
| `cursor` | 페이지네이션 커서 (`*`로 시작) |
| `review_type` | `all` / `positive` / `negative` |
| `date_range_type` | `all` / `include` / `exclude` |
| `start_date`, `end_date` | Unix timestamp (date_range_type과 함께 사용) |

**리뷰 단위 주요 필드:**

| 필드 | 설명 |
|---|---|
| `timestamp_created` | 리뷰 작성 시각 (Unix timestamp) |
| `voted_up` | 긍정(true) / 부정(false) |
| `review` | 리뷰 텍스트 전문 |
| `author.playtime_at_review` | 리뷰 작성 시점 플레이타임 (분) |
| `weighted_vote_score` | Steam 가중 평점 |

---

## 4. 구현 시 고려사항

### 데이터 수집 비용
- CS2처럼 리뷰가 500만 개 이상인 게임은 전량 수집이 현실적으로 어려움
- **권장 전략:**
  - 초기 수집: 최근 2~3년치를 `start_date` / `end_date`로 필터링해서 가져옴
  - 이후 운영: 주기적으로 신규 리뷰만 증분 수집 (`filter=recent` + cursor 기반)

### 집계 단위 결정
- 일별 집계는 리뷰 수가 적은 게임에서 노이즈가 커질 수 있음
- 주별 집계가 노이즈와 세밀도의 균형에서 적합할 가능성이 높음
- 게임별 리뷰 볼륨에 따라 동적으로 단위를 선택하는 방식도 고려 가능

### 변곡점 탐지 알고리즘
- 현재 `detect_inflection_points(threshold=0.20, min_volume=20)` 로직은 그대로 재사용 가능
- 집계 단위가 달라지므로 `min_volume` 임계값 재조정이 필요할 수 있음

### AI 요약 파이프라인 연결
- 변곡점 날짜 기준 ±14일 이내 리뷰 텍스트를 바로 확보 가능
- 현재 미구현 상태인 `EventSummary` 생성 로직을 자연스럽게 채울 수 있음
- 기존 `map_reduce` 파이프라인 구조 재사용 가능

### Steam News API 의존성
- AppReviews 기반으로 전환하면 Steam News 매칭이 불필요해짐
- 단, 이벤트 유형 분류(`patch`, `dlc`, `controversy` 등)는 News API가 담당하고 있으므로,
  리뷰 텍스트 기반의 분류 로직으로 대체하거나 News API를 보조 참조로만 유지하는 방향 결정 필요

---

## 5. 영향 범위

| 파일 | 변경 필요 여부 | 내용 |
|---|---|---|
| `crawling/steam/histogram_crawler.py` | 대체 또는 병행 유지 | AppReviews 기반 집계 로직으로 대체 |
| `crawling/steam/news_crawler.py` | 선택적 유지 | 이벤트 유형 분류 보조용으로만 사용 가능 |
| `backend/app/services/event_service.py` | 수정 필요 | 크롤러 의존 변경, EventSummary 생성 로직 추가 |
| `backend/app/api/v1/events.py` | 변경 없음 | API 인터페이스는 동일하게 유지 가능 |
| `database/` | 변경 없음 | GameEvent, EventSummary 스키마 그대로 활용 |
| `ai-pipeline/` | 연결 추가 | EventSummary 생성 흐름 구현 |

---

## 6. Map 단계 모델 선택 (2026년 5월 기준)

### 벤치마크 결과 요약

4개 모델을 동일한 리뷰 청크(영어, 한국어, 혼합 각 1개)로 테스트한 결과:

| 모델 | 성공률 | 평균 속도 | 총 출력 토큰 | 비고 |
|---|---|---|---|---|
| `gemma3:1b` | 2/3 | 18.35s | 289 | 한국어 청크 포맷 실패 |
| `qwen2.5:1.5b` | **3/3** | **2.37s** | 311 | 현재 채택 모델 |
| `qwen3:1.7b` (thinking ON) | 3/3 | 10.95s | 3841 | thinking으로 인한 과도한 토큰 |
| `gemma3n:e2b` | 2/3 | 4.22s | 459 | 한국어를 영어로 변환 출력 |

`qwen3:1.7b`의 thinking OFF(`/no_think`) 테스트 결과:

| 모델 | 성공률 | 평균 속도 | 총 출력 토큰 |
|---|---|---|---|
| `qwen3:1.7b` (thinking ON) | 3/3 | 8.29s | 3653 |
| `qwen3:1.7b` (thinking OFF) | 3/3 | 7.02s | 2944 |

thinking OFF 시 속도 15% 개선에 불과하고, 여전히 `qwen2.5:1.5b`보다 6배 느림.

### 결론

**Map 단계는 `qwen2.5:1.5b` 유지.**

- CPU 전용 환경에서 속도와 포맷 안정성이 가장 우수
- `qwen3:1.7b`의 thinking 모드는 Map(단순 추출)에는 과분하며 속도 손해가 큼
- thinking이 유용한 태스크는 `EventSummary` 이슈 원인 분석처럼 깊은 추론이 필요한 단계에 별도 활용 고려

### Map 출력 언어 고정

Map 프롬프트에 출력 언어 지정이 없으면 모델이 입력 언어를 따라가서 한국어/영어가 혼재된 Reduce 입력이 생성된다. Reduce는 항상 한국어로 출력하도록 하드코딩되어 있으므로, Map 출력은 영어로 통일하는 것이 일관성에 유리하다.

적용 위치: `ai-pipeline/ai_module/map_reduce/map_local.py`

```python
prompt = (
    "Summarize this game review chunk using the following structure:\n"
    "Output in English regardless of the review language.\n"  # 추가
    "PROS: up to 4 bullet points ...\n"
    ...
)
```

---

## 7. 결론

AppReviews API 전환은 현재 구조의 두 가지 핵심 한계(월별 세밀도, AI 요약 미연결)를 동시에 해결한다. 다만 데이터 수집 비용이 증가하므로, 전량 수집이 아닌 기간 필터링 + 증분 수집 전략을 병행해야 한다. 기존 변곡점 탐지 알고리즘과 DB 스키마는 재사용 가능하므로 전환 범위는 크롤러와 서비스 레이어에 한정된다.
