# 기술 아키텍처와 파이프라인 상세

이 문서는 개발자를 위한 기술 레퍼런스입니다. 서비스의 전반적인 소개는 [../README.md](../README.md)를 참고하세요.

---

## 1. 전체 아키텍처

전체 흐름은 "리뷰를 수집하고 → 저장하고 → AI로 요약하고 → 사용자에게 보여 주는" 네 단계로 이루어집니다. 이때 AI 요약은 토큰이 많이 드는 **Map 단계를 로컬 GPU에서**, 품질이 중요한 **Reduce 단계를 클라우드 LLM에서** 처리하도록 나눈 것이 핵심입니다.

```
[크롤러] Steam(ko/en/zh) / Metacritic(critic)
   │  최신순 + 도움순 혼합 수집, 인기 태그 함께 수집
   ▼
[send_to_api] ──X-API-Key──▶ [백엔드 /reviews/*] ──▶ PostgreSQL(external_reviews)
                                                          │
[로컬 GPU 머신]                                            │ reviews-for-map
  run_map_pipeline.py                                     ▼
   │ 1) GET /reviews-for-map (커서 기반 증분)  ◀──── [백엔드(클라우드)]
   │ 2) Map: 로컬 ollama gemma4:e4b 가 리뷰 묶음별 근거 추출
   │    └─ Redis chunk cache + 첫/force payload artifact 보존
   │ 3) POST /reduce (모은 근거 전송) ───────────▶  Reduce: Groq API(scout) + 키 로테이션
   ▼ (Cloudflare 터널을 통해 클라우드에 도달)          ▼
                                          game_review_summaries / playtime_analyses
                                          / critic_summaries 저장 + Redis 캐시 무효화
   ▼
[프론트엔드(React/nginx)] ──GET(공개)──▶ 목록·상세·레이더·장르 칩·추천·구매 시그널
```

**Map과 Reduce를 나눈 이유**

- **Map(로컬 GPU)**: 리뷰 전체를 다루므로 토큰이 가장 많이 드는 단계입니다. 이를 로컬 ollama에서 처리하면 Groq의 분당 토큰 한도(TPM)와 비용 부담을 피할 수 있습니다. 모델은 `gemma4:e4b`를 사용하는데, A/B 테스트 결과 `qwen2.5:7b`보다 약 18% 빠르면서 JSON 형식 준수도 더 안정적이었습니다.
- **Reduce(클라우드 Groq)**: 한국어로 자연스럽게 종합·구조화하는 단계로, 품질이 중요하므로 클라우드의 `llama-4-scout` 모델을 사용합니다.
- **라우팅(`--map-route auto`)**: 첫 요약이나 전체 재처리처럼 양이 많은 작업은 로컬 Map으로, 새 리뷰만 처리하는 소규모 증분은 Groq Map으로 보냅니다. 덕분에 로컬 GPU가 없어도 클라우드만으로 증분 갱신이 가능합니다.

---

## 2. 서비스 구성 (Docker)

백엔드 스택은 `docker-compose.yml`에 정의되어 있으며, 다음 서비스로 구성됩니다.

- **postgres (5432)**: PostgreSQL 15. `database/` 아래의 SQL 파일이 첫 부팅 시 자동 적용됩니다.
- **redis (6379)**: 요약·가격·여론 스냅샷을 캐싱합니다.
- **backend (8000)**: FastAPI 서버로 요약·분석·추천·구매 시그널·챗봇·번역 기능을 제공합니다.
- **scheduler**: 가격·여론 스냅샷 갱신 같은 주기 작업을 수행합니다.
- **frontend (80)**: React/Vite를 nginx로 정적 서빙하며 `/api`를 프록시합니다.
- **adminer (8888)**: 데이터베이스 관리 UI입니다.

로컬 Map 전용 ollama는 `docker-compose.map.yml`에 **별도로** 정의되어 있습니다. GPU를 사용하며, flash attention과 KV 캐시 8bit 양자화로 추론 속도를 높이고, `OLLAMA_NUM_PARALLEL=1`로 요청을 직렬화해 결과의 결정성을 확보합니다.

클라우드에 배포할 때는 `docker-compose.yml`(GPU 없음)을 클라우드에 두고, `docker-compose.map.yml`은 로컬 GPU 머신에 둡니다. 로컬 머신은 `run_map_pipeline --cloud-url https://<cloudflare-tunnel>` 형태로 클라우드 백엔드에 접속합니다.

