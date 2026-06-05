# 기술 아키텍처와 파이프라인 상세

개발자용 기술 레퍼런스입니다. 서비스 소개는 [../README.md](../README.md)를 참고하세요. 이 문서의 모든 설명은 현재 코드(`backend/`, `ai-pipeline/`, `frontend/`, `crawling/`, `database/`)를 기준으로 작성했습니다.

> 이 문서는 **데이터 흐름·파이프라인·점수 산출**에 초점을 둡니다. FastAPI 라우터·서비스 레이어·ORM 모델·백그라운드 잡·챗봇 요청 흐름·Redis 키 표를 **코드 단위로** 보려면 [BACKEND_CHATBOT_ARCH.md](./BACKEND_CHATBOT_ARCH.md)를 참고하세요(아래 §9·§11은 그 요약 + 점수 로직 보강).

---

## 1. 전체 시스템 흐름

리뷰를 **수집 → 적재 → DB 저장 → Map(근거 추출) → Reduce(요약·점수화) → 분리 저장 → 프론트 제공**하는 단방향 파이프라인입니다.

```
[크롤러] Steam(ko/en, 최신순+도움순) / Metacritic(critic, en)
   │  리뷰 + 작성시점 플레이타임 + 도움수 + 문장단위 카테고리 태그 + 인기태그(장르)
   ▼
[send_to_api] ──X-API-Key──▶ POST /api/v1/reviews/{platform}
                                   │  점수 normalized_score_100(0~100)로 정규화
                                   ▼
                              PostgreSQL: external_reviews (+ games / game_platform_map)
                                   │
[로컬 GPU 머신]                     │ ① GET /games/{id}/reviews-for-map (커서 기반 증분)
  run_map_pipeline.py  ◀───────────┘
   │ ② Map: 로컬 Ollama(gemma4:e4b) 또는 Groq → 리뷰 묶음별 evidence JSON
   │    └ Redis chunk 캐시 + 첫/force payload artifact 보존
   │ ③ score_anchors를 Steam 공식 query_summary로 보강(모드 A·B 공통)
   │ ④ POST /games/{id}/reduce (버킷·타입별 근거 + 앵커 전송)
   ▼ (Cloudflare 터널)                         ▼
                              Reduce: Groq(llama-4-scout) 기능별 4회 호출
                              (user / critic / playtime / final)
                                   │  검증·정제·결정론 점수 산출
                                   ▼
              game_review_summaries · user_summaries · playtime_analyses · critic_summaries
                                   │  + Redis 요약 캐시 무효화
                                   ▼
[프론트엔드(React/nginx)] ──GET(공개)──▶ 목록 · 상세(레이더·구간·추천·구매시그널) · 비교 · 챗봇
```

### Map과 Reduce를 나눈 이유

- **Map = 토큰 비용 집중 단계**: 리뷰 원문 전체를 다루므로 입력 토큰이 가장 큽니다. 이를 로컬 GPU(Ollama)로 처리하면 Groq의 분당 토큰 한도(TPM)와 비용을 피할 수 있습니다. 모델은 `gemma4:e4b`를 사용하며, 내부 비교에서 `qwen2.5:7b`보다 빠르고 JSON 형식 준수가 안정적이었습니다.
- **Reduce = 품질 집중 단계**: 추출된 근거를 한국어로 종합·구조화하는 단계로, 품질이 중요하므로 Groq의 `llama-4-scout`를 씁니다. 입력이 근거 JSON으로 압축돼 있어 토큰이 작습니다.
- **이원화의 운영 이점**: Map을 로컬/Groq로 라우팅(`--map-route auto`)할 수 있어, 대량 작업은 로컬 GPU로 비용을 아끼고 소규모 증분은 GPU 없이 클라우드만으로 처리합니다.

---

## 2. local Map과 Groq Map/Reduce 운영 방식

`MAP_BACKEND` 환경 변수와 `run_map_pipeline.py`의 라우팅이 Map 실행 위치를 정합니다(Reduce는 항상 Groq).

| 경로 | 트리거 | Map 위치 | 비고 |
|---|---|---|---|
| **모드 A (in-process)** | `POST /games/{id}/summarize` → `run_ai_pipeline_task` | `MAP_BACKEND` (클라우드 기본 `groq`) | 스케줄러 일일 증분이 이 경로. |
| **모드 B (precomputed)** | `run_map_pipeline.py` → `POST /games/{id}/reduce` → `run_reduce_from_precomputed_map` | 로컬 Ollama 또는 Groq (`--map-route`) | 첫 요약·전체 재처리. payload artifact 보존. |

