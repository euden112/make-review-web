# Map-Reduce 파이프라인 deep-dive

이 문서는 리뷰 요약 파이프라인의 **코드 단위 내부 동작**을 다룹니다. 데이터 흐름·운영 관점의 요약은 [ARCHITECTURE.md §4·§5](./ARCHITECTURE.md)를, 서비스 소개는 [../README.md](../README.md)를 보세요. 인접 주제인 크롤러 수집은 [steam_crawling.md](./steam_crawling.md), 플레이타임 버킷 설계는 [playtime_improvement.md](./playtime_improvement.md)에 별도로 정리돼 있습니다.

전체 코드는 `ai-pipeline/ai_module/map_reduce/`(+ 진입점 `run_map_pipeline.py`)에 있습니다.

```
reviews-for-map ──▶ sampler ──▶ chunker ──▶ Map(LLM→evidence JSON) ──▶ map_schema(파싱·복구)
                                                                              │
        score_anchors 보강(Steam query_summary) ◀─────────────────────────────┤
                                                                              ▼
                                   group by tags ──▶ POST /reduce ──▶ Reduce(4-call) ──▶ 점수 산출·검증 ──▶ DB
```

---

## 1. 청킹 (`chunker.py`)

Map LLM에 리뷰를 통째로 넣지 않고 **문자 수 기준으로 잘게 자릅니다**. 핵심 상수는 `_TARGET_CHUNK_CHARS = 1400`입니다.

청크를 작게 두는 이유는 토큰 한도가 아니라 **추출 품질**입니다. 큰 청크 하나를 주면 로컬 소형 모델이 청크 안의 리뷰를 빠짐없이 evidence로 뽑지 않고 일부만 요약해, evidence가 빈약해지고 pros/cons가 부족해집니다(검증: 5500자=1청크 → 품질 게이트 미달, ~1000자=다청크 → 통과). 그래서 청크 크기는 "모델이 한 번에 성실히 훑을 수 있는 분량"으로 잡습니다.

- **num_ctx 천장 안전장치**: `OLLAMA_NUM_CTX`가 설정돼 있으면, 입력이 컨텍스트를 넘어 잘리는 것을 막기 위해 `safe_input_tokens = max(num_ctx − 1650, 320)`(스키마 지시문·후보 토큰 예약분 1650 제외)을 문자로 환산(`_CHARS_PER_TOKEN = 2.5`)한 값을 천장으로 삼고, `_TARGET_CHUNK_CHARS`와 비교해 더 작은 쪽을 씁니다. 즉 목표값은 추출 친화 크기, num_ctx는 truncation 방지 상한입니다.
- **리뷰 단위 메타 주입**: 각 리뷰는 `[review_id=123 helpful=45 playtime=12h] 본문\n` 형태로 청크에 들어갑니다. `review_id`가 본문과 함께 들어가야 LLM이 evidence에 그 id를 정확히 인용할 수 있습니다(grounding의 출발점).
- **overlap**: 청크 경계에서 직전 `overlap_reviews = 2`개 리뷰를 다음 청크 앞에 다시 붙여, 경계에 걸친 맥락이 끊기는 것을 줄입니다.

청크 분할은 결정론적이라(같은 입력 → 같은 청크), Redis 캐시 키의 안정성을 보장합니다(§7).

---

## 2. 샘플링 (`sampler.py`)

리뷰가 수천 개여도 Map에는 **층화 추출한 표본**만 들어갑니다. 목표 수는 `AI_SUMMARY_REVIEW_TARGET`(기본·상한 200, 하한 12)입니다.

### 2-1. 전처리 필터

1. **언어 필터** (`_apply_language_filter`): Steam 리뷰는 `_ALLOWED_STEAM_LANGS = {english, koreana}`만 통과합니다(통합 게이머 관점 일관성 + 소형 모델이 안정적으로 처리하는 언어로 한정). Metacritic은 그대로 둡니다. 단 필터 후 `_MIN_AFTER_FILTER = 50`개 미만으로 떨어지면 **원본 전체로 폴백**해 표본이 고갈되는 것을 막습니다.
2. **스팸 필터** (`rules.is_spam_review`, `SPAM_RULE_VERSION = "v3-stricter-cpu"`): 길이(30~5000자)·단어 수(≥6)·문자 반복(≥5회 연속)·URL 점유율(≥30%)·알파넘 비율(<60%)·짧은 글의 단어 유니크 비율(<0.4) 규칙으로 거릅니다. 길이 하한 30자는 소형 모델이 짧은 입력에서 형식이 무너지는 것을 막기 위한 값입니다.