---

## 3. 데이터 흐름

### 3-1. 크롤링 (`crawling/`)

- **Steam**: 언어(한국어/영어)별로 최신순과 도움순(전체 기간 기준)을 함께 수집한 뒤, 이미 수집한 리뷰는 `seen` 집합으로 걸러 중복을 제거합니다. 기본 비중은 도움순을 우세하게 두어(`RECENT_PER_LANG=80`, `HELPFUL_PER_LANG=120`) 내용이 충실한 핵심 리뷰를 우선 확보합니다. 리뷰마다 작성 시점 플레이 시간(`playtime_at_review`), 도움 수(`helpful_count`), 문장 단위 카테고리 태그를 함께 모읍니다. 또한 게임의 **인기 태그(장르)** 는 상점 페이지 HTML의 `InitAppTagModal` 데이터를 파싱해 상위 8개를 가져옵니다.
- **Metacritic**: `game_list.json`에 정의된 `metacritic_slug`를 기준으로 평론가(critic) 리뷰를 영어로 수집합니다.
- **적재 (`send_to_api.py`)**: 수집한 리뷰를 `/api/v1/reviews/{platform}`으로 전송합니다. 이 엔드포인트는 쓰기 작업이므로 `X-API-Key`가 필요합니다. 점수는 `normalized_score_100`(0–100)으로 정규화해 저장합니다.

### 3-2. Map 단계 (`run_map_pipeline.py`, `map_local.py`)

1. `GET /reviews-for-map`을 호출해 커서(`game_summary_cursor`) 이후의 새 리뷰만(증분) 또는 전체(`force`)를 가져옵니다.
2. `sampler.py`가 점수 분포를 고려해 리뷰를 추려 냅니다(총 200개 목표).
3. Steam 리뷰의 `playtime_at_review` 분포에서 p33·p66 백분위를 구해 초반·중반·후반 경계를 정합니다. 백분위 기반이라 항상 약 3등분되며 극단값에 흔들리지 않습니다. 리뷰가 30개 미만이면 버킷을 만들지 않습니다.
4. 초반·중반·후반을 따로 묶어 청크로 나눕니다(버킷별 청킹). 이렇게 하면 각 구간의 근거가 누락되지 않습니다.
5. 청크마다 LLM이 `{review_id, aspect, polarity, detail}` 형태의 근거(evidence)를 추출합니다. 라우팅 정책에 따라 로컬 gemma4 또는 Groq를 사용하며, 결정성을 위해 순차로 처리합니다(`MAP_CONCURRENCY=1`).
6. Map 결과는 `map:{game_id}:{language}:{model}:{prompt_version}:{chunk_hash}` 키로 Redis에 저장합니다. 백엔드 내부 실행과 `run_map_pipeline.py` 모두 같은 키 전략을 쓰며, Redis 연결 실패 시 로컬 스크립트는 no-cache로 계속 진행합니다.
7. 첫 요약 또는 `force` 전체 재처리에서는 Reduce 입력 전체를 `ai-pipeline/artifacts/reduce_payloads/keep/`에 JSON artifact로 보존합니다. 이 파일은 버킷·타입별 Map 근거, 점수 기준값(score anchors), 카테고리 빈도, 플레이타임 버킷, `source_stats`를 포함합니다.
8. 버킷·타입별로 묶은 근거와 점수 기준값(score anchors), 카테고리 빈도를 `POST /reduce`로 전송합니다. 저장된 artifact는 `run_map_pipeline.py --from-payload <파일>`로 Map 없이 Reduce만 다시 실행하는 데 사용할 수 있습니다.

### 3-3. Reduce 단계 (`reduce_api.py`, 백엔드에서 실행)

Groq 클라이언트는 `GroqKeyRotator`로 감싸 두어, 429(요청 초과)가 감지되면 다음 키로 자동 전환합니다(`GROQ_API_KEYS`에 여러 키를 둘 수 있습니다). 요약은 기능별로 나눠 호출합니다. `user`(유저 요약과 "이런 사람에게 추천"), `critic`(평론가 요약), `playtime`(구간별 요약), `final`(통합 요약)이 그것입니다. 생성된 결과는 검증·정제를 거쳐 `game_review_summaries`, `playtime_analyses`, `critic_summaries`에 저장하고, 관련 Redis 캐시를 무효화합니다.

---

## 4. 점수와 요약 산출 로직

### 4-1. 항목별(aspect) 점수