- **라우팅(`--map-route auto`)**: 배치 리뷰 수가 임계 이상이면 로컬 Map, 작은 증분이면 Groq Map으로 보냅니다(`run_map_pipeline._route_to_groq`).
- **결정성**: Map은 순차 처리하고, 로컬 Ollama는 `OLLAMA_NUM_PARALLEL=1`로 슬롯을 직렬화해 실행 간 결과 흔들림을 줄입니다(`docker-compose.map.yml`).
- **Replay**: 모드 B의 `--from-payload`는 저장된 payload로 Map을 건너뛰고 Reduce만 다시 전송합니다. Reduce 로직만 바꿨을 때 Map 재실행 비용 없이 재생성할 수 있습니다.

---

## 3. 크롤링 → 적재 → DB

### 3-1. 크롤링 (`crawling/`)

- **Steam** (`steam_crawler.py`): 언어별(`koreana`/`english`)로 최신순(`RECENT_PER_LANG=80`)과 전체기간 도움순(`HELPFUL_PER_LANG=120`)을 함께 수집하고 `seen` 집합으로 교차 중복 제거합니다. 최신성(세일 유입·패치 이슈 반영)과 대표성(고-helpful 핵심 호평)을 모두 확보하려는 설계입니다. 리뷰마다 작성 시점 플레이 시간·도움 수·문장 단위 카테고리 태그를 수집하고, 상점 페이지의 인기 태그(장르) 상위 8개를 함께 가져옵니다.
- **Metacritic** (`metacritic_crawler.py`): Playwright로 `game_list.json`의 `metacritic_slug` 기준 평론가(critic) 리뷰를 영어로 수집합니다.
- **적재** (`send_to_api.py`): `/api/v1/reviews/{platform}`로 전송(쓰기, `X-API-Key` 필요). 점수는 `normalized_score_100`(0~100)으로 정규화합니다. Steam은 이진 추천(`is_recommended`), Metacritic critic은 100점, user는 10점(×10)을 사용합니다.

### 3-2. 적재 시 증분 보장

ingestion은 `(platform_id, game_id, source_review_key)` 유니크키로 upsert합니다(`reviews.py`). 이미 있는 리뷰는 UPDATE되어 중복 행이 생기지 않고, 신규 리뷰만 새 `external_reviews.id`를 받습니다. 따라서 일일 크롤이 같은 최신 윈도우를 재전송해도 안전하며, 요약 커서가 그 신규 ID만 처리합니다.

---

## 4. Map 단계 (`run_map_pipeline.py`, `map_local.py`, `map_groq.py`, `pipeline.py`)

1. `GET /games/{id}/reviews-for-map`으로 커서(`game_summary_cursor.last_summarized_review_id`) 이후의 신규 리뷰(증분) 또는 전체(`force`)를 받습니다.
2. `sampler.py`가 점수 분포·플랫폼·플레이타임 버킷을 고려해 층화 추출합니다(기본 목표 200건, `AI_SUMMARY_REVIEW_TARGET`). 한·영 리뷰만 통과시키고, 스팸 룰(`rules.py`)을 적용하며, 반례(부정) 근거가 최소 수 이상 포함되도록 보강합니다.
3. Steam 리뷰의 플레이타임 분포에서 **p33·p66 백분위**로 초반·중반·후반 경계를 정합니다(`compute_playtime_buckets`). 백분위 기반이라 항상 약 3등분되고 극단값에 흔들리지 않습니다. 플레이타임 보유 리뷰가 `MIN_REVIEWS_PER_BUCKET`(=18) 미만이면 버킷을 만들지 않습니다.
4. 버킷별로 균형 있게 청크를 구성합니다(각 구간 근거 누락 방지).
5. 청크마다 LLM이 **evidence JSON**을 생성합니다(`map_local._build_map_prompt`). 스키마는 `{review_id, source, aspect, polarity, detail, public_detail, spoiler_risk, spoiler_terms, snippet}`이며, 한 문장이 서로 다른 항목을 평가하면 여러 evidence로 분리합니다(`content`와 `story` 분리, 멀티플레이 경계 규칙 등). `polarity`는 리뷰어의 만족도 기준이지 특성 강도 기준이 아닙니다("어렵지만 재밌다"=긍정).
6. Map 결과는 `map:{game_id}:{language}:{model}:{prompt_version}:{chunk_hash}` 키로 Redis 캐시합니다. 프롬프트가 바뀌면 `MAP_PROMPT_VERSION`을 올려 캐시를 무효화합니다. Redis 연결 실패 시 로컬 스크립트는 no-cache로 진행합니다.
7. 첫/`force` 실행은 Reduce 입력 전체를 `ai-pipeline/artifacts/reduce_payloads/keep/`에 JSON으로 보존합니다(버킷·타입별 근거, `score_anchors`, 카테고리 빈도, 플레이타임 버킷, `source_stats`).
8. **score_anchors 보강**: `enrich_anchors_with_official`(`steam_rating.py`)이 Steam appreviews `query_summary`(`language=all&purchase_type=all`)를 호출해 `steam_review_score_desc`·`steam_total_positive`·`steam_total_reviews`·`steam_recommend_ratio`를 주입합니다. 모드 B(`run_map_pipeline`)는 cloud URL로 appid를 찾아 보강하고, 모드 A(in-process)도 DB에서 appid를 직접 읽어 같은 보강을 수행합니다(`ai_service._enrich_anchors_with_official`, blocking 호출은 스레드로 격리). 따라서 증분·full 모두 공식 전체 추천률을 baseline·등급의 기준으로 씁니다. 실패하면 수집 표본 추천률을 유지(fail-soft)합니다.
9. 버킷·타입별로 묶은 근거(`_group_map_outputs_by_tags`: all/early/mid/late/critic/user)와 앵커·대표 인용을 `POST /reduce`로 전송합니다.

