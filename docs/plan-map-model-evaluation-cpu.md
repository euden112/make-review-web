# CPU Map 모델 개선 테스트 시나리오

> 작성일: 2026-05-26  
> 대상: GPU 없는 학교 클라우드/로컬 CPU 환경  
> 기준 파이프라인: `json_v2_llm_map` Map JSON evidence + 기능별 Reduce  
> 관련 문서: `docs/plan-cpu-deployment.md`, `docs/plan-review-quality-pipeline.md`, `docs/review-quality-work-summary-2026-05-26.md`

## 1. 목적

현재 Map 단계 기본 모델은 `qwen2.5:1.5b`다. 이 모델은 기존 CPU 벤치마크에서 속도와 성공률이 가장 좋아 채택되었지만, 최신 요약 파이프라인은 단순 `PROS/CONS/ASPECTS/IDS` 문자열이 아니라 `evidence_items` 중심의 JSON schema를 요구한다.

따라서 이번 테스트의 목적은 다음이다.

- CPU 환경에서 현재 baseline인 `qwen2.5:1.5b`를 재측정한다.
- 2026-05 기준 Ollama에서 사용 가능한 최신 소형 모델을 같은 조건으로 비교한다.
- Map JSON 생성 성공률, repair 의존도, deterministic fallback 비율, 처리 시간을 기준으로 개선 후보를 선정한다.
- 모델 교체 여부를 감이 아니라 재현 가능한 dry-run 결과로 결정한다.

## 2. 현재 기준

### 2-1. 현재 설치/설정 상태

현재 로컬 Ollama 확인 결과:

```text
qwen2.5:1.5b
size: 986 MB
architecture: qwen2
parameters: 1.5B
context length: 32768
quantization: Q4_K_M
```

현재 compose 설정:

```yaml
LOCAL_MAP_MODEL=qwen2.5:1.5b
OLLAMA_NUM_CTX=2048
```

`OLLAMA_NUM_CTX=2048`은 CPU 환경에서 chunk 입력 크기와 출력 예약량을 보수적으로 제한하기 위한 설정이다. 모델 비교 중에도 이 값을 고정해 모델 자체의 차이를 비교한다.

### 2-2. 기존 채택 근거

`docs/plan-cpu-deployment.md` 기준 기존 벤치마크에서는 `qwen2.5:1.5b`가 가장 빨랐다.

| 모델 | 성공률 | 평균 속도 | 비고 |
|---|---:|---:|---|
| `gemma3:1b` | 2/3 | 18.35s | 한국어 청크 포맷 실패 |
| `qwen2.5:1.5b` | 3/3 | 2.37s | 채택 |
| `qwen3:1.7b` no_think | 3/3 | 7.02s | qwen2.5보다 느림 |

단, 이 결과는 구형 Map 출력 포맷 기준이다. 최신 `json_v2_llm_map` 기준에서는 별도 검증이 필요하다.

### 2-3. 최신 파이프라인의 추가 요구사항

최신 Map 단계는 다음 조건을 만족해야 한다.

- JSON object만 반환한다.
- `evidence_items`가 비어 있으면 실패다.
- `review_id`는 chunk 내부 ID만 사용한다.
- `snippet`은 원문에서 확인 가능한 텍스트여야 한다.
- `aspect`는 허용 목록 안에 있어야 한다.
- `detail`은 `"전투가 좋다"` 같은 일반론이 아니라 구체 상황을 포함해야 한다.
- 공개 출력 후보가 될 `public_detail`은 특정 보스명, 엔딩명, 반전, 캐릭터 사망, 후반 지역명, 퀘스트 결말을 직접 노출하지 않아야 한다.
- 좋은 모델은 raw evidence를 보존하면서도 공개용 표현을 스포일러 안전하게 추상화할 수 있어야 한다.
- LLM 출력이 깨져도 schema repair가 가능해야 하며, deterministic fallback에 과도하게 의존하면 안 된다.

## 3. 후보 모델

### 3-1. 1차 후보