기준점(baseline)은 게임의 추천률 앵커에서 도출하고, 항목마다 `skew = (긍정 − 부정) / (긍정 + 부정 + 1)`에 표본 수축(shrinkage)을 적용해 더합니다. 이때 긍정/부정 판정은 **Map LLM이 뽑은 근거의 polarity를 우선** 사용하고, Map이 다루지 않은 항목만 크롤러의 카테고리 누적치로 보완합니다. 과거에는 크롤러 카테고리 태그(부정 키워드가 없으면 긍정으로 처리하는 방식)가 기준이 되어, 불만이 많은 항목(예: 최적화)이 오히려 긍정으로 집계되어 점수가 역전되는 문제가 있었는데 이를 교정한 것입니다. 최종 점수는 코드가 산출하며, LLM은 인용 근거가 검증된 ±2.0 범위의 보정값(delta)만 제안합니다. 절대 기준이 없으므로, 프론트엔드는 이를 게임 내 상대적인 강점과 약점으로 표현합니다.

### 4-2. 플레이타임 구간별 요약

초반·중반·후반을 한 번의 LLM 호출로 함께 생성하면, 근거가 충분한데도 마지막 구간(주로 후반)이 빈 장단점과 형식적인 문장으로 무너지는 경우가 있었습니다. 이를 보정하기 위해, 장점과 단점이 모두 비어 있는 구간은 그 구간만 따로 다시 호출하고, 재호출해도 비어 있으면 형식적인 문장 대신 null(데이터 부족)로 둡니다. 각 구간의 감성 점수와 리뷰 수는 실제 추천 비율을 바탕으로 코드가 결정론적으로 산출합니다.

### 4-3. "이런 사람에게 추천" (recommended_for)

유저 요약 단계에서 LLM이 리뷰 근거를 바탕으로 게임별 추천 플레이어 유형과 그 사유를 생성하고, 이를 `game_review_summaries.recommended_for_json`에 저장합니다. 추천 문구는 사용자에게 그대로 노출되므로, 근거 추적용으로 들어간 `(review_id=N)` 표기는 저장 시 제거합니다. 과거에는 카테고리별 고정 문구를 사용해 모든 게임의 추천이 똑같았는데, 이를 실제 데이터 기반으로 교체했습니다. 만약 추천이 생성되지 않으면 `appeal.py`가 카테고리 추론 방식으로 대체합니다.

### 4-4. 증분 요약의 누적 처리

항목 점수의 기준값과 플레이타임 버킷은 새로 들어온 배치만이 아니라 **전체 리뷰 누적치**를 기준으로 산출해 대표성을 확보합니다. 반면 Map은 새 청크만 처리하여 비용을 아낍니다.

일반 스케줄러 증분 요약은 JSON artifact를 저장하지 않습니다. 단, 커서가 없는 게임의 첫 요약이나 수동 `force` 재처리는 재실행 비용이 큰 Map 입력을 보존하기 위해 artifact를 저장합니다. 운영 환경에서 이 저장을 완전히 끄려면 `AI_REDUCE_PAYLOAD_SAVE=false`를 설정합니다.

---

## 5. 주요 데이터베이스 테이블 (`database/*.sql`)

- **`games` / `game_platform_map`**: 게임의 정규 레코드와 플랫폼별 메타데이터를 담습니다. 메타데이터(`platform_meta_json`)에는 커버 이미지, 히어로 이미지, 인기 태그(`tags`)가 포함됩니다.
- **`external_reviews`**: 크롤링한 모든 리뷰를 저장합니다. `normalized_score_100`, `playtime_hours`, `review_categories_json`, `helpful_count` 등을 함께 보관합니다.
- **`game_review_summaries`**: AI가 생성한 통합 요약입니다. `is_current=TRUE`인 행이 현재 활성 버전이며, 추천 데이터(`recommended_for_json`, `caution_for_json`)는 마이그레이션 14에서 추가되었습니다.
- **`playtime_analyses`**: 초반·중반·후반 구간별 요약과 점수, 리뷰 수를 담습니다.
- **`critic_summaries`**: Metacritic 평론가 리뷰 요약입니다.
- **`game_summary_cursor`**: 게임별로 마지막으로 처리한 리뷰 ID를 기록해 증분 처리에 사용합니다.
- **`review_summary_jobs`**: 파이프라인 실행 로그로, 토큰 수와 신뢰도 지표를 남깁니다.