---

## 5. Reduce 단계와 점수 산출 (`reduce_api.py`)

Groq 클라이언트는 `GroqKeyRotator`로 감싸 429 발생 시 다음 키로 전환합니다(`GROQ_API_KEYS`). 요약은 **기능별 4회 호출**로 나뉩니다.

| 호출 | 입력 근거 | 산출물 |
|---|---|---|
| `user` | user 그룹(Steam 유저) | 유저 요약·장단점·키워드, 추천/주의 대상, 유저 점수 delta |
| `critic` | critic 그룹(Metacritic) | 평론가 요약·장단점·키워드 |
| `playtime` | early/mid/late 그룹 | 구간별 요약·장단점 |
| `final` | user+critic 합본 | 통합 한줄평·장단점·키워드, aspect delta, 종합 점수 delta |

### 5-1. 항목별(aspect) 점수 — 결정론 baseline + 검증된 LLM delta

`_compute_baseline_aspect_scores`:

- **baseline_neutral**: 감성 앵커(추천률 0~100)를 선형 매핑 → `5.0 + (anchor − 50) × 0.04`, `[2.5, 7.0]` 클램프. 게임 전반 수용도를 시작점에 반영합니다.
- **항목별 skew**: `(긍정 − 부정) / (긍정 + 부정 + 1)`에 표본 수축(`confidence = n / (n + 5)`)을 곱해 더합니다 → `score = baseline_neutral + adjusted_skew × 2.0 × confidence`, `[2.0, 9.0]` 클램프.
- **mention-polarity prior 보정**: 일부 항목은 "좋을 땐 침묵, 나쁠 때만 언급"되는 불만 주도(complaint-driven) 특성이 있어(조작감·최적화 등), 평균적인 게임도 구조적으로 음수 skew를 보입니다. 이를 그대로 두면 정상적인 게임도 해당 항목이 baseline 아래로 깔려 항상 약점으로 표시됩니다. 그래서 항목별 기대 skew(`_ASPECT_POLARITY_PRIOR`)를 빼 `adjusted_skew = skew − prior`로 **그 항목의 평균 대비**로 재중심화합니다. 평균적이면 `adjusted_skew ≈ 0` → baseline, 진짜 나쁠 때만 음수가 남아 점수가 내려갑니다. 같은 prior가 약점 라벨 게이트(`neg_dominance + prior ≥ 임계`, `_enrich_aspect_relative`)에도 적용돼, 구조적 순부정을 넘어선 과잉 부정일 때만 단독 약점으로 찍힙니다. prior는 도메인 초기값이며 `ai-pipeline/calibrate_aspect_priors.py`가 저장된 `polarity_mix`로 실측 평균을 산출해 튜닝합니다.
- **긍·부정 출처 우선순위**: Map LLM evidence의 `polarity`를 우선하고, Map이 다루지 않은 항목만 크롤러 카테고리 누적치로 폴백합니다. 과거 크롤러 태그(부정 키워드 없으면 긍정 처리)가 기준이 되어 불만 항목(예: 최적화)이 긍정으로 역전되던 문제를 교정한 것입니다.
- 데이터가 없는 항목은 점수를 지어내지 않고 누락시켜 프론트가 "데이터 부족"으로 표시합니다.

