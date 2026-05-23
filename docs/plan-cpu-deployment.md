# CPU 배포·로컬 CPU 테스트 계획

> 대상: GPU 없는 학교 클라우드 배포 + 로컬 CPU 환경 풀 파이프라인 테스트.
> 근거: `ai-pipeline/benchmark_map_models.py`, `docs/appreviews_migration_proposal.md` §6·§7.
> 본 문서가 CPU 배포 계획의 단일 출처(SoT). 메모리 `학교 클라우드 CPU 배포 계획`은 본 문서를 참조.

## 1. 배경

학교 클라우드는 GPU가 없어 Ollama 로컬 LLM이 CPU로 동작해야 한다. 로컬 LLM + 외부 API 하이브리드 아키텍처는 캡스톤 기술 기여 요소이므로 **유지**하되, Map 단계 모델·파라미터·Docker 설정을 CPU에 맞게 조정한다.

## 2. Map 모델 선택 — 벤치마크 기반 정정

> **정정 (2026-05-18)**: 초기 계획은 "Map 모델 `gemma3:4b → gemma3:1b`"였으나 벤치마크로 **반증**됨. `gemma3:1b`는 정확도·속도 모두 열위. **`qwen2.5:1.5b` 채택**으로 정정.

### 벤치마크 결과 (Map 단계, 3 청크: 영/한/혼합)

| 모델 | 성공률 | 평균 속도 | 출력 토큰 | 비고 |
|---|---|---|---|---|
| `gemma3:1b` | 2/3 | 18.35s | 289 | 한국어 청크 포맷 실패 |
| **`qwen2.5:1.5b`** | **3/3** | **2.37s** | 311 | **채택** |
| `qwen3:1.7b` (thinking ON) | 3/3 | 10.95s | 3841 | thinking 과토큰 |
| `qwen3:1.7b` (no_think) | 3/3 | 7.02s | 2944 | qwen2.5보다 6배 느림 |
| `gemma3n:e2b` | 2/3 | 4.22s | 459 | 한국어→영어 변환 |

### 결론

- **Map 단계 = `qwen2.5:1.5b`.** 정확도(3/3) 최고이자 속도(2.37s) 최고 — `gemma3:1b` 대비 약 8배 빠름. CPU에서 추론 속도가 최우선 지표이므로 GPU/CPU 무관하게 최선.
- Map 출력 언어는 영어 고정([map_local.py](../ai-pipeline/ai_module/map_reduce/map_local.py), "Output in English regardless of the review language.") — 한국어 포맷 실패·언어 혼입 회피.

## 3. 배포·테스트 전 필수 조치

| # | 항목 | 현재 상태 | 조치 |
|---|---|---|---|
| 1 | Map 모델 정합 | `docker-compose.yml` `LOCAL_MAP_MODEL=gemma3:1b`(backend·scheduler), [ai_service.py](../backend/app/services/ai_service.py) 기본값 `gemma3:4b` | 둘 다 **`qwen2.5:1.5b`로 통일**. ollama pull 경로 확인(demo.py) |
| 2 | `OLLAMA_NUM_CTX` | compose에 **주석만**, 미설정 → 기본 4096(GPU 가정) | backend(·scheduler) env에 **`OLLAMA_NUM_CTX=2048` 실제 추가**. 연쇄로 `num_predict` 자동 400([map_local.py:61](../ai-pipeline/ai_module/map_reduce/map_local.py#L61)), chunker 안전 입력 1448토큰 자동 축소([chunker.py](../ai-pipeline/ai_module/map_reduce/chunker.py)). 미설정 시 5500자 GPU 가정 → CPU OOM·초장시간 |
| 3 | GPU 예약 블록 | `docker-compose.yml` ollama `deploy.resources.reservations: nvidia` 존재 | CPU 호스트에서 nvidia 런타임 부재 시 ollama 컨테이너 **기동 실패** → 제거 또는 override 분리. **로컬 CPU 테스트 최우선 blocker** |
| — | `num_thread=0` | map_local 옵션 블록 미구현 | **계획에서 드롭**. Ollama에서 0=전 코어 자동(기본 동작과 동일), 실익 없음 |

## 4. 로컬 풀 파이프라인 CPU 테스트 절차

1. 조치 #3: docker-compose ollama GPU 예약 블록 비활성(주석/override)
2. 조치 #2: backend(·scheduler) env `OLLAMA_NUM_CTX=2048` 추가
3. 조치 #1: `LOCAL_MAP_MODEL=qwen2.5:1.5b` 통일 + `qwen2.5:1.5b` ollama pull 확인
4. `python demo.py` — 크롤 → API 적재 → 요약 → 기능 C·D 검증
5. (선택) `python -m app.jobs.price_refresher --once` — 기능 A 별도 검증 (buy-signal은 스냅샷 없으면 graceful degrade하므로 풀 파이프라인 테스트와 독립)

## 5. 범위 메모

- 리프레셔/스케줄러는 본 테스트와 독립 잡 — buy-signal graceful degrade로 풀 파이프라인 영향 없음.
- 클라우드 배포 환경 정비(compose 환경 분리·backend Dockerfile·secrets 등)는 별도 트랙(필요 시 협력). 본 문서는 Map/Ollama/GPU 한정.