### 2-2. 층화 예산 배분 (`stratified_select_reviews`)

플랫폼 비율은 **동적으로** 정합니다: `steam_budget = total_target × (필터 후 steam 수 / 전체 수)`, 나머지는 Metacritic. 각 예산을 다시 극성·점수 구간으로 쪼갭니다.

- **Steam**: 호출자가 넘긴 `steam_ratio`(추천/비추천 비율)로 pos/neg 예산을 나눕니다(`allocate`는 floor + 잔여분을 소수부 큰 키에 분배).
- **Metacritic**: `metacritic_bin_ratio`로 low(<50) / mid(50~75) / high(≥75) 예산을 나눕니다.

각 층 안에서는 `quality_score` 상위순으로 채웁니다.

### 2-3. 품질 점수 (`quality_score`)

```
playtime = min(playtime_hours, 500)            # 이상치 캡
playtime_score = w(bucket) × (playtime + 1)^0.5  # w: early 0.4 / mid 0.7 / late 0.5 / unknown 0.3
score = playtime_score + 1.2 × (helpful + 1)^0.5
```

플레이타임과 도움 수를 제곱근으로 눌러 한 축이 점수를 지배하지 못하게 하고, **균형 잡힌 mid 구간(0.7)**에 가장 큰 가중치를 줍니다(초반 과열 리뷰·후반 이상치보다 대표성이 높다는 판단). 버킷 가중치·이상치 캡의 배경은 [playtime_improvement.md](./playtime_improvement.md)에 더 있습니다.

### 2-4. 반례 근거 보강

표본이 한쪽으로 쏠려도 균형 잡힌 요약이 나오도록, **반대 근거를 최소 수만큼 강제로 포함**합니다: `min_counter_evidence = min(3, max(2, total_target // 8))`. Steam 비추천 또는 Metacritic <75점을 counter-evidence로 보고, 선택된 표본에 그 수가 부족하면 품질 점수가 낮은 비-counter 리뷰를 counter 후보로 교체합니다. 표본이 원본의 자연 분포를 그대로 반영하지는 않게 되는 trade-off가 있습니다(README §6 한계 참고).

> 플레이타임 버킷(p33/p66 경계, `MIN_REVIEWS_PER_BUCKET = 18`)은 샘플링·청킹·점수 산출에 두루 쓰여 [playtime_improvement.md](./playtime_improvement.md)에서 따로 다룹니다.

---

## 3. Map 프롬프트와 evidence 스키마 (`map_local.py`, `map_groq.py`, `map_schema.py`)

### 3-1. 프롬프트 버전

`MAP_PROMPT_VERSION = "json_v6_story_aspect_split"`(`pipeline.py`). 프롬프트를 바꾸면 이 문자열을 올려 캐시(§7)를 무효화합니다. `v6`은 `content`(콘텐츠 볼륨)와 `story`(서사·캐릭터)를 분리한 버전입니다.

### 3-2. 9개 항목(aspect)

`ALLOWED_ASPECTS`(`map_schema.py`)는 정확히 9개입니다.

```
graphics, controls, optimization, content, story, price_value, sound, gameplay, difficulty
```

이 집합에 없는 항목(예: 모델이 지어낸 `multiplayer`, `bug`)은 파싱 단계에서 **버려집니다**. 한 문장이 서로 다른 항목을 평가하면("그래픽은 좋은데 최적화가 나쁘다") 여러 evidence로 쪼갭니다.

### 3-3. evidence 항목 구조

청크마다 LLM이 내는 JSON의 핵심은 evidence 배열이며, 각 항목은 다음을 담습니다.

| 필드 | 의미 |
|---|---|
| `review_id` | 근거가 된 실제 리뷰 ID(청크에 주입된 id만 유효) |
| `source` | `steam_user` / `metacritic_user` / `metacritic_critic` |
| `aspect` | 9개 항목 중 하나 |
| `polarity` | `positive` / `mixed` / `negative` — **리뷰어 만족도** 기준(특성 강도 아님: "어렵지만 재밌다"=positive) |
| `detail` | 내부용 상세(원문 기반) |
| `public_detail` | 스포일러를 가린 공개용 문장(최대 220자) |
| `spoiler_risk` | `none` / `low` / `medium` / `high` |
| `spoiler_terms` | 가려야 할 게임 고유 용어(LLM이 지목) |
| `snippet` | 대표 인용 후보 원문 조각 |