`_apply_aspect_score_deltas`: LLM이 제안한 항목별 delta는 **`[-2.0, +2.0]` 클램프 + 인용 검증**을 통과해야 적용됩니다(`aspect_delta_evidence`에 유효 `review_id` ≥1개, baseline evidence_count ≥2). 미인용/무효 id면 delta 0. 즉 **최종 점수는 코드가 결정하고 LLM은 검증된 보정값만 제안**합니다.

### 5-2. 감성 점수의 출처 분리 (유저 / 종합 / 평론가 독립 산출)

세 점수가 서로 다른 앵커·근거로 산출됩니다(`_apply_sentiment_score_delta`: 앵커 + 검증 delta `[-8,+8]`, 인용 ≥2개·표본 ≥10 필요).

| 점수 | 앵커(baseline) | delta 근거 | 저장 위치 |
|---|---|---|---|
| **종합(unified)** | Steam 공식 `query_summary` 추천률(모드 A·B 모두 enrich, 실패 시 표본) | user + critic **합본** 근거 | `game_review_summaries.sentiment_score` |
| **유저** | 동일(공식 추천률 앵커) | **Steam 유저 근거만** | `user_summaries.score` |
| **평론가** | Metacritic 평론가 평균 | delta 없음(평균 자체가 평론 점수) | `critic_summaries.score` |

따라서 평론가 여론이 유저와 갈리는 게임은 종합 점수가 유저 점수에서 분리됩니다(예: 평론가가 낮으면 종합이 유저보다 낮게 끌림). 라벨(positive/mixed/negative)은 LLM 자유 값이 아니라 **최종 점수에서 결정론적으로 도출**합니다(≥60 positive, ≤45 negative, 그 사이 mixed).

### 5-3. 종합 등급 9밴드 라벨

프론트 헤드라인의 9밴드 등급(압도적으로 긍정적 … 압도적으로 부정적)은 **계산된 감성 점수**에서 Steam 공식 컷(95/85/80/70/40/30/20/10)으로 도출합니다(`GameDetailPage.ScoreBandBadge`). 점수 자체가 Steam 추천률을 기준점으로 하므로 "점수 ↔ 라벨"의 출처가 일치합니다. Steam 공식 `query_summary`의 원본 등급·추천률·리뷰 수는 `game_review_summaries.steam_rating_desc/label/ratio/count`에 보존되어 API로 함께 내려가지만, 표시 헤드라인은 점수 기반 밴드로 통일했습니다(요약에 실제 사용된 리뷰는 ~200건이라 공식 집계 수치를 그대로 노출하면 오인 소지가 있어 분리).

### 5-4. 플레이타임 구간별 요약

초반·중반·후반을 한 번에 생성하면 마지막 구간이 빈 장단점·형식 문장으로 무너지는 경우가 있어, 장단점이 모두 빈 구간은 그 구간만 재호출하고, 재호출해도 비면 형식 문장 대신 null(데이터 부족)로 둡니다. 구간별 감성 점수·리뷰 수는 실제 추천 비율로 결정론적으로 채웁니다(`_bucket_stats`, ai_service의 `bucket_stats`).

### 5-5. 추천 / 주의 대상 (recommended_for / caution_for)

`user` 호출이 리뷰 근거 기반으로 게임별 추천 플레이어 유형과 사유를 생성해 `game_review_summaries.recommended_for_json` / `caution_for_json`에 저장합니다. 노출 전 `(review_id=N)` 근거 표기는 제거합니다. 추천이 생성되지 않으면 `appeal.py`가 빈 목록을 반환합니다.

### 5-6. 증분 요약의 누적 처리

항목 점수의 기준값(앵커)과 플레이타임 버킷 임계·점수·리뷰수는 신규 배치만이 아니라 **전체 리뷰 누적치**로 산출해 대표성을 확보합니다(`_cumulative_playtime_from_reviews`). 반면 Map은 신규 청크만 처리해 비용을 아낍니다. 일상 스케줄러 증분은 payload artifact를 저장하지 않고, 첫/`force`만 보존합니다(`AI_REDUCE_PAYLOAD_SAVE`로 제어).

### 5-7. 증분 품질 가드 (모드 A)

Map은 신규 배치만 보고 통합 요약 본문(한줄평·장단점)을 매 증분 재생성하므로, 저품질·소량 신규 배치가 기존 양질 요약을 덮을 수 있습니다. 이를 막는 세 가드(모두 env 토글, fail-soft):

