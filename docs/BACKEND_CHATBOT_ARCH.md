# 백엔드 & 챗봇 아키텍처 상세

이 문서는 FastAPI 백엔드와 챗봇 시스템의 구현 세부 사항을 다룹니다.  
전체 데이터 흐름과 배포 구성은 [ARCHITECTURE.md](./ARCHITECTURE.md)를 참고하세요.

---

## 목차

1. [백엔드 전체 구조](#1-백엔드-전체-구조)
2. [라우터 & 엔드포인트](#2-라우터--엔드포인트)
3. [서비스 레이어](#3-서비스-레이어)
4. [ORM 모델 요약](#4-orm-모델-요약)
5. [백그라운드 잡](#5-백그라운드-잡)
6. [챗봇 시스템](#6-챗봇-시스템)
7. [캐싱 & Rate Limiting](#7-캐싱--rate-limiting)
8. [인증](#8-인증)

---

## 1. 백엔드 전체 구조

```
backend/app/
├── main.py                  # FastAPI 앱, 미들웨어, 라우터 등록
├── core/
│   ├── database.py          # SQLAlchemy async 엔진, get_db() 의존성
│   ├── redis_client.py      # Redis 캐시 헬퍼 (get/set/invalidate)
│   └── auth.py              # X-API-Key 헤더 인증
├── models/
│   └── domain.py            # 모든 ORM 모델 단일 파일
├── schemas/
│   ├── steam.py             # SteamPayload Pydantic 스키마
│   └── metacritic.py        # MetacriticPayload Pydantic 스키마
├── api/v1/
│   ├── reviews.py           # 리뷰 수집 수신
│   ├── summaries.py         # AI 요약 조회·트리거
│   ├── analysis.py          # 플레이타임·크리틱·유저 분석
│   ├── chat.py              # 챗봇 추천
│   ├── translate.py         # 텍스트 번역
│   ├── highlights.py        # 베스트 리뷰
│   ├── buy_signal.py        # 구매 타이밍 신호
│   ├── divergence.py        # Steam↔Metacritic 의견 괴리
│   └── appeal.py            # 추천 대상 플레이어
├── services/
│   ├── ai_service.py        # AI 파이프라인 오케스트레이션 (1200+ lines)
│   ├── chat_service.py      # 챗봇 카탈로그 빌드 + Groq 호출
│   ├── buy_signal_logic.py  # 구매 신호 판단 로직
│   └── recommendation_targets.py  # 추천 대상 레이블 정제
└── jobs/
    ├── scheduler.py         # 일별 배치 오케스트레이터
    ├── ai_batch.py          # 전체 게임 증분 요약
    ├── price_refresher.py   # 가격·감정 스냅샷 → Redis
    └── review_crawler_job.py # Steam 리뷰 증분 수집
```

### 진입점 (`main.py`)

- FastAPI 앱 생성, CORS 미들웨어 (`allow_origins=["*"]`)
- 9개 라우터를 `/api/v1` prefix로 등록
- `sys.path`에 `ai-pipeline` 경로 추가 (Docker 내부 `/workspace/ai-pipeline`)

---

## 2. 라우터 & 엔드포인트

### 리뷰 수집 (`reviews.py`)

```
POST /api/v1/reviews/steam          [API Key 필수]
POST /api/v1/reviews/metacritic     [API Key 필수]
```

- Steam: `is_recommended` boolean + `playtime_hours`, `normalized_score_100`으로 변환
- Metacritic: `review_type` (critic/user) 구분, 100점 기준 정규화
- 중복 방지: `source_review_key` unique constraint + upsert

### 요약 조회 & 파이프라인 트리거 (`summaries.py`)

```
GET  /api/v1/games                                → 전체 게임 목록
GET  /api/v1/games/:id                            → 단건 메타데이터
GET  /api/v1/games/:id/summary                    → AI 요약 (Redis 24h 캐시)
POST /api/v1/games/:id/summarize    [API Key]     → AI 파이프라인 비동기 트리거
GET  /api/v1/games/:id/reviews-for-map [API Key]  → 외부 Map 실행용 데이터
POST /api/v1/games/:id/reduce       [API Key]     → 외부 Map 결과로 Reduce 실행
```

`/summary` 응답 주요 필드:
```json
{
  "one_liner": "...",
  "sentiment_overall": "positive|mixed|negative",
  "sentiment_score": 78,
  "aspect_sentiment": { "graphics": { "label": "긍정적", "score": 82 } },
  "pros": ["..."],
  "cons": ["..."],
  "keywords": ["..."],
  "recommended_for": [{ "label": "...", "reason": "..." }],
  "representative_reviews": [{ "text": "...", "platform": "steam" }]
}
```

`/summarize` 쿼리 파라미터:
- `force=true` — cursor 초기화, 전체 리뷰 재처리
- `map_backend=local|groq` — Map 단계 모델 선택

### 분석 (`analysis.py`)

```
GET /api/v1/games/:id/playtime-analysis   → 플레이타임 3구간 분석 (Redis 캐시)
GET /api/v1/games/:id/critic-summary      → Metacritic 전문가 요약 (Redis 캐시)
GET /api/v1/games/:id/user-summary        → 일반 유저 요약 (Redis 캐시)
```

- `playtime-analysis`: Steam 리뷰 18개 미만 → 404
- `critic-summary`: Metacritic 크리틱 리뷰 10개 미만 → 404

### 챗봇 (`chat.py`)

```
POST /api/v1/chat/recommend
```

상세 내용은 [§6 챗봇 시스템](#6-챗봇-시스템) 참고.

### 베스트 리뷰 (`highlights.py`)

```
GET /api/v1/games/:id/highlights?limit=5   [API Key]
```

채점 기준: `is_recommended` or `score ≥ 70`, 감정 키워드, 느낌표, 텍스트 길이 → 상위 N개

### 구매 신호 (`buy_signal.py`)

```
GET /api/v1/games/:id/buy-signal
```

Redis의 가격 스냅샷(`price_refresher` 관리) + 감정 spike 여부로 구매 타이밍 판단.  
Steam API 직접 호출 없음.

### 추천 대상 (`appeal.py`)

```
GET /api/v1/games/:id/recommendation-targets?limit=4
```

`game_review_summaries.recommended_for_json` → 레이블 정제(`recommendation_targets.py`) → 반환

---

## 3. 서비스 레이어

### `ai_service.py` — AI 파이프라인 오케스트레이터

**`run_ai_pipeline_task(game_id, mode, language_code, force, map_backend)`**

```
1. game_summary_cursor 조회 → last_summarized_review_id
2. 신규 리뷰 fetch (cursor 이후 id만, force이면 전체)
3. 플랫폼 통계 계산
     steam_recommend_ratio, metacritic_avg, category_frequency
4. run_hybrid_summary_pipeline() 호출 (ai-pipeline 모듈)
     Map: 청크별 근거 추출 (로컬 Ollama 또는 Groq)
     Reduce: Groq API로 최종 요약 생성
5. 대표 리뷰 선택 (Steam 3 + Metacritic 3)
6. 품질 지표 계산
     sentiment_alignment, coverage_ratio, staleness_ratio, semantic_similarity_score
7. playtime_analyses / critic_summaries / user_summaries upsert
8. game_review_summaries 저장 (is_current=True)
9. cursor 업데이트 (last_summarized_review_id, last_summary_version)
10. Redis 캐시 무효화
```

- reduce 실패 시 기존 요약 보존 (partial 업데이트 방지)
- `AI_REDUCE_PAYLOAD_SAVE=auto` 이면 첫 요약·force 재처리 시 artifact 저장

주요 내부 헬퍼:

| 함수 | 역할 |
|------|------|
| `_strip_grounding_anchor()` | `(review_id=N)` 앵커를 공개 텍스트에서 제거 |
| `_select_platform_representative_reviews()` | Steam 3 + Metacritic 3 대표 리뷰 선택 |
| `_upsert_playtime_analysis()` | AI 요약 + 실제 추천 통계 병합 저장 |
| `_upsert_critic_summary()` | Metacritic 크리틱 요약 저장 |
| `_upsert_user_summary()` | 일반 유저 요약 저장 |
| `_cumulative_playtime_from_reviews()` | 전체 리뷰 기준 p33/p66 버킷 경계 계산 |
| `_compute_cumulative_aspect_counts()` | 카테고리 태그 누적집계 (aspect baseline) |

### `chat_service.py` — 챗봇 서비스

[§6](#6-챗봇-시스템) 참고.

### `recommendation_targets.py`

- `_repair_player_label(label, reason)` — "오픈 월드" 같은 과도하게 넓은 레이블을 근거 텍스트 키워드로 구체화
- `sanitize_player_targets(items, limit=5)` — 중복 제거, 빈 이유 필터, 정렬

폴백 레이블 (추천 미생성 시):
```
"빌드 조합을 즐기는 플레이어"
"빠른 액션을 선호하는 플레이어"
"서사와 캐릭터에 집중하는 플레이어"
"반복 플레이를 즐기는 플레이어"
```

### `buy_signal_logic.py`

Redis 가격 스냅샷 + `GameReviewSummary.steam_recommend_ratio` 결합 → 구매 타이밍 판단  
반환: `{ is_good_timing, reason, discount_pct, price_before, price_current, sentiment_trend }`

---

## 4. ORM 모델 요약

모든 모델은 `models/domain.py` 단일 파일에 정의됩니다.

| 테이블 | 역할 | 핵심 컬럼 |
|--------|------|-----------|
| `games` | 정규화된 게임 레코드 | `normalized_title`, `release_date` |
| `game_platform_map` | 게임↔플랫폼 매핑 | `external_game_id`, `platform_meta_json` (커버·태그 JSONB) |
| `external_reviews` | 크롤링 원본 리뷰 | `normalized_score_100`, `playtime_hours`, `review_categories_json` |
| `game_review_summaries` | AI 최종 요약 | `is_current`, `one_liner`, `aspect_sentiment_json`, `pros_json`, `cons_json` |
| `playtime_analyses` | 플레이타임 구간별 요약 | `early_max_hours`, `mid_max_hours`, 구간별 summary/score/pros/cons |
| `critic_summaries` | Metacritic 전문가 요약 | `summary_text`, `pros_json`, `cons_json` |
| `user_summaries` | 일반 유저 요약 | `summary_text`, `pros_json`, `cons_json` |
| `game_summary_cursor` | 증분 파이프라인 커서 | `last_summarized_review_id`, `last_summary_version` |
| `review_summary_jobs` | 파이프라인 실행 로그 | 토큰 수, 품질 지표, failure_reasons_json |
| `review_summary_chunks` | Map 단계 중간 결과 | `chunk_no`, `summary` (job 삭제 시 cascade) |
| `game_events` | 패치·DLC·할인 이벤트 | `event_date`, `event_type`, `sentiment_delta` |
| `ingestion_runs` | 크롤러 실행 로그 | `fetched`, `inserted`, `updated`, `errors` |

**주요 unique constraint**

- `external_reviews`: `(platform_id, game_id, source_review_key)`
- `game_platform_map`: `(platform_id, external_game_id)`, `(game_id, platform_id)`
- `game_review_summaries`: `is_current=True` 1개 유지 (파이프라인이 관리)
- `playtime_analyses`, `critic_summaries`, `user_summaries`: 게임당 1행

---

## 5. 백그라운드 잡

### `jobs/scheduler.py` — 일별 배치 오케스트레이터

매일 **17:05 UTC** (Steam 가격 갱신 후) 순차 실행:

```
1. price_refresher.refresh_once()
     → Steam API 가격 조회 → Redis 스냅샷 저장

2. review_crawler_job.crawl_steam_incremental()
     → Steam 신규 리뷰 수집 → external_reviews upsert

3. ai_batch.run_ai_batch()
     → 전체 게임 증분 요약 (게임별 격리, 한 게임 실패해도 계속)
```

- 단일 인스턴스 실행 → Groq API 동시 호출 방지
- 각 단계 실패 시 다음 단계 계속 (fail-isolated)
- `--once` (단발) / `--loop` (반복) 모드

### `jobs/ai_batch.py`

```python
for game in all_games:
    try:
        await run_ai_pipeline_task(game.id, force=False)
    except Exception:
        ai_fail += 1
        continue
```

반환: `{ ai_ok, ai_fail, total }`

---

## 6. 챗봇 시스템

### 6-1. 요청 흐름

```
POST /api/v1/chat/recommend
    │
    ├─ 1. IP별 Rate Limit 체크
    │       Redis key: chat_rate:{ip}
    │       한도: 10회/분 (60s 윈도우)
    │       초과 시 429 반환
    │
    ├─ 2. build_game_catalog(db)
    │       Redis hit  → chat:game_catalog (5분 TTL) 반환
    │       Redis miss → DB 전체 게임 조회
    │                    게임당: title, tags(5), one_liner, pros(4),
    │                             cons(3), keywords(6),
    │                             recommended_for(3), caution_for(2),
    │                             steam_recommend_ratio
    │                    → Redis에 저장 후 반환
    │
    ├─ 3. Groq API 호출
    │       system  : 게임 카탈로그 + 가드레일 프롬프트
    │       messages: 최근 20개 (메시지당 최대 1000자)
    │       model   : GROQ_TRANSLATE_MODEL (기본값: GROQ_MODEL)
    │       temperature: 0.7  /  timeout: 30s
    │       429 발생 시 GroqKeyRotator로 키 순환
    │
    └─ 4. ChatResponse { reply: "..." } 반환
```

### 6-2. `api/v1/chat.py`

```
POST /api/v1/chat/recommend
  Request:  ChatRequest(messages: list[{role, content}])
  Response: ChatResponse(reply: str)
```

Rate Limit 구현:
```python
key = f"chat_rate:{client_ip}"
count = await redis.incr(key)
if count == 1:
    await redis.expire(key, 60)
if count > 10:
    raise HTTPException(429)
```

Redis 장애 시 → Rate Limit 비활성화 (fail-open)

### 6-3. `services/chat_service.py`

주요 상수:
```python
MAX_HISTORY_MESSAGES = 20      # 전달할 최대 메시지 수
CATALOG_CACHE_TTL    = 300     # 게임 카탈로그 Redis TTL (초)
GROQ_TIMEOUT         = 30      # API 호출 타임아웃 (초)
GROQ_TEMPERATURE     = 0.7
```

**시스템 프롬프트 가드레일:**
- DB에 있는 게임만 추천 (환각 방지)
- 추천 근거는 카탈로그 데이터(tags, pros, cons, keywords)에 한정
- 게임 외 주제 질문 시 거절
- 싫다고 한 게임과 유사 게임 추천 금지
- 한국어 응답 고정

### 6-4. `GroqKeyRotator` (`ai_module/map_reduce/key_rotator.py`)

- **모듈 레벨 싱글턴**으로 인스턴스화 → 요청 간 키 순환 상태 유지
- `from_key_string("key1,key2,key3")` — 콤마 구분 키 파싱
- 429 에러 발생 시 다음 키로 자동 전환 (round-robin)
- 챗봇(`chat_service.py`)과 Groq Map(`map_groq.py`) 양쪽에서 공유

### 6-5. 프론트엔드 `ChatBot.jsx`

```
[게임 컨트롤러 버튼] → 클릭 → 채팅 창 토글 (우하단 고정)
```

| 항목 | 내용 |
|------|------|
| 창 크기 | 너비 320px, 높이 520px |
| 전송 | `Enter` 전송 / `Shift+Enter` 줄바꿈 |
| 글자 제한 | 1000자 |
| 히스토리 | `{id(UUID), role, content}` 배열 |
| 취소 | `AbortController`로 진행 중인 fetch 취소 가능 |
| 리셋 | 대화 초기화 버튼 |
| 스크롤 | 최신 메시지 자동 스크롤 |

상태:
```js
messages   // 전체 대화 이력
input      // 현재 입력값
isLoading  // 전송 중 여부 (UI 비활성화)
isOpen     // 창 열림/닫힘
```

---

## 7. 캐싱 & Rate Limiting

모든 Redis 조작은 `core/redis_client.py`에 집중되어 있습니다.

| Redis Key | TTL | 담당 |
|-----------|-----|------|
| `game_summary:{game_id}:{lang}` | 24h | AI 통합 요약 |
| `playtime_analysis:{game_id}` | 24h | 플레이타임 분석 |
| `critic_summary:{game_id}` | 24h | 크리틱 요약 |
| `user_summary:{game_id}` | 24h | 유저 요약 |
| `highlights:{game_id}:*` | 24h | 베스트 리뷰 |
| `chat_rate:{ip}` | 60s | 챗봇 Rate Limit |
| `chat:game_catalog` | 5min | 챗봇 게임 카탈로그 |
| `map_chunk:{game_id}:{model}:{ver}:{hash}` | 7일 | Map 단계 중간 결과 |
| `buy_signal:price:{game_id}` | scheduler 관리 | 가격 스냅샷 |
| `buy_signal:sentiment:{game_id}` | scheduler 관리 | 감정 스냅샷 |

**캐시 무효화 시점:**

- 파이프라인 재실행 → 해당 게임 전체 키 삭제
- 신규 리뷰 수집 → summary, playtime, highlights 삭제
- Redis 장애 → 비차단 graceful degradation (DB fallback)

---

## 8. 인증

**`core/auth.py`**

- `X-API-Key` 헤더 검증 → `API_SECRET_KEY` 환경변수와 비교
- 불일치 시 401 반환

| 구분 | 엔드포인트 |
|------|-----------|
| **API Key 필수** | `POST /reviews/*`, `POST /games/:id/summarize`, `GET /games/:id/highlights`, `GET /games/:id/reviews-for-map`, `POST /games/:id/reduce`, `GET /games/:id/divergence` |
| **공개** | `GET /games`, `GET /games/:id/summary`, `GET /games/:id/playtime-analysis`, `GET /games/:id/critic-summary`, `GET /games/:id/user-summary`, `GET /games/:id/buy-signal`, `GET /games/:id/recommendation-targets` |
| **Rate Limit만** | `POST /chat/recommend` |