### 3-4. 스포일러 처리

게임 고유명사(보스명·엔딩명)는 하드코딩하면 한 게임에 편향되고 확장이 안 되므로, **장르 무관 일반 카테고리**(`SPOILER_TERM_PATTERNS`: final_boss/ending/twist/death/late_area/quest_resolution)만 코드에 두고, 게임별 고유 스포일러는 LLM이 출력한 `spoiler_terms`가 담당합니다. 공개 문장은 `public_detail`을 쓰되 비어 있으면 `detail`에서 spoiler_terms를 마스킹해 생성하고 220자로 자릅니다(`_public_detail_from_item`).

---

## 4. JSON 복구 레이어 (`map_schema.py`)

소형 로컬 모델은 매번 형식이 흔들립니다. 파이프라인이 멈추지 않도록 **관용적 파싱 + 정규화**로 받아냅니다.

- **코드펜스·잡음 제거** (`safe_parse_json_object`): ```` ```json ```` 같은 펜스를 벗기고, 객체가 아니면 첫 `{`~마지막 `}` 구간만 잘라 다시 시도합니다.
- **id 화이트리스트** (`_int_list(..., allowed_ids)`): evidence의 `review_id`가 그 청크에 실제 주입된 id 집합에 없으면 버립니다 → **환각 인용 차단**. `review_ids`가 비면 청크 단위로 하드 실패(`ValueError`)시켜, 근거 없는 청크가 조용히 통과하지 못하게 합니다.
- **enum 정규화**: `polarity`는 셋 중 하나가 아니면 `mixed`로, `source`는 화이트리스트 밖이면 `steam_user`로, `spoiler_risk`는 `{none,low,medium,high}`로 강제합니다.
- **aspect 화이트리스트**: `ALLOWED_ASPECTS` 밖 항목은 드롭(단 인용 후보 `quote_candidates`의 aspect는 알 수 없을 때 `content`로 폴백).
- **수치 강제**: `source_mix`·`sentiment`의 positive/mixed/negative 등은 정수로 강제하고 누락은 0으로 채웁니다.

즉 "깨진 JSON이어도 살릴 수 있으면 정규화해서 받고, 근거 자체가 없으면(빈 evidence/빈 id) 그 청크만 실패"가 원칙입니다. 이 복구 동작은 `ai-pipeline/test_map_reduce_quality.py`의 `*_repairs_*` 테스트군이 합성 입력으로 고정합니다.

---

## 5. 그룹화와 Reduce 4-콜 (`pipeline.py`, `reduce_api.py`)

### 5-1. 태그별 그룹화

Map 결과를 `_group_map_outputs_by_tags`로 **all / early / mid / late / critic / user** 그룹으로 묶어 `POST /reduce`에 보냅니다. 각 그룹은 evidence JSON 문자열 리스트입니다. 대표 인용(`representative_quotes`)·`category_frequency`·플레이타임 버킷·`score_anchors`도 함께 전송합니다.

### 5-2. 기능별 4회 호출

Reduce는 한 번에 다 만들지 않고 **기능별로 4번** 호출합니다(`GroqKeyRotator`로 감싸 429 시 다음 키 전환).

| 호출 | 입력 그룹 | 산출물 |
|---|---|---|
| `user` | user | 유저 요약·장단점·키워드, 추천/주의 대상, 유저 점수 delta |
| `critic` | critic | 평론가 요약·장단점·키워드 |
| `playtime` | early/mid/late | 구간별 요약·장단점 |
| `final` | user+critic 합본 | 통합 한줄평·장단점·키워드, aspect delta, 종합 점수 delta |

나눈 이유: 한 호출에 모든 것을 시키면 마지막 항목(특히 후반 구간)이 형식 문장으로 무너지고, 유저 톤이 평론가 요약을 오염시킵니다. 분리하면 각 호출의 입력·책임이 좁아져 품질이 안정됩니다. 플레이타임 구간은 장단점이 모두 비면 그 구간만 재호출하고, 그래도 비면 null(데이터 부족)로 둡니다.

---

## 6. 점수 산출 — 결정론 baseline + 검증된 LLM delta

핵심 원칙: **최종 점수는 코드가 정하고, LLM은 인용이 검증된 보정값(delta)만 제안**합니다. ARCHITECTURE §5-1이 요약한 식의 유도 과정을 코드 기준으로 풉니다.

### 6-1. 항목 점수 baseline (`_compute_baseline_aspect_scores`)

```
baseline_neutral = clamp(5.0 + (anchor − 50) × 0.04, 2.5, 7.0)   # anchor = 추천률 0~100
skew            = (positive − negative) / (positive + negative + 1)
adjusted_skew   = skew − prior(aspect)        # §6-2
confidence      = n / (n + 5)                 # n = 항목 근거 수, K=5 표본 수축
score           = clamp(baseline_neutral + adjusted_skew × 2.0 × confidence, 2.0, 9.0)
```

- **표본 수축(shrinkage)**: 근거가 적은 항목은 `confidence`가 작아 skew를 baseline 쪽으로 끌어당깁니다. run마다 1~2건 차이로 점수가 크게 흔들리는 것을 막습니다.
- **출처 우선순위**: Map evidence의 `polarity`를 우선하고, Map이 다루지 않은 항목만 크롤러 카테고리 누적치로 폴백합니다(과거 크롤러 태그가 불만 항목을 긍정으로 역전시키던 문제 교정).
- **데이터 없는 항목**: 점수를 지어내지 않고 누락 → 프론트가 "데이터 부족"으로 표시.

### 6-2. mention-polarity prior (`_ASPECT_POLARITY_PRIOR`)

일부 항목은 "좋을 땐 침묵, 나쁠 때만 언급"되는 불만 주도(complaint-driven) 특성이 있어, 평균적인 게임도 구조적으로 음수 skew를 보입니다(예: `difficulty = −0.10`). 이를 빼서(`adjusted_skew = skew − prior`) **그 항목의 평균 대비**로 재중심화하면, 평균적인 게임은 `adjusted_skew ≈ 0`(baseline)이 되고 진짜 나쁠 때만 음수가 남습니다. prior는 **점수에만** 적용되고 강·약점 라벨(§6-4)에는 쓰지 않습니다. 도메인 초기값이며 `ai-pipeline/calibrate_aspect_priors.py`가 저장된 `polarity_mix`로 실측 평균을 산출해 튜닝합니다.

### 6-3. LLM delta 검증 (`_apply_aspect_score_deltas`, `_apply_sentiment_score_delta`)

LLM이 제안한 보정값은 **클램프 + 인용 검증**을 통과해야만 반영됩니다.

| delta | 범위 | 적용 조건 |
|---|---|---|
| 항목(aspect) delta | `[−2.0, +2.0]` | `aspect_delta_evidence`에 유효 `review_id` ≥1개 **AND** baseline evidence_count ≥2 |
| 감성(sentiment) delta | `[−8, +8]` | 인용 `review_id` ≥2개 **AND** 표본 ≥10 |

미인용·무효 id면 delta는 0입니다. 즉 LLM은 "어디서 봤는지"를 증명해야만 점수를 움직일 수 있습니다.

### 6-4. 항목별 강·약점 라벨 (`_enrich_aspect_relative`)

점수(0~10)와 **별개로** 각 항목에 `strength`/`weakness`/`neutral` 라벨을 답니다. 신호는 점수가 아니라 **전체 리뷰에서 집계한 언급량 + 긍정률**(`category_frequency`)입니다.

```
strength : 언급 ≥ 30  AND  긍정률 ≥ 0.92
weakness : 언급 ≥ 12  AND  긍정률 ≤ 0.78
그 외     : neutral
```

- 긍정 편향 코퍼스에서 단순 고긍정률은 거의 모든 항목이 충족하므로(0.90+), **언급량 하한**으로 자주 회자되는 정의적 특징만 강점으로 남깁니다.
- 낮은 긍정률은 드물어 진짜 불만만 약점으로 잡힙니다(예: Witcher 3 조작감 0.61).
- 카테고리→항목 매핑은 `ASPECT_KEY_MAP`. `버그`·`멀티플레이`는 9개 항목으로 안 떨어져 제외하며, 특히 `버그`는 "버그 있지만 재밌다" 식 긍정 태깅이 많아 최적화 신호를 오염시키므로 매핑하지 않습니다.

이 라벨은 레이더 축 색이 아니라 레이더 아래 **대표 강점/약점 캡션**과 보조 항목 문구에만 씁니다. 레이더 축의 색·라벨은 **점수**를 9밴드(95/85/80/70/40/30/20/10 컷)로 환산해 표시합니다. 따라서 한 항목의 "평가 강도(점수)"와 "리뷰에서 반복적으로 회자된 이슈(라벨)"가 분리됩니다. 이 설계로 전환한 배경(샘플 `polarity_mix` 방식이 명작 강점을 묻고 정상 항목을 거짓 약점으로 찍던 문제)은 ARCHITECTURE §5-1-1에 있습니다.

---

## 7. 캐시와 프롬프트 버전

- **Map chunk 캐시 키**: `map:{game_id}:{language}:{model}:{prompt_version}:{chunk_digest}`(`map_local.py`). `chunk_digest`는 청크 텍스트 해시라 같은 청크는 재처리하지 않습니다. 프롬프트를 바꾸면 `MAP_PROMPT_VERSION`이 키에 들어가므로 자동으로 캐시가 무효화됩니다. Redis 연결 실패 시 로컬 스크립트는 no-cache로 진행합니다.
- **로컬 Ollama 옵션**: `num_ctx`(기본 4096), `num_predict`(ctx≤2048이면 900, 아니면 2048). `OLLAMA_NUM_PARALLEL=1`로 슬롯을 직렬화해 실행 간 결과 흔들림을 줄입니다(`docker-compose.map.yml`).
- **요약·분석 캐시**: Reduce가 성공해 DB를 갱신하면 `summary`·`playtime-analysis`·`critic-summary`·`user-summary` 응답 캐시를 무효화합니다.

---

## 8. 모드 A/B와 Replay

Reduce는 항상 Groq에서 돌지만 Map 실행 위치는 두 갈래입니다.

| 모드 | 트리거 | Map 위치 | 용도 |
|---|---|---|---|
| **A (in-process)** | `POST /games/{id}/summarize` → `run_ai_pipeline_task` | `MAP_BACKEND`(클라우드 기본 `groq`) | 스케줄러 일일 증분 |
| **B (precomputed)** | `run_map_pipeline.py` → `POST /games/{id}/reduce` | 로컬 Ollama 또는 Groq | 첫 요약·전체 재처리 |

- **라우팅(`--map-route auto`)**: `--groq-review-threshold`(기본 80) 이상의 큰 배치(첫/`force`)는 로컬 Map으로 TPM을 피하고, 그 미만 소형 증분은 Groq Map으로 보냅니다. `local`/`groq`로 강제할 수도 있습니다.
- **payload artifact**: 첫/`force` 실행은 `/reduce` 입력 전체(버킷·타입별 근거, `score_anchors`, `category_frequency`, 플레이타임 버킷, `source_stats`)를 `ai-pipeline/artifacts/reduce_payloads/keep/game_{id}_..._{prompt_version}_{ts}.json`으로 보존합니다(`AI_REDUCE_PAYLOAD_SAVE`, 기본 `auto`).
- **Replay (`--from-payload`)**: 저장된 payload로 Map을 건너뛰고 Reduce만 다시 보냅니다. **Reduce 로직만 바꿨을 때** 비싼 Map 재실행 없이 100개 게임을 재생성할 수 있어, 점수·라벨 로직 반복 튜닝의 핵심 도구입니다.

증분 누적·품질 가드(`AI_MIN_NEW_REVIEWS`, evidence carry-forward, 빈약 결과 보존)는 모드 A의 운영 안정성 장치로 [ARCHITECTURE §5-6·§5-7](./ARCHITECTURE.md)에 정리돼 있습니다.

---

## 관련 문서

- [ARCHITECTURE.md](./ARCHITECTURE.md) — 전체 시스템 흐름·DB·API·배포(이 문서의 상위 맥락).
- [playtime_improvement.md](./playtime_improvement.md) — 플레이타임 버킷·품질 가중치 상세.
- [steam_crawling.md](./steam_crawling.md) — 크롤러 수집·전처리·카테고리 태깅.
- [PRESENTATION.md](./PRESENTATION.md) — 이 파이프라인의 품질을 외부 지표(RAGAS)·회귀 테스트로 검증한 결과.
