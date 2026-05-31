# 캡스톤4분반_7조 — 게임 리뷰 AI 요약 서비스

흩어져 있는 게임 리뷰를 AI가 대신 읽고, **무엇이 좋고 아쉬운지, 시간이 지나도 재미있는지, 평론가와 유저의 평가가 어떻게 다른지, 지금이 살 만한 때인지**를 한눈에 정리해 주는 서비스입니다. 이를 위해 LLM 요약 파이프라인을 **로컬 GPU(Map)와 클라우드 LLM(Reduce)으로 나눈** Map-Reduce 구조로 설계해 비용과 품질을 동시에 잡았습니다.

---

## 프로젝트 소개

### 1. 어떤 문제를 푸는가

게임을 사기 전에 사용자는 보통 리뷰를 찾아봅니다. 하지만 인기 게임 하나에도 리뷰가 수백, 수천 개씩 달려 있고, 길이도 내용도 제각각이라 "이 게임이 나에게 맞을까?"를 빠르게 판단하기 어렵습니다. 화면에 표시되는 평균 평점 하나만으로는, 그 게임의 장점과 단점이 무엇인지, 오래 해도 질리지 않는지, 평론가와 일반 유저의 생각이 갈리는지를 알 수 없습니다.

이 서비스는 바로 그 간극을 메우기 위해 만들어졌습니다.

| 이런 사용자에게 | 이렇게 도움이 됩니다 |
|---|---|
| 구매 전 실제 유저 평가를 빠르게 보고 싶은 게이머 | 흩어진 리뷰를 근거 기반 요약으로 정리해 보여줍니다 |
| 플레이타임에 따라 평가가 어떻게 변하는지 궁금한 사용자 | 초반·중반·후반 구간별로 나눠 평가를 보여줍니다 |
| 평론가 평가와 유저 평가의 차이를 보고 싶은 사용자 | 두 집단의 평가를 분리해 비교할 수 있게 합니다 |
| 할인이나 구매 타이밍을 고민하는 사용자 | 가격과 최근 여론을 종합한 구매 시그널을 제공합니다 |

### 2. 무엇을 제공하는가

이 프로젝트는 Steam과 Metacritic의 리뷰를 수집한 뒤, AI가 **리뷰 속 실제 근거를 바탕으로** 요약을 생성하는 게임 리뷰 AI 요약 서비스입니다. 제공하는 기능은 다음과 같습니다.

- 게임마다 한 줄 요약과 함께 장점·단점을 정리해 줍니다.
- 항목별 평가를 레이더 차트로 보여 주며, 이때 점수는 절대값이 아니라 그 게임 안에서의 상대적인 강점과 약점으로 표현합니다.
- 플레이타임을 초반·중반·후반으로 나눠 구간별 평가를 제공합니다.
- "이런 사람에게 추천"과 "주의가 필요한 사람"을 함께 안내합니다.
- 평론가 리뷰를 따로 요약해 유저 평가와 비교할 수 있게 합니다.
- 할인율과 최근 여론을 종합한 구매 타이밍 시그널을 보여 줍니다.

### 3. 무엇이 다른가 (핵심 차별점)

| 차별점 | 설명 |
|---|---|
| 근거 기반 요약 | 단순한 평균 평점이 아니라, 실제 리뷰 문장에서 근거를 추출해 장단점과 추천 대상을 구성합니다. |
| 플레이타임 분석 | "초반은 좋지만 후반에는 반복적이다"처럼, 플레이 시간에 따라 달라지는 평가를 포착합니다. |
| 유저·평론가 분리 | 유저 리뷰와 평론가 리뷰를 따로 요약하여 두 시각을 비교할 수 있습니다. |
| 비용·성능 최적화 | 토큰이 많이 드는 단계는 로컬 GPU에서, 품질이 중요한 최종 요약은 클라우드 LLM에서 처리합니다. |

### 4. 어떻게 만들었는가 (기술 챌린지)

개발 과정에서 마주한 핵심 문제와 해결 방식을 STAR(상황·과제·행동·결과) 형식으로 정리했습니다.

<details>
<summary><b>① 대량 리뷰를 LLM으로 처리할 때의 비용·토큰 한도 문제</b></summary>