마이그레이션 파일은 `docker-compose.yml`에서 `database/NN_*.sql`을 초기화 스크립트로 하나씩 마운트하며, 첫 부팅 시 파일 이름 순서대로 적용됩니다.

---

## 6. API 엔드포인트 (`/api/v1`)

| 라우터 | 경로 | 인증 |
|---|---|---|
| 리뷰 적재 | `POST /reviews/{steam\|metacritic}` | 필요 (X-API-Key) |
| 요약 조회 | `GET /games/`, `GET /games/{id}/summary`, `/user-summary` | 공개 |
| 파이프라인 | `GET /games/{id}/reviews-for-map`, `POST /games/{id}/reduce`, `POST /games/{id}/summarize` | 필요 (X-API-Key) |
| 분석 | `GET /games/{id}/playtime-analysis`, `/critic-summary` | 공개 |
| 추천 대상 | `GET /games/{id}/recommendation-targets` | 공개 |
| 구매 시그널 | `GET /games/{id}/buy-signal` | 공개 |
| 유저/평론가 괴리 | `GET /games/{id}/divergence` | 공개 |
| 챗봇 / 번역 | `POST /chat/recommend`, `POST /translate/batch` | 공개 |

API 문서는 `http://localhost:8000/docs`에서 확인할 수 있습니다.

---

## 7. 환경 변수 (`.env`)

```
GROQ_API_KEY=...                # Reduce와 Groq Map에 사용하는 단일 키
GROQ_API_KEYS=key1,key2,key3    # (선택) 여러 키. 429 발생 시 자동으로 다음 키로 전환합니다.
GROQ_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
API_SECRET_KEY=...              # 백엔드 쓰기 엔드포인트 인증 키
LOCAL_MAP_MODEL=gemma4:e4b      # 로컬 Map 기본 모델
OLLAMA_BASE_URL=http://localhost:11434
VITE_API_BASE=                  # 비워 두면 nginx의 /api 프록시를 사용합니다.
```

`.env`는 git에 포함되지 않으며, docker-compose가 `${VAR}` 형태로 값을 컨테이너에 주입합니다.

---

## 8. 분리 배포 (클라우드 백엔드 + 로컬 GPU Map)

- **클라우드**: `docker compose up -d`로 ollama를 제외한 스택을 띄우고, `API_SECRET_KEY`와 `GROQ_API_KEY(S)`를 설정한 뒤 Cloudflare 터널로 외부에 노출합니다.
- **로컬 GPU 머신**: ollama(gemma4:e4b)만 띄우면 되며, Reduce가 클라우드에서 처리되므로 Groq 키는 필요하지 않습니다.

  ```bash
  python run_map_pipeline.py \
    --cloud-url https://<tunnel> --api-key <API_SECRET_KEY> \
    --map-route auto --model gemma4:e4b --all
  ```

  `--map-route auto`를 쓰면 첫·대규모 작업은 로컬 Map으로, 소규모 증분은 Groq Map으로 자동 분배됩니다. 따라서 로컬 GPU가 꺼져 있어도 클라우드 단독으로 증분 갱신을 처리할 수 있습니다.

요약을 직접 트리거하려면 다음과 같이 실행합니다.

```bash
# 증분 처리
python run_map_pipeline.py --cloud-url <url> --api-key <키> --game-id {id}
# 전체 재처리 (커서 무시)
python run_map_pipeline.py --cloud-url <url> --api-key <키> --game-id {id} --force
```

---

## 9. 프론트엔드 (`frontend/src/`)

- **`GameListPage`**: 게임 목록을 그리드로 보여 주며, Steam 인기 태그를 이용한 장르 필터와 평점 정렬을 제공합니다.
- **`GameDetailPage`**: 게임 상세 화면으로, 다음 요소를 보여 줍니다.
  - 상단에 Steam 인기 태그를 장르 칩으로 표시합니다.
  - 카테고리 레이더는 절대 점수가 아니라 게임 내 상대 능력치로 그립니다. min-max로 펴되 과장을 막기 위해 하한(floor)을 두고, 축마다 강점·보통·약점 등급을 표시하며, 데이터가 부족한 축은 중앙으로 함몰시킵니다.
  - 유저·평론가 요약, 장단점, 리뷰 토픽(LLM이 추출한 가변 토픽으로 장르 칩과 구분됩니다), 플레이타임 구간별 카드, 대표 리뷰(도움 수가 높은 리뷰 우선), 추천 대상, 구매 타이밍 시그널을 함께 제공합니다.