| 우선순위 | 모델 | 예상 크기 | 테스트 이유 |
|---:|---|---:|---|
| baseline | `qwen2.5:1.5b` | 986MB | 현재 운영 기준. 모든 결과의 비교 기준 |
| 1 | `qwen3.5:2b` | 약 2.7GB | 최신 Qwen3.5 소형 라인업. CPU 부담과 품질의 균형 후보 |
| 2 | `qwen3.5:4b` | 약 3.4GB | JSON evidence 품질 개선 가능성이 가장 큰 1차 후보 |
| 3 | `phi4-mini:3.8b` | 약 2.5GB | instruction following, function/tool 지원, 다국어 성능 기대 |
| 4 | `qwen3:4b` | 약 2.5GB | Qwen3 계열 비교 기준. Qwen3.5와 세대 차이 확인 |
| 5 | `gemma3:4b-it-qat` | 약 3GB대 | QAT 기반 메모리 절감 후보. 다국어/요약 비교용 |
| 6 | `llama3.2:3b` | 약 2.0GB | 3B급 일반 instruction 모델 비교 기준 |

### 3-2. 후순위 또는 제외 후보

| 모델 | 판단 | 이유 |
|---|---|---|
| `qwen3.5:0.8b` | 후순위 | 너무 작아 baseline 대비 품질 개선 가능성이 낮음. 초저사양 fallback 후보 |
| `qwen3.5:9b` | 조건부 | CPU에서 가능은 하지만 batch Map 단계에는 느릴 가능성이 큼 |
| `qwen3.6:27b`, `qwen3.6:35b` | 제외 | 17GB 이상으로 CPU Map 모델 후보로 과함 |
| Mistral Small 4 | 제외 | 119B total / 6.5B active. CPU-only Map extractor로 부적합 |
| Gemma 4 E2B/E4B | 별도 실험 | 2026 최신 후보지만 현재 Ollama 태그/호환성 확인 후 별도 트랙에서 검증 |
| `smollm2:1.7b` | 낮은 우선순위 | 8K context와 낮은 추출 품질 우려 |

## 4. 테스트 전 준비

### 4-1. 컨테이너 상태 확인

```bash
docker compose ps
docker exec capstone_ollama ollama --version
docker exec capstone_ollama ollama list
docker exec capstone_backend printenv LOCAL_MAP_MODEL
docker exec capstone_backend printenv OLLAMA_NUM_CTX
```

기대값:

- `capstone_ollama` 실행 중
- `capstone_backend` 실행 중
- `OLLAMA_NUM_CTX=2048`
- baseline `qwen2.5:1.5b` 설치됨

`backend`가 `uvicorn --reload`의 `__pycache__` 스캔 문제로 종료되면, 테스트용으로 reload 없는 backend 실행 방식을 별도 적용한다.

### 4-2. 후보 모델 pull

1차 후보만 먼저 받는다.

```bash
docker exec capstone_ollama ollama pull qwen3.5:2b
docker exec capstone_ollama ollama pull qwen3.5:4b
docker exec capstone_ollama ollama pull phi4-mini:3.8b
docker exec capstone_ollama ollama pull qwen3:4b
docker exec capstone_ollama ollama pull gemma3:4b-it-qat
docker exec capstone_ollama ollama pull llama3.2:3b
```

모델 pull 후 크기와 양자화 정보를 기록한다.

```bash
docker exec capstone_ollama ollama list
docker exec capstone_ollama ollama show qwen3.5:2b
docker exec capstone_ollama ollama show qwen3.5:4b
docker exec capstone_ollama ollama show phi4-mini:3.8b
```

## 5. 테스트 시나리오

### 5-1. 시나리오 A: 단일 게임 smoke test

목적:

- 모델이 현재 Map JSON prompt를 실행 가능한지 빠르게 확인한다.
- malformed JSON, 빈 evidence, timeout 같은 즉시 탈락 조건을 잡는다.

대상:

- 리뷰 수가 충분하고 기존 검증 기록이 있는 `ELDEN RING`

명령 예시:

```bash
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=qwen2.5:1.5b python /workspace/ai-pipeline/dry_quality_run.py --games 1 --review-limit 36 --assert-gates"
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=qwen3.5:2b python /workspace/ai-pipeline/dry_quality_run.py --games 1 --review-limit 36 --assert-gates"
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=qwen3.5:4b python /workspace/ai-pipeline/dry_quality_run.py --games 1 --review-limit 36 --assert-gates"
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=phi4-mini:3.8b python /workspace/ai-pipeline/dry_quality_run.py --games 1 --review-limit 36 --assert-gates"
```

통과 조건:

- exit code 0
- deterministic fallback rate 0.0
- Map LLM success rate 0.8 이상
- Reduce error 없음
- total tokens가 기존 예산을 크게 초과하지 않음

즉시 탈락 조건:

- 모델 호출 timeout 반복
- `map_deterministic_fallback_chunks` 발생
- `evidence_items` 공백으로 gate 실패
- 원문 밖 `review_id` 생성이 repair로도 복구되지 않음