- **상황** 게임 하나에도 리뷰가 수백 개 이상이어서, 전체를 클라우드 LLM에 그대로 넣으면 토큰 사용량과 비용이 크게 늘어났습니다.
- **과제** 리뷰의 핵심 근거는 유지하면서도 비용을 줄일 수 있는 요약 파이프라인이 필요했습니다.
- **행동** Map-Reduce 구조를 설계했습니다. Map 단계에서는 로컬 GPU LLM이 리뷰 묶음마다 핵심 근거(evidence)를 뽑아내고, Reduce 단계에서는 클라우드 LLM이 그 근거를 모아 최종 한국어 요약을 생성합니다.
- **결과** 토큰이 많이 드는 전처리를 로컬에서 처리해 비용 부담을 줄이면서도, 최종 요약 품질은 클라우드 LLM으로 유지할 수 있었습니다.
</details>

<details>
<summary><b>② 단순 감성 요약이 아닌 근거 기반 요약을 만드는 문제</b></summary>

- **상황** 요약이 "좋다, 나쁘다" 수준에 그치면 사용자가 구매 판단에 활용하기 어렵습니다.
- **과제** AI가 실제 리뷰 근거를 바탕으로 장점, 단점, 추천 대상을 구조화하도록 만들어야 했습니다.
- **행동** Map 단계에서 리뷰마다 `review_id`, 평가 항목(aspect), 긍정/부정(polarity), 구체적 내용(detail) 형태의 근거를 추출하고, Reduce 단계에서 이 근거들을 토대로 장단점·추천 대상·주의 대상·항목별 평가를 생성합니다.
- **결과** 단순 요약이 아니라, 실제 리뷰 근거에 기반한 설명형 요약을 제공할 수 있었습니다.
</details>

<details>
<summary><b>③ 플레이타임에 따라 달라지는 평가를 분석하는 문제</b></summary>

- **상황** 게임은 초반 경험과 후반 경험이 다를 수 있지만, 일반적인 리뷰 평균은 이런 변화를 보여 주지 못합니다.
- **과제** 플레이 시간에 따른 만족도 변화를 사용자에게 보여 줄 방법이 필요했습니다.
- **행동** Steam 리뷰의 `playtime_at_review`(리뷰 작성 시점의 플레이 시간)를 기준으로 초반·중반·후반 구간을 나누고, 각 구간을 별도로 요약하도록 파이프라인을 구성했습니다.
- **결과** 사용자는 "초반은 좋지만 후반에 반복감이 심한 게임"처럼 시간에 따라 달라지는 평가를 확인할 수 있게 되었습니다.
</details>

<details>
<summary><b>④ 크롤링 데이터와 AI 결과의 신뢰성을 관리하는 문제</b></summary>

- **상황** 리뷰 데이터는 플랫폼·언어·최신성·도움순 여부에 따라 편향될 수 있고, LLM의 결과도 실행할 때마다 흔들릴 수 있습니다.
- **과제** 다양한 리뷰를 수집하면서도 중복과 편향을 줄이고, AI 결과를 안정적으로 저장해야 했습니다.
- **행동** Steam 리뷰를 최신순과 도움순으로 함께 수집해 중복을 제거하고, 결과는 PostgreSQL에 구조화해 저장한 뒤 Redis 캐시를 무효화하여 항상 최신 요약이 반영되도록 했습니다.
- **결과** 리뷰 데이터의 대표성을 높이고, 프론트엔드에서 안정적으로 최신 분석 결과를 제공할 수 있었습니다.
</details>

---

## 기술 스택

| 영역 | 사용 기술 |
|---|---|
| Frontend | React, Vite, nginx |
| Backend | FastAPI |
| Database | PostgreSQL |
| Cache | Redis |
| AI Pipeline | Ollama 기반 로컬 LLM, Groq API |
| Infra | Docker Compose, Cloudflare Tunnel |

---

## 빠르게 실행해 보기 (로컬)

