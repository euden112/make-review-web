# 게임 이슈 트래킹 기획서

## 1. 배경 및 문제 정의

### 기존 방향의 한계
초기 기획에서는 언어권별(한국어/영어/중국어) 여론을 비교하는 Regional Pipeline을 구현했다.
그러나 다음 이유로 폐기한다.

- **정보 가치 낮음**: 게임 구매자가 "한국 유저 의견"을 별도로 확인하는 수요가 적음
- **비용 비효율**: 동일 게임에 대해 AI 파이프라인을 3번 중복 실행
- **Map 단계에서 번역 처리 완료**: Reduce 단계는 언어와 무관하게 영어 텍스트만 입력받으므로 언어별 분리의 실익 없음

### 새로운 방향
> "이 게임에서 어떤 이슈가 있었고, 그 시점에 유저 여론은 어떻게 바뀌었는가"

게임은 출시 이후 패치, DLC, 운영 논란, 버그 등 다양한 이벤트를 겪는다.
현재 Steam/Metacritic 리뷰 요약은 **전체 기간의 평균**만 보여주지만,
이슈 트래킹은 **특정 시점의 여론 변화**를 포착한다.

---

## 2. 핵심 가치

| 기존 unified summary | 이슈 트래킹 |
|---------------------|------------|
| "전반적으로 좋은 게임" | "2024년 3월 패치 이후 밸런스 논란" |
| 전체 기간 평균 감성 | 이벤트 전후 감성 변화 |
| 정적 요약 | 시계열 맥락 제공 |

두 기능이 함께 제공되면:
> "종합적으로는 긍정적이나, 특정 패치 이후 핵심 유저층이 이탈한 이력이 있음"

---

## 3. 사용 데이터 소스

### 3-1. Steam News API (공식)
```
GET https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/
    ?appid={appid}&count=100&format=json
```

- 패치노트, 업데이트 공지, 개발자 발표 등 타임스탬프와 함께 제공
- `feedlabel` 필드로 "Patch Notes" / "Game Update" 등 카테고리 구분 가능
- 공식 API, 인증 불필요

### 3-2. Steam Review Histogram (비공식)
```
GET https://store.steampowered.com/appreviewhistogram/{appid}?l=en
```

- 게임 출시 이후 월별 긍정/부정 리뷰 수 반환
- 여론 변곡점(급변 구간) 감지에 활용
- 비공식이지만 커뮤니티 도구에서 광범위하게 사용 중

### 3-3. 기존 리뷰 DB (기수집)
- `ExternalReview.date_posted` 필드를 활용해 이벤트 시점 전후 리뷰 필터링
- 별도 수집 불필요, 기존 크롤링 데이터 재활용

---

## 4. 시스템 구조

```
┌─────────────────────────────────────────────┐
│              데이터 수집 레이어               │
│                                             │
│  Steam News API    Review Histogram API     │
│  (이벤트 타임라인)  (월별 긍부정 비율)        │
└────────────┬───────────────┬────────────────┘
             │               │
             ▼               ▼
┌─────────────────────────────────────────────┐
│              이벤트 감지 레이어               │
│                                             │
│  1. Histogram에서 감성 변곡점 탐지           │
│     (전월 대비 부정 비율 ±20%p 이상)         │
│  2. 변곡점 시점과 News 이벤트 매칭           │
│  3. GameEvent 레코드 생성                   │
└────────────────────┬────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────┐
│           이벤트 기반 요약 레이어             │
│                                             │
│  이벤트 날짜 ± 14일 리뷰 필터링             │
│  (기존 ExternalReview.date_posted 활용)     │
│            ↓                               │
│  기존 Map-Reduce 파이프라인 실행            │
│  (변경 없이 재사용)                         │
│            ↓                               │
│  EventSummary 저장                         │
└────────────────────┬────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────┐
│              API 제공 레이어                 │
│                                             │
│  GET /games/{id}/events                    │
│  → 이벤트 목록 + 각 시점 요약              │
└─────────────────────────────────────────────┘
```

---

## 5. 변곡점 감지 알고리즘