- **(a) 최소 신규 건수**(`AI_MIN_NEW_REVIEWS`, 기본 8): 신규가 임계 미만이면 재요약을 건너뛰고 기존 요약을 유지합니다. **커서를 전진시키지 않아** 신규가 누적되며, 임계를 넘으면 함께 요약됩니다. 첫 요약·`force`는 예외.
- **(b) 누적 evidence 합성**(`AI_EVIDENCE_CARRY`): 직전 실행이 reduce에 쓴 grouped evidence를 Redis(`reduce_evidence:{game_id}`, 롤링 윈도우)에 캐시하고, 다음 증분의 reduce 입력에 신규 evidence와 **병합**합니다(그룹별 상한 + 중복 제거). Map은 여전히 신규만 처리(비용 유지)하되 누적 대표성을 유지해 최근 편향을 완화합니다. 모드 B(full/`force`/replay)도 이 캐시를 시드해, full 직후의 첫 증분이 prior 없이 신규-편향되는 갭을 닫습니다.
- **(c) 빈약 결과 보존**: reduce는 성공했으나 결과가 빈약(한줄평이 비었거나 센티넬, 또는 장점·단점·키워드가 모두 빈 경우)하면 기존 요약을 덮지 않고 보존하며 커서도 전진시키지 않아 다음에 재시도합니다. 기존 reduce 실패(`error_code`) 보존 가드와 짝을 이룹니다.

> 주의: 모드 B로 백필된 게임은 (b) 캐시가 비어 있어 **첫 모드-A 증분 1회**가 신규-편향될 수 있습니다(신규 ≥임계 & 비-degraded일 때). (a)/(c)가 이를 완화하며, 한 번 full/replay로 캐시를 시드하면 이후로는 닫힙니다.

---

## 6. 상태 관리: cursor · Redis cache · reduce payload artifact

| 메커니즘 | 위치 | 역할 |
|---|---|---|
| **review cursor** | `game_summary_cursor` (PK: game_id, language_code) | 마지막 처리 `external_reviews.id` 기록. 다음 실행은 그 이후만 처리. `force`는 커서 무시. |
| **Map chunk 캐시** | Redis `map:{game}:{lang}:{model}:{prompt_ver}:{hash}` | 동일 청크 재처리 방지. 프롬프트 버전으로 무효화. |
| **요약·분석 캐시** | Redis (`get_summary_cache` / `get_json_cache`) | `summary`·`playtime-analysis`·`critic-summary`·`user-summary` 응답 캐시. 파이프라인 재실행 시 무효화. |
| **가격·여론 스냅샷** | Redis `buy_signal:price:{id}` / `buy_signal:sentiment:{id}` | `price_refresher`가 채움. 구매 시그널 API가 읽음. |
| **챗봇 카탈로그** | Redis `chat:game_catalog` (TTL 300s) | 챗봇 system prompt용 게임 카탈로그. |
| **prior evidence 캐시** | Redis `reduce_evidence:{game_id}` (TTL 90일) | 다음 증분 reduce에 합칠 직전 grouped evidence(롤링). 모드 A/B가 시드(§5-7 b). |
| **reduce payload artifact** | `ai-pipeline/artifacts/reduce_payloads/keep/` | 첫/`force` Reduce 입력 보존. `--from-payload`로 Map 없이 재생성. |

---

## 7. 주요 데이터베이스 테이블 (`database/*.sql`, `models/domain.py`)

- **`games` / `game_platform_map`**: 게임 정규 레코드 + 플랫폼별 메타(`platform_meta_json`: 커버·히어로 이미지, 인기 태그, 점수). `external_game_id`는 Steam appid / Metacritic slug.
- **`external_reviews`**: 크롤한 모든 리뷰. `normalized_score_100`, `playtime_hours`, `is_recommended`, `helpful_count`, `review_categories_json`(문장 단위 카테고리·감성 태그). 유니크키 `(platform, game, source_review_key)`.
- **`game_review_summaries`**: 통합 요약 메타. `is_current=TRUE` AND `summary_type='unified'` AND `review_language IS NULL`이 활성 버전. `one_liner`, `pros/cons/keywords_json`, `aspect_sentiment_json`, `recommended_for/caution_for_json`, `sentiment_score`, `steam_rating_*`(공식 등급), 신뢰도 지표.
- **`user_summaries`**: 유저 리뷰 전용 요약·장단점·점수(Steam 근거 독립).
- **`critic_summaries`**: Metacritic 평론가 요약·점수.
- **`playtime_analyses`**: early/mid/late 구간별 요약·점수·리뷰수·버킷 임계.
- **`game_summary_cursor`**: 게임별 마지막 처리 리뷰 ID(증분).
- **`review_summary_jobs`**: 파이프라인 실행 로그(토큰 수, schema_compliance·hallucination_score·anchor_deviation 등 신뢰도 지표).
- **`game_events`**: Steam 뉴스 기반 패치·DLC·논란·세일 이벤트와 부정 비율 변화.