```bash
cp .env.example .env            # GROQ_API_KEY, API_SECRET_KEY 등을 채웁니다.
docker compose up -d --build                     # 백엔드 스택(API·DB·Redis·프론트엔드)
docker compose -f docker-compose.map.yml up -d   # 로컬 GPU LLM(ollama)
docker exec capstone_ollama_map ollama pull gemma4:e4b

python demo.py                  # 크롤링 → 적재 → Map-Reduce → 비교까지 한 번에 실행합니다.
```

실행 후 프론트엔드는 `http://localhost`, API 문서는 `http://localhost:8000/docs`에서 확인할 수 있습니다.

---

## 운영 모드: Map 단계를 어디서 돌릴 것인가

이 서비스의 AI 요약은 **Map(근거 추출) → Reduce(최종 요약)** 두 단계로 나뉩니다. Reduce는 항상 클라우드 LLM(Groq API)에서 실행되지만, **Map 단계는 두 가지 방식 중에서 선택**할 수 있습니다. 클라우드 백엔드에는 GPU가 없으므로, 운영 환경에서는 Map을 어디서 돌릴지가 핵심 결정 사항입니다.

Map 백엔드는 `MAP_BACKEND` 환경 변수로 정해지며, `docker-compose.yml`에서는 기본값이 `groq`입니다.

| 모드 | Map 실행 위치 | 언제 쓰는가 |
|---|---|---|
| **Groq Map** (`MAP_BACKEND=groq`) | 클라우드 백엔드 안에서 Groq API 호출 | 일상적인 증분 요약, GPU가 없는 클라우드 배포 |
| **로컬 Map** (`MAP_BACKEND=local` 또는 `run_map_pipeline.py`) | 로컬 GPU의 Ollama | 첫 요약·전체 재처리처럼 리뷰가 많아 토큰이 폭증하는 작업 |

### 모드 A — 클라우드 백엔드 단독 (기본값)

클라우드에 올린 백엔드 한 대로 수집·요약·서빙을 모두 처리하는 가장 단순한 구성입니다. Ollama가 필요 없습니다.

- **스케줄러 자동 파이프라인**: `scheduler` 컨테이너가 매일 정해진 시각(17:05 UTC)에 **① 가격·여론 갱신 → ② Steam 증분 리뷰 크롤 → ③ AI 증분 요약**을 한 타임라인으로 직렬 실행합니다. 크롤이 신규 리뷰를 적재한 뒤 요약이 그 신규분만 처리하도록 크롤을 요약 앞에 둡니다. `MAP_BACKEND=groq`이므로 Map도 Groq API로 처리됩니다.
- **증분의 보장**: 크롤은 매일 최신순으로 얕게 수집해 재전송하지만, ingestion이 `(platform, game, 리뷰키)` 유니크키로 upsert하므로 중복 리뷰는 흡수되고 신규 리뷰만 새 ID를 얻습니다. 요약 커서는 그 신규 ID만 처리합니다. 따라서 "지난번 이후"만 요약되는 흐름이 DB 계층에서 보장됩니다. 수집 깊이는 `CRAWL_RECENT_PER_LANG`(언어당, 기본 100)로 조절합니다.
- **Metacritic은 자동 크롤 제외**: Metacritic 크롤러는 브라우저(playwright) 기반으로 무겁고 평론가 리뷰가 거의 고정이라, 일일 스케줄러에서는 제외하고 필요할 때 수동으로 수집합니다.
- **수동 트리거**: 특정 게임을 즉시 요약하려면 아래처럼 호출합니다.

  ```bash
  curl -X POST "https://<클라우드주소>/api/v1/games/{game_id}/summarize" \
       -H "X-API-Key: <API_SECRET_KEY>"
  ```

이 모드에서 동작에 필요한 환경 변수는 `GROQ_API_KEY`(또는 키 로테이션용 `GROQ_API_KEYS`)와 `MAP_BACKEND=groq`입니다. 요청이 많아 Groq 무료 한도(분당 토큰·요청 수)에 걸릴 수 있으므로, 쉼표로 구분한 여러 키를 `GROQ_API_KEYS`에 넣어 두면 한도 초과(429) 시 자동으로 다음 키로 전환합니다.

### 모드 B — 로컬 GPU에서 Map, 클라우드에서 Reduce