```python
def detect_inflection_points(monthly_data: list[dict]) -> list[dict]:
    """
    monthly_data: [{"positive": 120, "negative": 30}, ...]
    반환: 변곡점 목록 (월 인덱스 + 변화율)
    """
    inflections = []
    for i in range(1, len(monthly_data)):
        prev = monthly_data[i-1]
        curr = monthly_data[i]

        prev_neg_ratio = prev["negative"] / (prev["positive"] + prev["negative"])
        curr_neg_ratio = curr["negative"] / (curr["positive"] + curr["negative"])

        delta = curr_neg_ratio - prev_neg_ratio  # 양수 = 부정 증가, 음수 = 긍정 회복

        if abs(delta) >= 0.20:  # 20%p 이상 변화
            inflections.append({
                "month_index": i,
                "delta": delta,
                "direction": "negative_spike" if delta > 0 else "positive_recovery"
            })
    return inflections
```

---

## 6. DB 스키마 추가

### GameEvent 테이블
```sql
CREATE TABLE game_events (
    id              SERIAL PRIMARY KEY,
    game_id         INTEGER REFERENCES games(id),
    event_date      DATE NOT NULL,
    event_type      VARCHAR(32),   -- patch / dlc / controversy / sale / unknown
    title           TEXT,          -- News API에서 가져온 제목
    news_url        TEXT,          -- 원문 링크
    sentiment_delta FLOAT,         -- 부정 비율 변화량 (Histogram 기반)
    direction       VARCHAR(32),   -- negative_spike / positive_recovery
    created_at      TIMESTAMP DEFAULT now()
);
```

### EventSummary 테이블
```sql
CREATE TABLE event_summaries (
    id              SERIAL PRIMARY KEY,
    event_id        INTEGER REFERENCES game_events(id),
    summary_text    TEXT,          -- 해당 시점 리뷰 요약
    sentiment_overall VARCHAR(16), -- positive / mixed / negative
    sentiment_score FLOAT,
    pros_json       JSONB,
    cons_json       JSONB,
    keywords_json   JSONB,
    source_review_count INTEGER,
    review_window_start DATE,      -- 수집 기간 시작
    review_window_end   DATE,      -- 수집 기간 종료
    created_at      TIMESTAMP DEFAULT now()
);
```

---

## 7. 새로 만들 컴포넌트

| 파일 | 역할 |
|------|------|
| `crawling/steam/news_crawler.py` | Steam News API 호출, 이벤트 파싱 |
| `crawling/steam/histogram_crawler.py` | Histogram 파싱, 변곡점 감지 |
| `backend/app/models/domain.py` | `GameEvent`, `EventSummary` 모델 추가 |
| `backend/app/services/event_service.py` | 이벤트 감지 + 요약 오케스트레이션 |
| `backend/app/api/v1/events.py` | 이벤트 조회 엔드포인트 |

### 재사용하는 기존 컴포넌트 (변경 없음)
- `ai_module/map_reduce/pipeline.py` — 파이프라인 그대로 재사용
- `ai_module/map_reduce/sampler.py` — 리뷰 샘플링 그대로 재사용
- `ai_module/evaluation/` — 신뢰도 평가 그대로 재사용

---

## 8. API 명세

### 이벤트 목록 조회
```
GET /api/v1/games/{game_id}/events

Response:
{
  "game_id": 123,
  "events": [
    {
      "id": 1,
      "event_date": "2024-03-15",
      "event_type": "patch",
      "title": "Version 2.1.0 Balance Update",
      "news_url": "https://store.steampowered.com/...",
      "direction": "negative_spike",
      "sentiment_delta": 0.28,
      "summary": {
        "sentiment_overall": "negative",
        "sentiment_score": 31.2,
        "pros": ["전투 메커니즘 개선"],
        "cons": ["핵심 캐릭터 과도한 너프", "과금 요소 추가"],
        "keywords": ["밸런스", "너프", "과금", "환불"],
        "summary_text": "2.1.0 패치 이후 밸런스 변경에 대한 강한 반발...",
        "source_review_count": 847,
        "review_window": "2024-03-08 ~ 2024-03-29"
      }
    }
  ]
}
```

---

## 9. 기존 시스템과의 관계

```
unified summary   →  게임 전체 종합 평가 (유지)
event summary     →  특정 이슈 시점 여론 (신규)
regional summary  →  언어권별 여론 비교 (제거)
```

Regional Pipeline 제거로 확보되는 AI 호출 비용을 Event Summary 생성에 활용한다.

---

## 10. 구현 순서

1. `histogram_crawler.py` — 변곡점 감지 로직 (데이터 기반)
2. `news_crawler.py` — Steam News API 연동
3. `GameEvent` / `EventSummary` DB 모델 추가
4. `event_service.py` — 이벤트 + 요약 오케스트레이션
5. `events.py` API 엔드포인트
6. Regional Pipeline 제거 (별도 작업)