마이그레이션은 `database/NN_*.sql`을 파일명 순서로 적용합니다. **주의**: `docker-compose.yml`은 빈 볼륨 첫 부팅 시에만, 그리고 현재 14번까지만 마운트합니다. `15_migration_steam_rating.sql`은 기존 DB에 수동 `ALTER`가 필요합니다.

---

## 8. 주요 API 엔드포인트 (`/api/v1`)

| 라우터 | 경로 | 인증 |
|---|---|---|
| 리뷰 적재 | `POST /reviews/steam`, `POST /reviews/metacritic` | X-API-Key |
| 게임/요약 조회 | `GET /games/`, `GET /games/{id}`, `GET /games/{id}/summary` | 공개 |
| 유저/평론가/구간 | `GET /games/{id}/user-summary`, `/critic-summary`, `/playtime-analysis` | 공개 |
| 추천 대상 | `GET /games/{id}/recommendation-targets` | 공개 |
| 구매 시그널 | `GET /games/{id}/buy-signal`, `GET /games/buy-signals/bulk` | 공개 |
| 챗봇 / 번역 | `POST /chat/recommend`, `POST /translate/batch` | 공개 |
| 파이프라인 | `GET /games/{id}/reviews-for-map`, `POST /games/{id}/reduce`, `POST /games/{id}/summarize` | X-API-Key |
| 유저·평론가 괴리 | `GET /games/{id}/divergence` | X-API-Key |
| 하이라이트/우선검증 | `GET /games/{id}/highlights`, `GET /reviews/priority/general` | X-API-Key |

> `divergence`·`highlights`는 구현돼 있으나 현재 프론트 기본 화면에서 직접 호출하지는 않는 보조 엔드포인트입니다. 프론트가 호출하는 것은 위 공개 엔드포인트 중 list/summary/playtime/critic/user/buy-signal(+bulk)/recommendation-targets/translate/chat 입니다.

API 문서: `http://localhost:8000/docs`.

---

## 9. 백엔드 서비스 구성 (`backend/app/`)

> 디렉터리 트리·라우터별 엔드포인트·서비스 내부 헬퍼·ORM 컬럼·Redis 키 표 등 **코드 단위 레퍼런스**는 [BACKEND_CHATBOT_ARCH.md](./BACKEND_CHATBOT_ARCH.md)에 별도로 정리돼 있습니다. 아래는 데이터 흐름 관점의 요약입니다.

- `main.py` — FastAPI 앱, 9개 라우터 등록(`reviews`/`summaries`/`analysis`/`chat`/`translate`/`highlights`/`buy_signal`/`divergence`/`appeal`).
- `services/ai_service.py` — 파이프라인 오케스트레이션. 증분 커서, 앵커 계산, `run_hybrid_summary_pipeline` 호출(모드 A) 또는 precomputed reduce(모드 B), 결과 upsert(`game_review_summaries`/`user_summaries`/`playtime_analyses`/`critic_summaries`), 신뢰도 지표, 캐시 무효화.
- `services/buy_signal_logic.py` — 외부 I/O 없는 순수 판정 함수(`analyze_sentiment`, `build_signal`). 리프레셔 잡과 조회 API가 공유.
- `services/chat_service.py` — 챗봇 카탈로그 구성 + Groq 호출(아래 §11).
- `services/recommendation_targets.py` — 추천 대상 정제.
- `jobs/scheduler.py` — 일일 직렬 잡(가격·여론 → 증분 크롤 → AI 배치), 실패 격리.
- `jobs/price_refresher.py` — Steam 가격·히스토그램 여론을 Redis 스냅샷으로 적재(일 1회 17:05 UTC).
- `jobs/review_crawler_job.py` — Steam 최신순 얕은 증분 크롤 → ingestion 자기호출.
- `jobs/ai_batch.py` — 게임별 증분 요약 배치.
- `core/` — async DB 엔진(`database.py`), Redis 헬퍼(`redis_client.py`), API 키 인증(`auth.py`).

### 구매 시그널 판정 (`buy_signal_logic.build_signal`)