첫 요약이나 전체 재처리처럼 리뷰 수가 많을 때는, 토큰이 많이 드는 Map을 **로컬 GPU에서 돌려 비용과 한도 부담을 피하는** 구성을 사용합니다. 로컬 머신과 클라우드 백엔드는 **Cloudflare Tunnel로 공개된 백엔드 URL을 통해** 연결됩니다.

연결 흐름은 다음과 같습니다.

```
[로컬 GPU 머신]                         [클라우드 백엔드]
run_map_pipeline.py
  │  ① GET  /api/v1/games/{id}/reviews-for-map   ← 요약할 리뷰를 내려받음
  │  ② 로컬 Ollama로 Map 단계 실행 (근거 추출)
  │  ③ POST /api/v1/games/{id}/reduce            → 추출한 근거를 보내 Reduce·저장 요청
  ▼                                              ▼
 (GPU)                                    Groq Reduce → DB 저장 → 캐시 무효화
```

실행 절차:

```bash
# 1) 로컬 GPU 머신에서 Ollama 기동 후 Map 모델 준비
docker compose -f docker-compose.map.yml up -d
docker exec capstone_ollama_map ollama pull gemma4:e4b

# 2) 클라우드 백엔드를 Cloudflare Tunnel로 외부에 노출 (클라우드 측에서 실행)
#    예: cloudflared tunnel --url http://localhost:8000  →  https://xxx.trycloudflare.com

# 3) 로컬에서 클라우드 백엔드를 향해 Map 파이프라인 실행
python run_map_pipeline.py \
  --cloud-url https://xxx.trycloudflare.com \
  --all \
  --api-key <API_SECRET_KEY>
```

주요 옵션:

| 옵션 | 설명 |
|---|---|
| `--cloud-url` | 연결할 클라우드 백엔드 주소 (Cloudflare Tunnel URL) |
| `--all` / `--game-id N` | 전체 게임 처리 / 특정 게임만 처리 |
| `--force` | 커서를 무시하고 전체 리뷰를 다시 처리 (첫 요약·재생성) |
| `--map-route auto\|local\|groq` | Map을 어디서 돌릴지. `auto`는 리뷰가 많은 배치는 로컬, 작은 증분은 Groq로 보냄 |
| `--api-key` | 클라우드 백엔드 인증 키(`API_SECRET_KEY`) |

> 백엔드 컨테이너 자체에서 로컬 Ollama를 쓰고 싶다면(백엔드와 Ollama가 같은 네트워크에 있을 때), `run_map_pipeline.py` 대신 요약 요청에 `?map_backend=local`을 붙여 한 번만 로컬로 처리할 수도 있습니다.
>
> ```bash
> curl -X POST "https://<클라우드주소>/api/v1/games/{game_id}/summarize?force=true&map_backend=local" \
>      -H "X-API-Key: <API_SECRET_KEY>"
> ```
>
> 이때 백엔드에는 `OLLAMA_BASE_URL`로 접근 가능한 Ollama가 떠 있어야 합니다.

---

## 더 알아보기

구현 세부 사항은 별도 문서로 정리해 두었습니다.

- **[기술 아키텍처와 파이프라인 상세 → docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — 데이터 흐름, 점수 산출 로직, 데이터베이스 구조, API, 배포 구성을 설명합니다.
- **[통합 패치노트 → docs/INTEGRATION_CHANGES.md](docs/INTEGRATION_CHANGES.md)** — 기능 개선 작업과 클라우드 배포 작업을 합치면서 무엇이 어떻게 바뀌었는지 정리합니다.

---

## 브랜치 전략

중간 발표 시점까지는 `main` 단일 브랜치로 운영했습니다. 기말 발표를 위한 기능 확장부터는 `feature/*` 브랜치를 분기한 뒤 Pull Request로 `main`에 병합하는 방식을 적용합니다.

```
main
├── feat/evidence-grounded-scoring   # 근거 기반 점수, 레이더, 추천, 크롤링 개선, gemma4 Map
├── feature/cloud-deploy             # API 키 인증, Groq 키 로테이션, 클라우드 분리 배포
└── integration/feat-cloud           # 위 두 작업과 main을 합친 통합 브랜치
```
