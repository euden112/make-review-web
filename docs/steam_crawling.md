# Steam 리뷰 크롤링

## 담당 역할

Steam API를 활용한 **게임 리뷰 수집 및 전처리 파이프라인** 구현을 담당했습니다.  
수집된 리뷰는 AI 요약 파이프라인의 입력 데이터로 사용됩니다.

---

## 관련 파일

- `crawling/steam/steam_crawler.py` — 핵심 크롤링 로직
- `crawling/game_list.json` — 수집 대상 게임 목록
- `crawling/output/steam.json` — 수집 결과 저장

---

## 전체 흐름

```
game_list.json
      ↓
Steam Review API 호출 (한국어 + 영어)
      ↓
전처리 (텍스트 정제 + 스팸 필터)
      ↓
카테고리 태깅 (그래픽/조작감/최적화 등)
      ↓
steam.json 저장
      ↓
send_to_api.py → 백엔드 DB 적재
```

---

## 핵심 기능

### 1. 2-Pool 수집 전략

단순 최신순 수집의 편향 문제를 해결하기 위해 **두 가지 풀을 조합**합니다.

```python
RECENT_PER_LANG  = 80   # 최신순: 현재 여론·최근 패치 반영
HELPFUL_PER_LANG = 120  # 도움순: 오래된 핵심 리뷰 확보
```

| 풀 | 방식 | 목적 |
|---|---|---|
| Pool 1 (최신순) | `filter=recent` | 최신 패치 반응, 현재 여론 반영 |
| Pool 2 (도움순) | `filter=all` | 오래됐어도 핵심 호평/비판 확보 |

- 두 풀을 합친 후 `seen` 집합으로 **중복 제거**
- recent를 먼저 등록해 겹치는 리뷰는 최신 버전 유지
- 게임당 한국어 + 영어 각각 수집 → 최대 **200개** 저장

---

### 2. 전처리 파이프라인

수집한 리뷰를 AI 요약에 적합하도록 정제합니다.

```python
def preprocess_body(text: str) -> str | None:
    # 줄바꿈·탭 → 공백
    # 이모지 제거
    # 반복 특수문자 정리
    # 최소 길이(10자) 미만 제거
    # 최대 길이(1000자) 초과 시 문장 단위 잘라내기
```

| 처리 항목 | 내용 |
|---|---|
| 최소 길이 | 10자 미만 제거 |
| 최대 길이 | 1000자 초과 시 문장 단위 트리밍 |
| 이모지 | 전부 제거 |
| 반복 특수문자 | 2개 초과 시 1개로 축약 |

---

### 3. 스팸 필터

```python
def rule_based_filter(text):
    # URL 2개 이상 → 스팸 처리

def korean_spam_filter(text):
    # 자모(ㅋㅋㅋ, ㅠㅠ 등) 비율 50% 초과 → 스팸 처리
```

---

### 4. 카테고리 태깅

리뷰 텍스트에서 **11개 카테고리**를 키워드 매칭으로 분류하고 긍/부정 감성도 함께 태깅합니다.

| 카테고리 | 예시 키워드 |
|---|---|
| 그래픽 | 그래픽, 비주얼, graphics, visuals |
| 조작감 | 조작감, 컨트롤, controls, input lag |
| 최적화 | 프레임, 렉, fps drop, stuttering |
| 콘텐츠 양 | 플레이타임, endgame, replayability |
| 가성비 | 가성비, 세일, overpriced, refund |
| 스토리 | 스토리, 세계관, narrative, lore |
| 사운드 | BGM, OST, soundtrack, audio |
| 난이도 | 난이도, 소울라이크, souls-like |
| 멀티플레이 | 멀티, 코옵, co-op, matchmaking |
| 재미 | 재미, 중독성, addictive, fun to play |
| 버그 | 버그, 오류, buggy, game-breaking |

```python
# 태깅 결과 예시
[
    {"category": "그래픽", "sentiment": "positive"},
    {"category": "최적화", "sentiment": "negative"}
]
```

---

### 5. 이미지 및 태그 수집

게임 메타데이터도 함께 수집합니다.

```python
# 커버/히어로 이미지 URL 수집
images = get_image_urls(app_id)

# Steam 인기 태그 수집 (장르 분류용)
tags = fetch_popular_tags(app_id)  # 예: ["RPG", "오픈 월드", "액션"]
```

---

### 6. 재시작 시 중복 수집 방지

```python
# 이미 수집된 게임은 스킵
if slug in existing_data:
    print(f"  → 이미 수집됨, 스킵: {slug}")
    continue
```

크롤링 도중 중단되어도 재시작 시 완료된 게임은 건너뜁니다.

---

### 7. Rate Limit 대응

```python
for attempt in range(5):
    try:
        resp = requests.get(url, ...)
        if resp.status_code == 429:
            raise requests.RequestException("Rate Limit 429")
    except:
        backoff = min(30, (2 ** attempt) + random.uniform(0, 1))
        time.sleep(backoff)  # 지수 백오프
```

Steam API의 요청 제한에 대응하기 위해 **지수 백오프(Exponential Backoff)** 방식으로 최대 5회 재시도합니다.

---

## 차별점 요약

| 항목 | 내용 |
|---|---|
| 수집 전략 | 최신순 + 도움순 2-Pool 조합으로 편향 방지 |
| 언어 | 한국어(koreana) + 영어(english) 동시 수집 |
| 카테고리 태깅 | 11개 카테고리 키워드 매칭 + 긍부정 감성 분류 |
| 스팸 필터 | URL 다수 포함, 자모만 나열된 리뷰 자동 제거 |
| 안정성 | 지수 백오프 재시도 + 중단 후 재시작 지원 |
| 메타데이터 | 커버/히어로 이미지, 인기 태그 함께 수집 |

---

## 한계 및 향후 개선 방향

- Steam API 특성상 삭제된 리뷰는 수집 불가
- 언어 필터가 한국어/영어만 지원 (다국어 확장 가능)
- 키워드 기반 카테고리 태깅이라 문맥 파악에 한계 있음 (LLM 기반 태깅으로 개선 가능)
- 게임당 최대 200개로 제한되어 있어 리뷰 수가 많은 인기 게임은 샘플링 편향 가능성 있음