### 5-2. 시나리오 B: 현재 DB 전체 dry-run

목적:

- 현재 보유 DB 전체에서 모델이 안정적으로 통과하는지 확인한다.
- 현재 DB에 리뷰 보유 게임이 5개 미만이면 모든 게임을 대상으로 한다.

명령 예시:

```bash
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=qwen2.5:1.5b python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 36 --assert-gates"
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=qwen3.5:2b python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 36 --assert-gates"
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=qwen3.5:4b python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 36 --assert-gates"
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=phi4-mini:3.8b python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 36 --assert-gates"
```

확인 항목:

- 게임별 chunks 수
- 게임별 Map LLM success rate
- deterministic fallback rate
- Reduce requests
- Reduce input/output tokens
- gate result
- 출력 근거 예시의 구체성

### 5-3. 시나리오 C: 확장 리뷰 수 테스트

목적:

- chunk 수가 늘어날 때 CPU 시간이 감당 가능한지 확인한다.
- JSON 출력 안정성이 긴 입력에서도 유지되는지 본다.

명령 예시:

```bash
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=qwen3.5:2b python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 72 --assert-gates"
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=qwen3.5:4b python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 72 --assert-gates"
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=phi4-mini:3.8b python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 72 --assert-gates"
```

통과 조건:

- fallback rate 0.0 유지
- 평균 chunk 처리 시간이 baseline 대비 2.5배 이내면 우수
- 4B급 모델은 baseline 대비 4배 이내면 조건부 허용
- output evidence가 더 구체적이어야 속도 저하를 정당화할 수 있음

### 5-4. 시나리오 D: CPU 운영 시간 추정

목적:

- 학교 클라우드에서 일일 배치가 가능한지 추정한다.

계산식:

```text
estimated_map_time_per_game =
  average_chunk_latency_sec * average_chunks_per_game

estimated_daily_map_time =
  estimated_map_time_per_game * target_games_per_day
```

판정 기준:

| 등급 | 조건 |
|---|---|
| A | 50게임/일 가능. 품질도 baseline 이상 |
| B | 20~50게임/일 가능. scheduler 운영 시간 조정 필요 |
| C | 10~20게임/일 가능. 품질 개선이 확실할 때만 채택 |
| D | 10게임/일 미만. CPU 운영 기본값으로 부적합 |

## 6. 측정 지표

### 6-1. 필수 지표

| 지표 | 의미 | 목표 |
|---|---|---|
| `map_llm_valid_chunks` | LLM JSON이 repair 없이 통과한 chunk 수 | 높을수록 좋음 |
| `map_llm_repaired_chunks` | LLM 선택 결과를 repair로 복구한 chunk 수 | 허용하되 과도하면 감점 |
| `map_deterministic_fallback_chunks` | LLM 실패 후 deterministic fallback 사용 | 0이어야 함 |
| `map_json_invalid_chunks` | JSON parse/schema 실패 | 낮을수록 좋음 |
| `map_empty_evidence_chunks` | evidence 공백 | 0이어야 함 |
| chunk 평균 처리 시간 | CPU 속도 | baseline 대비 평가 |
| gate result | 자동 품질 gate | 모든 게임 passed |

### 6-2. 품질 지표

출력에서 다음이 보이면 가점한다.

- 일반론이 아니라 플레이 조건, 문제 유형, 감정 영향, 구매 판단에 필요한 구체 경험을 포함함
- 스포일러 고유명사를 직접 노출하지 않고도 원문 경험을 설명함
- 예: "후반부 대형 보스전에서 반복 실패로 피로감을 느꼈다", "특정 엔딩 연출 이후 강제 종료를 겪었다"
- 긍정/부정이 모두 evidence로 분리됨
- `snippet`이 원문에 존재함
- `review_id` anchor가 출력과 연결됨
- user/critic/playtime 그룹이 서로 섞이지 않음

다음이 보이면 감점한다.

- `"전투가 좋다"`, `"콘텐츠가 다양하다"` 같은 일반론 반복
- 특정 보스명, 엔딩명, 반전, 캐릭터 사망, 후반 지역명, 퀘스트 결말을 공개 출력에 그대로 노출
- 원문에 없는 사실 생성
- `review_id` hallucination
- 한국어 리뷰를 의미가 변형된 영어로 재작성
- `evidence_items`가 많아도 detail이 모두 비슷함
- 스포일러 위험 문장을 단순 삭제해 정보량이 다시 일반론 수준으로 떨어짐

## 7. 결과 기록 양식

각 모델별로 아래 표를 채운다.