`is_good_timing = 할인 중 AND 긍정 회복(positive_recovery) AND 가격 스냅샷 신선`. 월별 여론은 직전 달 대비 부정 비율 변화 ±20%p로 `positive_recovery`/`negative_spike`/`stable`을 판정합니다. 가격 스냅샷이 28시간을 넘으면(`PRICE_STALE_SECONDS`) 할인을 단정하지 않고 "스토어 확인 권장"으로 낮춥니다. 조회 API(`buy_signal.py`)는 **Redis 스냅샷만 읽어** Steam 직접 호출이 없습니다(레이트리밋 노출 0). 결과는 `buy_signal:result:{id}`로 경량 캐시합니다.

---

## 10. 프론트엔드 화면 구성 (`frontend/src/`)

라우팅(`App.jsx`): `/`(목록), `/games/:id`(상세), `/compare`(비교) + 전역 `ChatBot` 오버레이. API 베이스는 `VITE_API_BASE`(빈 값이면 nginx `/api` 프록시).

- **`GameListPage`** (`/`): 게임 카드 그리드, 태그(장르) 필터, 구매 시그널 bulk 조회(`/buy-signals/bulk`).
- **`GameDetailPage`** (`/games/:id`): 한 화면에서 7개 데이터원을 병렬 fetch(`/games/{id}`, `/summary`, `/playtime-analysis`, `/critic-summary`, `/user-summary`, `/buy-signal`, `/recommendation-targets`).
  - **종합 평가**: 점수 + 9밴드 등급 배지(점수 기반 도출).
  - **항목별 레이더**: 공통 5축(`content·gameplay·graphics·controls·optimization`) 고정. 면적=점수 절대 크기, 색·라벨=게임 내 상대 강·약점. 데이터 부족 축은 중앙 함몰. `story·difficulty·sound·price_value`는 근거·기준점 차이가 충분할 때만 방향 문구로 노출.
  - **유저/평론가 요약 카드**, **플레이타임 구간 카드**, **이런 사람에게 추천/주의**, **플랫폼별 대표 리뷰**(도움 수 높은 순, 비한국어는 `/translate/batch`로 번역), **구매 타이밍 시그널**.
- **`GameComparePage`** (`/compare`): 여러 게임의 요약·점수·플레이타임을 나란히 비교.

---

## 11. 챗봇 기능 구조 (보조 기능)

챗봇은 **핵심 파이프라인이 아니라 위에서 생성한 요약 DB를 활용하는 보조 사용자 기능**입니다.

- **`frontend/src/ChatBot.jsx`**: 전역 오버레이. `POST /api/v1/chat/recommend`로 대화 메시지를 보냅니다. `AbortController`로 언마운트 시 진행 중 요청을 취소합니다.
- **`backend/app/api/v1/chat.py`**: 입력 검증(메시지 ≤20개, 메시지당 ≤1000자, role 화이트리스트) + **IP 기준 분당 10회 rate limit**(Redis `chat_rate:{ip}`). 타임아웃/429/설정 오류를 각각 504/502/503으로 매핑.
- **`backend/app/services/chat_service.py`**: 추천 생성.

**챗봇이 사용하는 데이터 근거 (단순 게임명 매핑이 아님)** — `build_game_catalog`가 DB에서 게임별로 다음을 모아 system prompt에 카탈로그로 주입합니다.

- `game_platform_map.platform_meta_json`의 **Steam 태그**(상위 5개)
- `game_review_summaries`의 **한줄평·장점·단점·키워드·추천 대상·주의 대상·Steam 추천률**

즉 챗봇은 요약 파이프라인이 만든 구조화 데이터(한줄평, 장단점, 추천 대상 등)를 그대로 근거로 사용합니다. system prompt는 "목록에 없는 게임은 존재하지 않는 것으로 취급", "외부 학습 지식으로 게임을 설명·평가 금지", "게임 추천 외 주제(정치·뉴스·코딩 등) 거절"을 강제해 환각과 범위 이탈을 차단합니다. 카탈로그는 Redis에 5분 캐시(`chat:game_catalog`), Groq 호출은 모듈 레벨 싱글턴 `GroqKeyRotator`로 키를 로테이션합니다. 모델은 `GROQ_TRANSLATE_MODEL`(없으면 `GROQ_MODEL`)을 사용합니다.

> 한계: 챗봇 추천 품질은 카탈로그(요약 DB)의 채움 정도에 종속됩니다. 요약이 없는 게임은 태그만으로 다뤄지며, 의미 임베딩 기반 유사도 검색이 아니라 LLM이 텍스트 카탈로그를 읽고 판단하는 방식입니다.

