# Game Review Aggregator - Backend

FastAPI 기반 백엔드입니다. Steam/Metacritic 리뷰 적재, 게임 목록/상세 조회, AI 요약 파이프라인 트리거, 플레이타임/평론가/유저 요약 조회, 구매 시그널, 추천 대상, 챗봇, 번역 API를 제공합니다.

## 역할

- `POST /api/v1/reviews/{steam|metacritic}`로 크롤링 결과를 수신하고 PostgreSQL에 upsert합니다.
- `POST /api/v1/games/{game_id}/summarize`로 백엔드 내부 Map/Reduce 요약을 실행합니다.
- `GET /api/v1/games/{game_id}/reviews-for-map`와 `POST /api/v1/games/{game_id}/reduce`로 로컬 GPU Map -> 클라우드 Reduce 분리 실행을 지원합니다.
- 요약 결과를 `game_review_summaries`, `user_summaries`, `critic_summaries`, `playtime_analyses`에 나눠 저장하고 Redis 캐시를 무효화합니다.
- 쓰기/비용 발생 엔드포인트는 `X-API-Key` 인증을 요구합니다.

## 주요 디렉토리

```text
backend/
├── requirements.txt
└── app/
    ├── main.py                  # FastAPI 앱, CORS, 라우터 등록
    ├── core/
    │   ├── auth.py              # X-API-Key 인증
    │   ├── database.py          # PostgreSQL async 세션/엔진
    │   └── redis_client.py      # Redis 연결
    ├── api/v1/
    │   ├── reviews.py           # Steam/Metacritic 리뷰 적재
    │   ├── summaries.py         # 목록, 요약 조회, summarize, reviews-for-map, reduce
    │   ├── analysis.py          # 플레이타임, 평론가, 유저 상세 요약
    │   ├── buy_signal.py        # 구매 타이밍 시그널
    │   ├── appeal.py            # 추천 대상
    │   ├── divergence.py        # 유저/평론가 괴리
    │   ├── chat.py              # 추천 챗봇
    │   └── translate.py         # 번역
    ├── jobs/
    │   ├── scheduler.py         # 일일 배치 루프
    │   ├── review_crawler_job.py
    │   ├── price_refresher.py
    │   └── ai_batch.py
    ├── models/domain.py         # SQLAlchemy ORM
    └── services/                # AI, 구매 시그널, 챗봇, 추천 대상 로직
```

## 로컬 실행

루트에서 Docker Compose를 사용하는 방식이 기본입니다.

```bash
cp .env.example .env
docker compose up -d --build
```

API 문서는 `http://localhost:8000/docs`에서 확인합니다.

백엔드만 직접 실행하려면 다음처럼 실행합니다. 이 경우 PostgreSQL/Redis가 먼저 떠 있어야 합니다.

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## 필수 환경 변수

```env
DATABASE_URL=postgresql+asyncpg://postgres:password@postgres:5432/review_db
REDIS_URL=redis://redis:6379/0
GROQ_API_KEY=...
GROQ_API_KEYS=key1,key2,key3
GROQ_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
MAP_BACKEND=groq
LOCAL_MAP_MODEL=gemma4:e4b
OLLAMA_BASE_URL=http://localhost:11434
API_SECRET_KEY=...
```

`GROQ_API_KEYS`는 선택 값이지만, 여러 키를 쉼표로 넣으면 429 발생 시 다음 키로 자동 전환합니다. `MAP_BACKEND=groq`는 GPU가 없는 클라우드 기본값입니다. 로컬 Ollama Map을 백엔드에서 직접 쓰려면 `map_backend=local` 요청 파라미터와 접근 가능한 `OLLAMA_BASE_URL`이 필요합니다.

## 인증

다음 엔드포인트는 `X-API-Key: <API_SECRET_KEY>` 헤더가 필요합니다.

- `POST /api/v1/reviews/steam`
- `POST /api/v1/reviews/metacritic`
- `POST /api/v1/games/{game_id}/summarize`
- `GET /api/v1/games/{game_id}/reviews-for-map`
- `POST /api/v1/games/{game_id}/reduce`
- 운영/검증용 highlights, priority, divergence 계열 일부 엔드포인트

`API_SECRET_KEY`가 서버에 설정되어 있지 않으면 인증 엔드포인트는 500을 반환합니다.