| 모델 | size | ctx | review_limit | games | chunks | valid | repaired | fallback | avg chunk sec | gate | 비고 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| `qwen2.5:1.5b` | 986MB | 32K | 36 | 2 |  |  |  |  |  |  | baseline |
| `qwen3.5:2b` |  |  | 36 | 2 |  |  |  |  |  |  |  |
| `qwen3.5:4b` |  |  | 36 | 2 |  |  |  |  |  |  |  |
| `phi4-mini:3.8b` |  |  | 36 | 2 |  |  |  |  |  |  |  |
| `qwen3:4b` |  |  | 36 | 2 |  |  |  |  |  |  |  |
| `gemma3:4b-it-qat` |  |  | 36 | 2 |  |  |  |  |  |  |  |
| `llama3.2:3b` |  |  | 36 | 2 |  |  |  |  |  |  |  |

출력 근거 예시도 모델별로 최소 5개 기록한다.

```text
모델:
게임:
근거 예시:
- 
- 
- 
- 
- 
문제:
- 
```

## 8. 최종 판정 규칙

### 8-1. 기본값 교체 조건

다음 조건을 모두 만족해야 `LOCAL_MAP_MODEL` 기본값 교체를 검토한다.

- 현재 DB 전체 dry-run 통과
- `map_deterministic_fallback_chunks=0`
- baseline보다 evidence detail 품질이 명확히 좋음
- 평균 chunk 처리 시간이 baseline 대비 허용 범위 안
- 72 review 확장 테스트에서 timeout이 없음
- `summary`, `pros`, `cons`, `keywords`의 일반론 반복이 줄어듦

### 8-2. 모델별 예상 판정

| 모델 | 기대 판정 |
|---|---|
| `qwen3.5:2b` | 1차 교체 후보. 품질 개선이 있고 속도 저하가 작으면 기본값 후보 |
| `qwen3.5:4b` | 품질 우선 후보. CPU 시간이 허용되면 기본값 또는 고품질 모드 후보 |
| `phi4-mini:3.8b` | JSON 안정성 후보. evidence 품질이 좋으면 기본값 후보 |
| `qwen3:4b` | 비교군. Qwen3.5보다 나으면 후보 유지 |
| `gemma3:4b-it-qat` | 다국어/요약 후보. 한국어 evidence 품질이 좋을 때만 유지 |
| `llama3.2:3b` | 보조 비교군. 한국어/JSON 품질이 약하면 제외 |

### 8-3. 운영 모드 분리 가능성

테스트 결과에 따라 모델을 하나로 고정하지 않고 모드를 나눌 수 있다.

| 모드 | 모델 | 용도 |
|---|---|---|
| fast | `qwen2.5:1.5b` 또는 `qwen3.5:2b` | 데모, 빠른 회귀 테스트, 저사양 CPU |
| balanced | `qwen3.5:4b` 또는 `phi4-mini:3.8b` | 기본 배치 후보 |
| quality | `qwen3.5:9b` | 소량 게임 고품질 재요약 전용 |

## 9. 검증 완료 후 반영 작업

모델 교체가 결정되면 다음 파일을 갱신한다.

- `docker-compose.yml`: backend/scheduler `LOCAL_MAP_MODEL`
- `demo.py`: `DEFAULT_LOCAL_MAP_MODEL`
- `backend/app/services/ai_service.py`: fallback 기본값
- `docs/plan-cpu-deployment.md`: Map 모델 선택 근거 갱신
- `docs/review-quality-work-summary-YYYY-MM-DD.md`: 실측 결과 기록

변경 후 최소 검증:

```bash
python demo.py --test --scenario all --skip-docker --skip-crawl --timeout 900 --verify-frontend
```

Docker 전체 검증이 가능하면:

```bash
python demo.py --test --scenario all --reset-volumes --force --timeout 900 --verify-frontend
```

## 10. 결론

현 시점에서 `qwen2.5:1.5b`는 여전히 안전한 baseline이다. 하지만 최신 `json_v2_llm_map` 파이프라인에서는 `qwen3.5:2b`, `qwen3.5:4b`, `phi4-mini:3.8b`가 실질적인 개선 후보로 보인다.

모델 교체는 단일 샘플 응답이나 일반 벤치마크가 아니라, 현재 DB와 `dry_quality_run.py --assert-gates` 기준으로 결정해야 한다. 특히 CPU 환경에서는 품질 개선이 있어도 chunk 처리 시간이 배치 운영 한계를 넘으면 기본값으로 채택하지 않는다.