> 챗봇 요청 흐름(rate limit → 카탈로그 빌드 → Groq 호출)·시스템 프롬프트 가드레일·`ChatBot.jsx` UI 상태 등 **코드 단위 상세**는 [BACKEND_CHATBOT_ARCH.md §6](./BACKEND_CHATBOT_ARCH.md#6-챗봇-시스템)을 참고하세요.

---

## 12. 배포 구조

- **클라우드**: `docker compose up -d`로 ollama를 제외한 스택(postgres·redis·backend·scheduler·frontend·adminer)을 띄우고, `API_SECRET_KEY`·`GROQ_API_KEY(S)`를 설정한 뒤 Cloudflare 터널로 백엔드를 외부 노출합니다. Map은 `MAP_BACKEND=groq`.
- **로컬 GPU 머신**(선택): `docker-compose.map.yml`로 ollama(`gemma4:e4b`)만 띄웁니다. Reduce가 클라우드에서 처리되므로 로컬엔 Groq 키가 필요 없습니다.

  ```bash
  python run_map_pipeline.py \
    --cloud-url https://<tunnel> --api-key <API_SECRET_KEY> \
    --map-route auto --model gemma4:e4b --all
  # 증분: --game-id {id} / 전체 재처리: --game-id {id} --force
  ```

스케줄러·API는 단일 인스턴스로만 기동합니다(잡 중복 = 레이트리밋/Groq 한도 침해).

---

## 13. 환경 변수 (`.env`)

```
GROQ_API_KEY=...                # Reduce/Groq Map 단일 키
GROQ_API_KEYS=key1,key2,key3    # (선택) 여러 키. 429 시 자동 전환
GROQ_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
GROQ_TRANSLATE_MODEL=llama-3.1-8b-instant   # 번역·챗봇 모델(미설정 시 GROQ_MODEL)
MAP_BACKEND=groq                # groq(클라우드 기본) | local
GROQ_MAP_MODEL=...              # (선택) Groq Map 모델(미설정 시 GROQ_MODEL)
API_SECRET_KEY=...              # 백엔드 쓰기 엔드포인트 인증 키
INTERNAL_API_BASE=http://backend:8000   # 스케줄러 크롤 자기호출 대상
CRAWL_RECENT_PER_LANG=100       # 증분 크롤 수집 깊이(언어당)
OLLAMA_BASE_URL=http://localhost:11434   # map_backend=local 경로 전용
LOCAL_MAP_MODEL=gemma4:e4b      # 로컬 Map 기본 모델
VITE_API_BASE=                  # 비우면 nginx /api 프록시 사용
```

`.env`는 git에 포함되지 않으며 docker-compose가 `${VAR}`로 주입합니다.

---

## 14. 알려진 한계와 향후 개선점

- **마이그레이션 자동화**: compose init 스크립트는 빈 볼륨에서만 실행되고 15번이 미마운트라, 운영 DB는 수동 `ALTER`가 필요합니다. Alembic 등 마이그레이션 도구 도입이 개선 후보입니다.
- **공식 앵커 적용 범위**: Steam 공식 `query_summary` 보강은 이제 모드 A·B 모두에 적용됩니다(증분도 in-process로 enrich). 다만 Steam 호출이 실패하면 그 실행만 수집 표본 추천률로 폴백하므로, 일시적으로 표본·모집단 간 차이가 남을 수 있습니다. 또한 모드 B로 백필된 게임은 첫 증분의 prior evidence 캐시가 비어 있습니다(§5-7 주의).
- **가격 영속성**: 가격은 Redis 전용(TTL 약 30h)이라 Redis 초기화 시 일시적으로 비며 `price_refresher` 재실행이 필요합니다.
- **챗봇 검색 방식**: 텍스트 카탈로그를 LLM이 읽는 방식이라 게임 수가 늘면 프롬프트가 커집니다. 임베딩 기반 후보 선별(RAG)로 확장할 여지가 있습니다.
- **Metacritic 갱신**: 평론가 리뷰는 수동 수집이라 신작 평론 반영에 지연이 있습니다.
- **CORS/보안**: 현재 `allow_origins=["*"]`이며 공개 배포 시 도메인 제한이 필요합니다.
- **LLM 비결정성**: Map/Reduce 결과는 실행마다 흔들릴 수 있어, 점수는 결정론 baseline + 검증 delta로 안정화했고 신뢰도 지표(`review_summary_jobs`)로 모니터링합니다.
